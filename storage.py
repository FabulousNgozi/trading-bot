"""
storage.py — durable state for the bot.

Two stores, deliberately separate:

* **SQLite** (``data/bot.db``) is the source of truth for risk state. The
  daily P/L, the trade counter, the consecutive-loss counter and the
  open-contract record all live here so that a restart cannot reset them.
  This is the single most important safety property of the module: a bot that
  forgets it already lost $5 today is worse than no bot.

* **CSV** (``data/trades.csv``) is a human-readable append-only trade log for
  spreadsheet review. It is a convenience export, never the authority.

Trading-day boundary
--------------------
The "day" is defined by ``TRADING_DAY_TIMEZONE`` and rolls over at local
midnight in that zone. All daily counters are keyed by that date string, so
changing the timezone changes the boundary — document it, don't hide it.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id       INTEGER UNIQUE,
    symbol            TEXT    NOT NULL,
    index_value       INTEGER,
    direction         TEXT    NOT NULL,          -- CALL / PUT
    stake             REAL    NOT NULL,
    payout            REAL,
    buy_price         REAL,
    duration          INTEGER NOT NULL,
    duration_unit     TEXT    NOT NULL,
    entry_spot        REAL,
    exit_spot         REAL,
    status            TEXT    NOT NULL,          -- pending / won / lost / sold / unresolved
    profit            REAL,
    signal_score      INTEGER,
    signal_reasons    TEXT,
    signal_features   TEXT    NOT NULL DEFAULT '{}',
    base_stake        REAL,
    martingale_enabled INTEGER NOT NULL DEFAULT 0,
    martingale_step   INTEGER NOT NULL DEFAULT 0,
    strategy_variant  TEXT    NOT NULL DEFAULT 'baseline',
    strategy_id       TEXT    NOT NULL DEFAULT 'trend_confluence_v1',
    trading_day       TEXT    NOT NULL,
    opened_at         TEXT    NOT NULL,
    closed_at         TEXT,
    resolved          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_day    ON trades(trading_day);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS daily_state (
    trading_day        TEXT PRIMARY KEY,
    realised_pnl       REAL    NOT NULL DEFAULT 0,
    trades_count       INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS ai_duration_studies (
    symbol                 TEXT NOT NULL,
    strategy_id            TEXT NOT NULL,
    strategy_variant       TEXT NOT NULL,
    duration_minutes       INTEGER NOT NULL,
    data_end_epoch         INTEGER NOT NULL,
    train_trades           INTEGER NOT NULL DEFAULT 0,
    train_wins             INTEGER NOT NULL DEFAULT 0,
    train_net_profit       REAL NOT NULL DEFAULT 0,
    validation_trades      INTEGER NOT NULL DEFAULT 0,
    validation_wins        INTEGER NOT NULL DEFAULT 0,
    validation_win_rate    REAL NOT NULL DEFAULT 0,
    validation_net_profit  REAL NOT NULL DEFAULT 0,
    validation_drawdown    REAL NOT NULL DEFAULT 0,
    payout_multiple        REAL NOT NULL DEFAULT 1.95,
    payout_samples         INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (symbol, strategy_id, strategy_variant, duration_minutes)
);

CREATE TABLE IF NOT EXISTS shadow_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    strategy_id         TEXT NOT NULL,
    strategy_variant    TEXT NOT NULL,
    direction           TEXT NOT NULL,
    setup_key           TEXT NOT NULL,
    signal_epoch        INTEGER NOT NULL,
    entry_epoch         INTEGER NOT NULL,
    exit_candle_epoch   INTEGER NOT NULL,
    duration_minutes    INTEGER NOT NULL,
    signal_score        INTEGER NOT NULL,
    signal_approved     INTEGER NOT NULL DEFAULT 1,
    entry_price         REAL,
    exit_price          REAL,
    won                 INTEGER,
    resolved            INTEGER NOT NULL DEFAULT 0,
    selected_for_trade  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    UNIQUE(symbol, strategy_id, strategy_variant, direction, signal_epoch, duration_minutes)
);
CREATE INDEX IF NOT EXISTS idx_shadow_pending
    ON shadow_signals(symbol, resolved, exit_candle_epoch);
"""


def payout_estimates_from_db(db_path: str, symbol: str,
                            sample_limit: int = 100) -> Dict[int, Dict[str, float]]:
    """Read recent per-duration winning payout ratios without locking the bot DB."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        rows = conn.execute(
            """SELECT duration,stake,profit FROM trades WHERE symbol=?
               AND duration_unit='m' AND status='won' AND resolved=1
               AND stake>0 AND profit IS NOT NULL ORDER BY id DESC LIMIT ?""",
            (symbol, sample_limit),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return {}
    finally:
        if "conn" in locals():
            conn.close()
    grouped: Dict[int, List[float]] = {}
    for duration, stake, profit in rows:
        multiple = 1.0 + float(profit) / float(stake)
        if 1.0 < multiple < 5.0:
            grouped.setdefault(int(duration), []).append(multiple)
    return {duration: {"payout_multiple": statistics.median(values),
                       "samples": len(values)}
            for duration, values in grouped.items()}


@dataclass
class TradeRecord:
    contract_id: Optional[int]
    symbol: str
    index_value: Optional[int]
    direction: str
    stake: float
    duration: int
    duration_unit: str
    status: str = "pending"
    payout: Optional[float] = None
    buy_price: Optional[float] = None
    entry_spot: Optional[float] = None
    exit_spot: Optional[float] = None
    profit: Optional[float] = None
    signal_score: Optional[int] = None
    signal_reasons: str = ""
    signal_features: str = "{}"
    base_stake: Optional[float] = None
    martingale_enabled: int = 0
    martingale_step: int = 0
    strategy_variant: str = "baseline"
    strategy_id: str = "trend_confluence_v1"
    trading_day: str = ""
    opened_at: str = ""
    closed_at: Optional[str] = None
    resolved: int = 0


class Storage:
    """SQLite + CSV persistence with restart-safe risk state."""

    def __init__(self, db_path: str = "data/bot.db",
                 csv_path: str = "data/trades.csv",
                 timezone_name: str = "UTC") -> None:
        self.db_path = db_path
        self.csv_path = csv_path
        self.timezone_name = timezone_name
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if csv_path:
            Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(trades)").fetchall()}
        if "strategy_variant" not in columns:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN strategy_variant "
                "TEXT NOT NULL DEFAULT 'baseline'")
        if "strategy_id" not in columns:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN strategy_id "
                "TEXT NOT NULL DEFAULT 'trend_confluence_v1'")
        if "signal_features" not in columns:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN signal_features "
                "TEXT NOT NULL DEFAULT '{}'" )
        for name, definition in (
            ("base_stake", "REAL"),
            ("martingale_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("martingale_step", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {name} {definition}")
        duration_columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(ai_duration_studies)").fetchall()}
        for name, definition in (("payout_multiple", "REAL NOT NULL DEFAULT 1.95"),
                                 ("payout_samples", "INTEGER NOT NULL DEFAULT 0")):
            if name not in duration_columns:
                self._conn.execute(
                    f"ALTER TABLE ai_duration_studies ADD COLUMN {name} {definition}")
        shadow_columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(shadow_signals)").fetchall()}
        if "signal_approved" not in shadow_columns:
            self._conn.execute(
                "ALTER TABLE shadow_signals ADD COLUMN signal_approved "
                "INTEGER NOT NULL DEFAULT 1")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    def _tz(self):
        if ZoneInfo is None:
            return timezone.utc
        try:
            return ZoneInfo(self.timezone_name)
        except Exception:
            return timezone.utc

    def now(self) -> datetime:
        return datetime.now(self._tz())

    def trading_day(self, when: Optional[datetime] = None) -> str:
        """Local date string (YYYY-MM-DD) in the configured trading timezone."""
        when = when or self.now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(self._tz()).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Daily state
    # ------------------------------------------------------------------
    def get_daily_state(self, day: Optional[str] = None) -> Dict[str, Any]:
        day = day or self.trading_day()
        row = self._conn.execute(
            "SELECT * FROM daily_state WHERE trading_day = ?", (day,)
        ).fetchone()
        if row is None:
            return {"trading_day": day, "realised_pnl": 0.0, "trades_count": 0}
        return {
            "trading_day": row["trading_day"],
            "realised_pnl": float(row["realised_pnl"]),
            "trades_count": int(row["trades_count"]),
        }

    def _ensure_day(self, day: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO daily_state (trading_day, realised_pnl, "
            "trades_count, updated_at) VALUES (?, 0, 0, ?)",
            (day, self.now().isoformat()),
        )

    def add_realised_pnl(self, amount: float, day: Optional[str] = None) -> None:
        day = day or self.trading_day()
        self._ensure_day(day)
        self._conn.execute(
            "UPDATE daily_state SET realised_pnl = realised_pnl + ?, "
            "updated_at = ? WHERE trading_day = ?",
            (amount, self.now().isoformat(), day),
        )
        self._conn.commit()

    def increment_trade_count(self, day: Optional[str] = None) -> None:
        day = day or self.trading_day()
        self._ensure_day(day)
        self._conn.execute(
            "UPDATE daily_state SET trades_count = trades_count + 1, "
            "updated_at = ? WHERE trading_day = ?",
            (self.now().isoformat(), day),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Risk state (consecutive losses, pause, last trade time)
    # ------------------------------------------------------------------
    def get_risk(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM risk_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_risk(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO risk_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_consecutive_losses(self) -> int:
        return int(self.get_risk("consecutive_losses", "0") or 0)

    def set_consecutive_losses(self, n: int) -> None:
        self.set_risk("consecutive_losses", str(max(0, int(n))))

    def get_paused_until(self) -> Optional[datetime]:
        raw = self.get_risk("paused_until")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def set_paused_until(self, when: Optional[datetime]) -> None:
        self.set_risk("paused_until", when.isoformat() if when else "")

    def get_last_trade_ts(self) -> Optional[datetime]:
        raw = self.get_risk("last_trade_ts")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def set_last_trade_ts(self, when: datetime) -> None:
        self.set_risk("last_trade_ts", when.isoformat())

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def insert_trade(self, rec: TradeRecord) -> int:
        if not rec.trading_day:
            rec.trading_day = self.trading_day()
        if not rec.opened_at:
            rec.opened_at = self.now().isoformat()
        cur = self._conn.execute(
            """INSERT INTO trades
               (contract_id, symbol, index_value, direction, stake, payout,
                buy_price, duration, duration_unit, entry_spot, exit_spot,
                status, profit, signal_score, signal_reasons, strategy_variant,
                strategy_id, signal_features, base_stake, martingale_enabled,
                martingale_step,
                trading_day, opened_at, closed_at, resolved)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.contract_id, rec.symbol, rec.index_value, rec.direction,
             rec.stake, rec.payout, rec.buy_price, rec.duration,
             rec.duration_unit, rec.entry_spot, rec.exit_spot, rec.status,
             rec.profit, rec.signal_score, rec.signal_reasons,
             rec.strategy_variant, rec.strategy_id, rec.signal_features,
             rec.base_stake, rec.martingale_enabled, rec.martingale_step,
             rec.trading_day, rec.opened_at,
             rec.closed_at, rec.resolved),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_trade_result(self, contract_id: int, status: str,
                            profit: Optional[float] = None,
                            payout: Optional[float] = None,
                            exit_spot: Optional[float] = None,
                            resolved: bool = True) -> None:
        self._conn.execute(
            """UPDATE trades SET status = ?, profit = COALESCE(?, profit),
               payout = COALESCE(?, payout),
               exit_spot = COALESCE(?, exit_spot),
               closed_at = ?, resolved = ?
               WHERE contract_id = ?""",
            (status, profit, payout, exit_spot, self.now().isoformat(),
             1 if resolved else 0, contract_id),
        )
        self._conn.commit()

    def get_trade(self, contract_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM trades WHERE contract_id = ?", (contract_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_unresolved_trades(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE resolved = 0"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status = 'pending'"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_trades(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status = 'pending'"
        ).fetchone()
        return int(row["c"])

    def recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def resolved_trade_performance(self, strategy_id: Optional[str] = None
                                   ) -> Dict[str, Dict[str, float]]:
        """Return resolved outcomes grouped by market and direction."""
        sql = """SELECT symbol, direction, stake, base_stake, profit
               FROM trades
               WHERE resolved = 1 AND status IN ('won', 'lost', 'sold')
                     AND profit IS NOT NULL"""
        params = ()
        if strategy_id:
            sql += " AND strategy_id = ?"
            params = (strategy_id,)
        sql += " ORDER BY closed_at, id"
        rows = self._conn.execute(sql, params).fetchall()
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = f"{row['symbol']}:{row['direction']}"
            item = grouped.setdefault(key, {"trades": 0, "wins": 0,
                "net_profit": 0.0, "flat_net_profit": 0.0, "base_stake_total": 0.0})
            stake = float(row["stake"] or 0.0)
            base = float(row["base_stake"] or stake)
            profit = float(row["profit"])
            item["trades"] += 1
            item["wins"] += int(profit > 0)
            item["net_profit"] += profit
            item["base_stake_total"] += base
            item["flat_net_profit"] += profit * base / stake if stake > 0 else profit
        return grouped

    def resolved_policy_performance(self, strategy_id: str
                                    ) -> Dict[str, Dict[str, float]]:
        """Research resolved outcomes by the strategy variant that placed them."""
        rows = self._conn.execute(
            """SELECT strategy_variant, stake, base_stake, profit
               FROM trades
               WHERE strategy_id = ? AND resolved = 1
                     AND status IN ('won', 'lost', 'sold')
                     AND profit IS NOT NULL ORDER BY closed_at, id""", (strategy_id,)
        ).fetchall()
        grouped: Dict[str, Dict[str, Any]] = {}
        curves: Dict[str, List[float]] = {}
        for row in rows:
            name = row["strategy_variant"]
            item = grouped.setdefault(name, {"trades": 0, "wins": 0,
                "net_profit": 0.0, "flat_net_profit": 0.0,
                "flat_stake_total": 0.0, "profit_factor": 0.0,
                "gross_flat_wins": 0.0, "flat_win_count": 0,
                "gross_flat_losses": 0.0, "flat_loss_count": 0})
            stake = float(row["stake"] or 0.0)
            base = float(row["base_stake"] or stake)
            profit = float(row["profit"])
            flat_profit = profit * base / stake if stake > 0 else profit
            item["trades"] += 1
            item["wins"] += int(profit > 0)
            item["net_profit"] += profit
            item["flat_net_profit"] += flat_profit
            item["flat_stake_total"] += base
            if flat_profit > 0:
                item["gross_flat_wins"] += flat_profit
                item["flat_win_count"] += 1
            elif flat_profit < 0:
                item["gross_flat_losses"] += abs(flat_profit)
                item["flat_loss_count"] += 1
            curves.setdefault(name, []).append(flat_profit)
        for name, item in grouped.items():
            equity = peak = drawdown = gross_wins = gross_losses = 0.0
            for row_profit in curves[name]:
                equity += row_profit
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
                if row_profit > 0:
                    gross_wins += row_profit
                elif row_profit < 0:
                    gross_losses += abs(row_profit)
            item["flat_max_drawdown"] = drawdown
            streak = longest = 0
            for value in curves[name]:
                streak = 0 if value > 0 else streak + 1
                longest = max(longest, streak)
            item["max_losing_streak"] = longest
            item["average_win"] = (item["gross_flat_wins"] / item["flat_win_count"]
                                   if item["flat_win_count"] else 0.0)
            item["average_loss"] = (item["gross_flat_losses"] / item["flat_loss_count"]
                                    if item["flat_loss_count"] else 0.0)
            item["profit_factor"] = (gross_wins / gross_losses
                                     if gross_losses else float("inf") if gross_wins else 0.0)
        return grouped

    def resolved_price_action_performance(self) -> List[Dict[str, Any]]:
        """Summarize settled outcomes by market, strategy, and saved setup."""
        rows = self._conn.execute(
            """SELECT symbol, strategy_id, strategy_variant, direction,
                      signal_features, profit, stake, base_stake
               FROM trades WHERE resolved=1 AND status IN ('won','lost','sold')
                 AND profit IS NOT NULL ORDER BY id"""
        ).fetchall()
        groups: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            try:
                features = json.loads(row["signal_features"] or "{}")
            except (TypeError, ValueError):
                features = {}
            if not isinstance(features, dict) or not features.get("setup_key"):
                continue
            key = (row["symbol"], row["strategy_id"], row["strategy_variant"],
                   row["direction"], str(features["setup_key"]))
            item = groups.setdefault(key, {"symbol": key[0], "strategy_id": key[1],
                "policy": key[2], "direction": key[3], "setup_key": key[4],
                "trades": 0, "wins": 0, "net_profit": 0.0,
                "gross_flat_wins": 0.0, "flat_win_count": 0,
                "gross_flat_losses": 0.0, "flat_loss_count": 0,
                "flat_profits": []})
            item["trades"] += 1
            item["wins"] += int(float(row["profit"]) > 0)
            item["net_profit"] += float(row["profit"])
            stake = float(row["stake"] or 0.0)
            base = float(row["base_stake"] or stake)
            item.setdefault("flat_net_profit", 0.0)
            item.setdefault("flat_stake_total", 0.0)
            item["flat_net_profit"] += float(row["profit"]) * base / stake if stake > 0 else float(row["profit"])
            item["flat_stake_total"] += base
            flat_profit = float(row["profit"]) * base / stake if stake > 0 else float(row["profit"])
            item["flat_profits"].append(flat_profit)
            if flat_profit > 0:
                item["gross_flat_wins"] += flat_profit
                item["flat_win_count"] += 1
            elif flat_profit < 0:
                item["gross_flat_losses"] += abs(flat_profit)
                item["flat_loss_count"] += 1
        for item in groups.values():
            streak = longest = 0
            for value in item.pop("flat_profits"):
                streak = 0 if value > 0 else streak + 1
                longest = max(longest, streak)
            item["max_losing_streak"] = longest
            item["average_win"] = (item["gross_flat_wins"] / item["flat_win_count"]
                                   if item["flat_win_count"] else 0.0)
            item["average_loss"] = (item["gross_flat_losses"] / item["flat_loss_count"]
                                    if item["flat_loss_count"] else 0.0)
        return list(groups.values())

    def record_shadow_signal(self, signal: Any, duration_minutes: int) -> Optional[int]:
        """Queue a closed-candle signal for a next-open/expiry-close shadow result."""
        direction = signal.direction
        if direction not in ("CALL", "PUT"):
            direction = {"up": "CALL", "down": "PUT"}.get(signal.trend_1m)
        if (direction not in ("CALL", "PUT") or signal.candle_epoch is None
                or not signal.price_action.get("setup_key")):
            return None
        duration = (max(1, round(duration_minutes / 60))
                    if duration_minutes >= 60 else duration_minutes)
        duration = duration if duration in (1, 2, 3, 5) else 1
        setup_key = str(signal.price_action.get("setup_key") or "unknown")
        signal_epoch = int(signal.candle_epoch)
        self._conn.execute(
            """INSERT OR IGNORE INTO shadow_signals
               (symbol,strategy_id,strategy_variant,direction,setup_key,
                signal_epoch,entry_epoch,exit_candle_epoch,duration_minutes,
                signal_score,signal_approved,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal.symbol, signal.strategy_id, signal.strategy_variant,
             direction, setup_key, signal_epoch, signal_epoch + 60,
             signal_epoch + duration * 60, duration, int(signal.score),
             int(signal.actionable),
             self.now().isoformat()),
        )
        row = self._conn.execute(
            """SELECT id FROM shadow_signals WHERE symbol=? AND strategy_id=?
               AND strategy_variant=? AND direction=? AND signal_epoch=?
               AND duration_minutes=?""",
            (signal.symbol, signal.strategy_id, signal.strategy_variant,
             direction, signal_epoch, duration),
        ).fetchone()
        self._conn.commit()
        return int(row["id"]) if row else None

    def resolve_shadow_signals(self, symbol: str, candles: List[Any]) -> int:
        """Resolve pending shadows using completed bars only, like the backtest."""
        pending = self._conn.execute(
            "SELECT * FROM shadow_signals WHERE symbol=? AND resolved=0",
            (symbol,),
        ).fetchall()
        by_epoch = {int(c.epoch): c for c in candles}
        resolved = 0
        for row in pending:
            entry = by_epoch.get(int(row["entry_epoch"]))
            expiry = by_epoch.get(int(row["exit_candle_epoch"]))
            if entry is None or expiry is None:
                continue
            entry_price, exit_price = float(entry.open), float(expiry.close)
            won = (exit_price > entry_price if row["direction"] == "CALL"
                   else exit_price < entry_price)
            self._conn.execute(
                """UPDATE shadow_signals SET entry_price=?,exit_price=?,won=?,
                   resolved=1,resolved_at=? WHERE id=? AND resolved=0""",
                (entry_price, exit_price, int(won), self.now().isoformat(), row["id"]),
            )
            resolved += 1
        if resolved:
            self._conn.commit()
        return resolved

    def mark_shadow_selected(self, shadow_id: Optional[int]) -> None:
        if shadow_id is None:
            return
        self._conn.execute(
            "UPDATE shadow_signals SET selected_for_trade=1 WHERE id=?",
            (shadow_id,),
        )
        self._conn.commit()

    def shadow_signal_performance(self) -> List[Dict[str, Any]]:
        """Group resolved shadow price-direction outcomes by market and setup."""
        rows = self._conn.execute(
            """SELECT symbol,strategy_variant,direction,setup_key,selected_for_trade,
                      signal_approved,
                      COUNT(*) AS trades,SUM(won) AS wins
               FROM shadow_signals WHERE resolved=1
               GROUP BY symbol,strategy_variant,direction,setup_key,selected_for_trade,
                        signal_approved
               ORDER BY trades DESC"""
        ).fetchall()
        return [{"symbol": row["symbol"], "policy": row["strategy_variant"],
                 "direction": row["direction"], "setup_key": row["setup_key"],
                 "selected": bool(row["selected_for_trade"]),
                 "approved": bool(row["signal_approved"]),
                 "trades": int(row["trades"]), "wins": int(row["wins"] or 0),
                 "win_rate": round(100 * int(row["wins"] or 0) /
                                   int(row["trades"]), 1)} for row in rows]

    def resolved_payout_estimates(self, symbol: str,
                                  sample_limit: int = 100) -> Dict[int, Dict[str, float]]:
        """Use recent settled winning contracts to estimate per-duration payout."""
        return payout_estimates_from_db(self.db_path, symbol, sample_limit)

    def martingale_vs_flat_performance(self) -> Dict[str, Any]:
        """Compare actual martingale P/L with a scaled base-stake counterfactual."""
        rows = self._conn.execute(
            """SELECT symbol,stake,base_stake,profit,closed_at,id FROM trades
               WHERE martingale_enabled=1 AND resolved=1
                 AND status IN ('won','lost','sold') AND profit IS NOT NULL
               ORDER BY closed_at,id"""
        ).fetchall()
        grouped: Dict[str, List[tuple]] = {"ALL": []}
        for row in rows:
            stake = float(row["stake"] or 0.0)
            base = float(row["base_stake"] or stake)
            actual = float(row["profit"])
            flat = actual * base / stake if stake > 0 else actual
            grouped.setdefault(row["symbol"], []).append((actual, flat))
            grouped["ALL"].append((actual, flat))

        def summarize(items: List[tuple]) -> Dict[str, Any]:
            result = {"trades": len(items)}
            for label, index in (("martingale", 0), ("flat", 1)):
                equity = peak = drawdown = net = 0.0
                wins: List[float] = []
                losses: List[float] = []
                streak = longest_streak = 0
                for item in items:
                    value = item[index]
                    net += value
                    equity += value
                    peak = max(peak, equity)
                    drawdown = max(drawdown, peak - equity)
                    if value > 0:
                        wins.append(value)
                        streak = 0
                    else:
                        if value < 0:
                            losses.append(abs(value))
                        streak += 1
                        longest_streak = max(longest_streak, streak)
                result[f"{label}_net_profit"] = round(net, 2)
                result[f"{label}_max_drawdown"] = round(drawdown, 2)
                result[f"{label}_average_win"] = round(sum(wins) / len(wins), 2) if wins else 0.0
                result[f"{label}_average_loss"] = round(sum(losses) / len(losses), 2) if losses else 0.0
                result[f"{label}_max_losing_streak"] = longest_streak
            result["flat_model"] = "realized return scaled to base stake (estimate)"
            return result

        return {"overall": summarize(grouped["ALL"]),
                "by_market": [{"symbol": symbol, **summarize(items)}
                              for symbol, items in grouped.items() if symbol != "ALL"]}

    def save_duration_study(self, symbol: str, strategy_id: str,
                            strategy_variant: str, data_end_epoch: int,
                            results: Dict[int, Dict[str, object]]) -> None:
        """Persist the latest train/validation duration comparison."""
        now = self.now().isoformat()
        for duration, row in results.items():
            self._conn.execute(
                """INSERT INTO ai_duration_studies
                   (symbol, strategy_id, strategy_variant, duration_minutes,
                    data_end_epoch, train_trades, train_wins, train_net_profit,
                    validation_trades, validation_wins, validation_win_rate,
                    validation_net_profit, validation_drawdown,
                    payout_multiple, payout_samples, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, strategy_id, strategy_variant,
                               duration_minutes) DO UPDATE SET
                     data_end_epoch=excluded.data_end_epoch,
                     train_trades=excluded.train_trades,
                     train_wins=excluded.train_wins,
                     train_net_profit=excluded.train_net_profit,
                     validation_trades=excluded.validation_trades,
                     validation_wins=excluded.validation_wins,
                     validation_win_rate=excluded.validation_win_rate,
                     validation_net_profit=excluded.validation_net_profit,
                     validation_drawdown=excluded.validation_drawdown,
                     payout_multiple=excluded.payout_multiple,
                     payout_samples=excluded.payout_samples,
                     updated_at=excluded.updated_at""",
                (symbol, strategy_id, strategy_variant, duration,
                 data_end_epoch, int(row.get("train_trades", 0)),
                 int(row.get("train_wins", 0)),
                 float(row.get("train_net_profit", 0.0)),
                 int(row.get("validation_trades", 0)),
                 int(row.get("validation_wins", 0)),
                 float(row.get("validation_win_rate", 0.0)),
                 float(row.get("validation_net_profit", 0.0)),
                 float(row.get("validation_drawdown", 0.0)),
                 float(row.get("payout_multiple", 1.95)),
                 int(row.get("payout_samples", 0)), now),
            )
        self._conn.commit()

    def get_duration_study(self, symbol: str, strategy_id: str,
                           strategy_variant: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM ai_duration_studies
               WHERE symbol=? AND strategy_id=? AND strategy_variant=?
               ORDER BY duration_minutes""",
            (symbol, strategy_id, strategy_variant),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolved_duration_performance(self, symbol: str, strategy_id: str,
                                      strategy_variant: str
                                      ) -> Dict[int, Dict[str, float]]:
        rows = self._conn.execute(
            """SELECT duration,stake,base_stake,profit
               FROM trades
               WHERE symbol=? AND strategy_id=? AND strategy_variant=?
                     AND resolved=1 AND status IN ('won','lost','sold')
                     AND profit IS NOT NULL ORDER BY closed_at,id""",
            (symbol, strategy_id, strategy_variant),
        ).fetchall()
        grouped: Dict[int, Dict[str, Any]] = {}
        curves: Dict[int, List[float]] = {}
        for row in rows:
            duration = int(row["duration"])
            stake, base = float(row["stake"] or 0), float(row["base_stake"] or row["stake"] or 0)
            profit = float(row["profit"])
            flat_profit = profit * base / stake if stake > 0 else profit
            item = grouped.setdefault(duration, {"trades": 0, "wins": 0,
                "net_profit": 0.0, "flat_net_profit": 0.0})
            item["trades"] += 1
            item["wins"] += int(profit > 0)
            item["net_profit"] += profit
            item["flat_net_profit"] += flat_profit
            curves.setdefault(duration, []).append(flat_profit)
        for duration, values in curves.items():
            equity = peak = drawdown = 0.0
            for value in values:
                equity += value
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
            grouped[duration]["flat_max_drawdown"] = drawdown
        return grouped

    def all_trades(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def log_event(self, level: str, event: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO events (ts, level, event, detail) VALUES (?,?,?,?)",
            (self.now().isoformat(), level, event, detail),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------
    def append_csv(self, rec: TradeRecord) -> None:
        if not self.csv_path:
            return
        path = Path(self.csv_path)
        new_file = not path.exists()
        fields = ["contract_id", "symbol", "index_value", "direction", "stake",
                  "payout", "buy_price", "duration", "duration_unit",
                  "entry_spot", "exit_spot", "status", "profit",
                  "signal_score", "trading_day", "opened_at", "closed_at",
                  "strategy_variant", "strategy_id", "signal_features",
                  "base_stake", "martingale_enabled", "martingale_step"]
        if not new_file:
            # Keep appending safely when upgrading an existing CSV with its old
            # header. SQLite remains the complete source for the new features.
            with path.open("r", newline="", encoding="utf-8") as existing:
                old_fields = next(csv.reader(existing), None)
            if old_fields:
                fields = old_fields
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow({k: getattr(rec, k, "") for k in fields})

    def export_csv(self, path: Optional[str] = None) -> str:
        """Write the full trade history to a CSV and return the path."""
        path = path or self.csv_path or "data/trades_export.csv"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        rows = self.all_trades()
        fields = ["id", "contract_id", "symbol", "index_value", "direction",
                  "stake", "payout", "buy_price", "duration", "duration_unit",
                  "entry_spot", "exit_spot", "status", "profit",
                  "signal_score", "strategy_variant", "strategy_id", "trading_day",
                  "opened_at", "closed_at",
                  "resolved", "signal_features", "base_stake",
                  "martingale_enabled", "martingale_step"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
