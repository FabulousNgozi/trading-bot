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
import os
import sqlite3
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
"""


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
                status, profit, signal_score, signal_reasons, trading_day,
                opened_at, closed_at, resolved)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.contract_id, rec.symbol, rec.index_value, rec.direction,
             rec.stake, rec.payout, rec.buy_price, rec.duration,
             rec.duration_unit, rec.entry_spot, rec.exit_spot, rec.status,
             rec.profit, rec.signal_score, rec.signal_reasons, rec.trading_day,
             rec.opened_at, rec.closed_at, rec.resolved),
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
                  "signal_score", "trading_day", "opened_at", "closed_at"]
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
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
                  "signal_score", "trading_day", "opened_at", "closed_at",
                  "resolved"]
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
