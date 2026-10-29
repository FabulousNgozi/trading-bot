"""
trade_monitor.py — follow a contract to its final state and record the result.

Rules
-----
* A contract is only considered finished when the API reports a terminal
  status: ``won``, ``lost``, ``sold`` or ``cancelled``. ``open`` is never
  treated as a result.
* If the outcome cannot be determined (connection lost, repeated errors), the
  trade is marked **unresolved** — never guessed. The risk manager then
  blocks new trades until it is reconciled.
* The open-contract slot is released only on a confirmed final state.
* P/L is taken from the API's ``profit`` field, not recomputed locally, so
  the bot's books match the broker's.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from config import Config
from deriv_client import DerivClient, DerivError
from logger_config import get_logger
from risk_manager import RiskManager
from storage import Storage

log = get_logger("trade_monitor")

TERMINAL_STATUSES = {"won", "lost", "sold", "cancelled"}


class TradeMonitor:
    def __init__(self, client: DerivClient, cfg: Config,
                 risk: RiskManager, storage: Storage) -> None:
        self.client = client
        self.cfg = cfg
        self.risk = risk
        self.storage = storage
        self._poll_errors: Dict[int, int] = {}

    def poll_result(self, contract_id: int) -> Dict[str, Any]:
        """Check one open contract once; suitable for a concurrent scan loop."""
        cid = int(contract_id)
        try:
            poc = self.client.open_contract(cid)
        except DerivError as exc:
            errors = self._poll_errors.get(cid, 0) + 1
            self._poll_errors[cid] = errors
            log.warning("Could not read contract %s (%d/5): %s", cid, errors, exc)
            if errors >= 5:
                self._poll_errors.pop(cid, None)
                return self._mark_unresolved(cid, f"repeated read failures: {exc}")
            return {"status": "open", "profit": None, "resolved": False}

        self._poll_errors.pop(cid, None)
        status = str(poc.get("status") or "").lower()
        if status in TERMINAL_STATUSES:
            return self._record_final(cid, poc)
        return {"status": status or "open", "profit": None, "resolved": False}

    # ------------------------------------------------------------------
    def wait_for_result(self, contract_id: int,
                        timeout: Optional[float] = None,
                        poll_interval: float = 2.0,
                        on_update: Optional[Callable[[Dict[str, Any]], None]] = None
                        ) -> Dict[str, Any]:
        """
        Poll a contract until it reaches a terminal state.

        Returns a dict with keys: status, profit, payout, exit_spot,
        resolved. On timeout or repeated failure the status is
        ``unresolved`` and ``resolved`` is False.
        """
        # Duration in seconds + generous buffer for settlement.
        if timeout is None:
            unit_secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "t": 2}
            base = self.cfg.duration * unit_secs.get(self.cfg.duration_unit, 60)
            timeout = base + 90

        deadline = time.time() + timeout
        consecutive_errors = 0
        last_state: Dict[str, Any] = {}

        while time.time() < deadline:
            try:
                poc = self.client.open_contract(contract_id)
                consecutive_errors = 0
            except DerivError as exc:
                consecutive_errors += 1
                log.warning(
                    "Could not read contract %s (%d/5): %s",
                    contract_id, consecutive_errors, exc,
                )
                if consecutive_errors >= 5:
                    return self._mark_unresolved(
                        contract_id, f"repeated read failures: {exc}"
                    )
                time.sleep(poll_interval * 2)
                continue

            status = str(poc.get("status") or "").lower()
            last_state = poc

            if on_update:
                try:
                    on_update(poc)
                except Exception:
                    pass

            if status in TERMINAL_STATUSES:
                return self._record_final(contract_id, poc)

            time.sleep(poll_interval)

        return self._mark_unresolved(
            contract_id, f"no final state within {timeout:.0f}s"
        )

    # ------------------------------------------------------------------
    def _record_final(self, contract_id: int, poc: Dict[str, Any]) -> Dict[str, Any]:
        status = str(poc.get("status") or "").lower()
        profit = self._to_float(poc.get("profit"))
        payout = self._to_float(poc.get("payout"))
        exit_spot = self._to_float(poc.get("exit_tick") or poc.get("sell_spot"))

        if profit is None:
            # A terminal status with no profit is contradictory. Do not
            # invent a number — mark it unresolved for reconciliation.
            return self._mark_unresolved(
                contract_id, f"terminal status {status!r} but no profit field"
            )

        self.storage.update_trade_result(
            contract_id, status=status, profit=profit,
            payout=payout, exit_spot=exit_spot, resolved=True,
        )
        self.risk.record_result(profit)

        state = self.risk.status()
        log.info(
            "Contract %s %s  P/L=%+.2f  day P/L=%+.2f  "
            "streak=%d  trades left=%d",
            contract_id, status.upper(), profit,
            state["realised_pnl"], state["consecutive_losses"],
            state["trades_remaining"],
        )
        if not state["can_trade"]:
            log.warning("Trading halted: %s", state["blocked_by"])

        return {
            "status": status,
            "profit": profit,
            "payout": payout,
            "exit_spot": exit_spot,
            "resolved": True,
        }

    # ------------------------------------------------------------------
    def _mark_unresolved(self, contract_id: int, reason: str) -> Dict[str, Any]:
        log.error(
            "Contract %s UNRESOLVED: %s — new trades are blocked until this "
            "is reconciled", contract_id, reason,
        )
        self.storage.update_trade_result(
            contract_id, status="unresolved", resolved=False
        )
        self.storage.log_event("ERROR", "contract_unresolved",
                               f"{contract_id}: {reason}")
        return {
            "status": "unresolved",
            "profit": None,
            "payout": None,
            "exit_spot": None,
            "resolved": False,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    def reconcile(self) -> int:
        """
        Try to resolve previously-unresolved contracts.

        Called at startup and before any new trade. Returns the number of
        contracts successfully resolved.
        """
        unresolved = self.storage.get_unresolved_trades()
        if not unresolved:
            return 0

        log.info("Reconciling %d unresolved contract(s)", len(unresolved))
        resolved_count = 0
        for trade in unresolved:
            cid = trade.get("contract_id")
            if cid is None:
                continue
            try:
                poc = self.client.open_contract(int(cid))
            except DerivError as exc:
                log.warning("Reconcile failed for %s: %s", cid, exc)
                continue
            status = str(poc.get("status") or "").lower()
            if status in TERMINAL_STATUSES:
                self._record_final(int(cid), poc)
                resolved_count += 1
            elif status == "open":
                # Still genuinely open — leave it pending, not unresolved.
                self.storage.update_trade_result(
                    int(cid), status="pending", resolved=False
                )
        return resolved_count

    # ------------------------------------------------------------------
    @staticmethod
    def _to_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
