"""
state.py — thread-safe shared state between the bot thread and HTTP handlers.

Why a separate object
---------------------
The bot runs in a background thread; Flask serves requests on other threads.
Rather than letting handlers reach into ``TradingBot`` internals (which would
mean reading mutable engine objects from another thread), the bot *publishes*
a plain dict snapshot and handlers only ever read that. Publishing is done
under a lock, and every mutation bumps a monotonically increasing version.

Contents are deliberately JSON-safe: no dataclasses, no ``Signal`` objects,
no ``datetime`` — only primitives, so ``json.dumps`` can never fail mid-stream
and no engine object can escape into a response.

Security note
-------------
Nothing in this module ever stores or accepts a token. ``account_payload()``
whitelists exactly four fields; the API token has no route into this object.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional


# Bot lifecycle states surfaced to the UI.
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
PAUSED = "paused"
STOPPING = "stopping"
ERROR = "error"
NOT_CONFIGURED = "not_configured"


class BotState:
    """A snapshot store with a lock and a version counter."""

    LOG_CAPACITY = 600          # ring buffer of log lines kept for the UI
    TRADE_LIMIT = 200           # rows of trade history exposed

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._version = 0
        self._logs: Deque[Dict[str, Any]] = deque(maxlen=self.LOG_CAPACITY)

        self.status: str = STOPPED
        self.status_detail: str = "Not started."
        self.error: Optional[str] = None

        # Account — whitelisted fields only, never the token.
        self.account: Optional[Dict[str, Any]] = None

        # Engine risk snapshot (from RiskManager.status()).
        self.risk: Dict[str, Any] = {}

        # One row per configured market, refreshed every scan.
        self.scanner: List[Dict[str, Any]] = []
        self.last_scan_at: Optional[float] = None

        # Open contracts currently being monitored.
        self.open_contract: Optional[Dict[str, Any]] = None
        self.open_contracts: List[Dict[str, Any]] = []

        # Recent trades from SQLite.
        self.trades: List[Dict[str, Any]] = []

        # Last backtest run, if one has been executed from the UI.
        self.backtest: Optional[Dict[str, Any]] = None
        self.backtest_running: bool = False

        # Runtime stats.
        self.started_at: Optional[float] = None
        self.scans: int = 0
        self.signals_seen: int = 0

    # ------------------------------------------------------------------
    # Log ring buffer
    # ------------------------------------------------------------------
    def add_log(self, level: str, message: str) -> None:
        with self._lock:
            self._logs.append({
                "ts": time.time(),
                "level": level.upper(),
                "message": message,
            })
            self._version += 1

    def logs(self, since: float = 0.0, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._logs if r["ts"] >= since]
        return rows[-limit:]

    # ------------------------------------------------------------------
    # Mutators (each bumps the version so SSE can detect change)
    # ------------------------------------------------------------------
    def set_status(self, status: str, detail: str = "",
                   error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            if detail:
                self.status_detail = detail
            self.error = error
            if status == RUNNING and self.started_at is None:
                self.started_at = time.time()
            if status in (STOPPED, ERROR):
                self.started_at = None
                self.open_contract = None
                self.open_contracts = []
            self._version += 1

    def set_account(self, account: Optional[Dict[str, Any]]) -> None:
        """Store the four whitelisted account fields. Nothing else."""
        with self._lock:
            if account is None:
                self.account = None
            else:
                self.account = {
                    "account_id": account.get("account_id"),
                    "account_type": account.get("account_type"),
                    "currency": account.get("currency"),
                    "balance": account.get("balance"),
                }
            self._version += 1

    def set_risk(self, risk: Dict[str, Any]) -> None:
        with self._lock:
            self.risk = dict(risk or {})
            self._version += 1

    def set_scanner(self, rows: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.scanner = list(rows)
            self.last_scan_at = time.time()
            self.scans += 1
            self.signals_seen = sum(1 for r in rows if r.get("actionable"))
            self._version += 1

    def merge_scanner(self, rows: List[Dict[str, Any]]) -> None:
        """Merge a rotated batch, marking old readings as waiting/stale."""
        with self._lock:
            by_symbol = {r.get("symbol"): dict(r) for r in self.scanner}
            for row in by_symbol.values():
                row["actionable"] = False
                row["direction"] = "NONE"
                row["status"] = "waiting for next scan"
                row["reasons"] = ["not scanned in this batch"]
            for row in rows:
                by_symbol[row.get("symbol")] = dict(row)
            self.scanner = sorted(by_symbol.values(), key=lambda r: str(r.get("symbol", "")))
            self.last_scan_at = time.time()
            self.scans += 1
            self.signals_seen = sum(1 for r in self.scanner if r.get("actionable"))
            self._version += 1

    def set_open_contract(self, contract: Optional[Dict[str, Any]]) -> None:
        self.set_open_contracts([contract] if contract else [])

    def set_open_contracts(self, contracts: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.open_contracts = [dict(c) for c in contracts]
            self.open_contract = (
                dict(self.open_contracts[0]) if self.open_contracts else None
            )
            self._version += 1

    def set_trades(self, trades: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.trades = trades[: self.TRADE_LIMIT]
            self._version += 1

    def set_backtest(self, payload: Optional[Dict[str, Any]],
                     running: bool = False) -> None:
        with self._lock:
            self.backtest = payload
            self.backtest_running = running
            self._version += 1

    def clear_runtime(self) -> None:
        """Reset everything that is per-session, keeping config and logs."""
        with self._lock:
            self.account = None
            self.risk = {}
            self.scanner = []
            self.open_contract = None
            self.open_contracts = []
            self.started_at = None
            self.scans = 0
            self.signals_seen = 0
            self._version += 1

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def snapshot(self, include_trades: bool = True) -> Dict[str, Any]:
        """A complete, JSON-safe view of everything the UI renders."""
        with self._lock:
            now = time.time()
            return {
                "version": self._version,
                "server_time": now,
                "status": self.status,
                "status_detail": self.status_detail,
                "error": self.error,
                "demo_only": True,
                "account": dict(self.account) if self.account else None,
                "risk": dict(self.risk),
                "scanner": [dict(r) for r in self.scanner],
                "last_scan_at": self.last_scan_at,
                "open_contract": dict(self.open_contract)
                if self.open_contract else None,
                "open_contracts": [dict(c) for c in self.open_contracts],
                "trades": [dict(t) for t in self.trades]
                if include_trades else [],
                "backtest": dict(self.backtest) if self.backtest else None,
                "backtest_running": self.backtest_running,
                "uptime_seconds": int(now - self.started_at)
                if self.started_at else 0,
                "scans": self.scans,
                "signals_seen": self.signals_seen,
            }

    def risk_payload(self) -> Dict[str, Any]:
        """
        Derived risk numbers the UI shows as gauges.

        ``daily_loss_remaining`` is computed here rather than in the browser so
        the number the user reads is the number the engine enforces.
        """
        with self._lock:
            risk = dict(self.risk)
        limit = float(risk.get("daily_loss_limit") or 0.0)
        pnl = float(risk.get("realised_pnl") or 0.0)
        used = max(0.0, -pnl)
        risk["daily_loss_used"] = round(used, 2)
        risk["daily_loss_remaining"] = round(max(0.0, limit - used), 2)
        risk["daily_loss_pct"] = (
            round(min(100.0, used / limit * 100.0), 1) if limit > 0 else 0.0
        )
        target = float(risk.get("daily_profit_target") or 0.0)
        gained = max(0.0, pnl)
        risk["daily_profit_remaining"] = round(
            max(0.0, target - gained), 2
        )
        risk["daily_profit_pct"] = (
            round(min(100.0, gained / target * 100.0), 1)
            if target > 0 else 0.0
        )
        max_trades = int(risk.get("max_trades_per_day") or 0)
        done = int(risk.get("trades_today") or 0)
        risk["trades_pct"] = (
            round(min(100.0, done / max_trades * 100.0), 1)
            if max_trades > 0 else 0.0
        )
        return risk


def iso(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds -> ISO-8601 UTC string, for display."""
    if not ts:
        return None
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# A single process-wide instance; the Flask app and the bot thread share it.
STATE = BotState()
