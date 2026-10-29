"""
trade_executor.py — proposal validation and purchase.

The executor is the only place a purchase can originate. It enforces a strict
sequence and refuses to buy unless every check passes:

    1. risk manager says yes
    2. contract type is actually offered for this symbol
    3. a proposal is obtained for the configured stake/duration/currency
    4. the proposal is validated against the config
    5. only then: buy

If the proposal is rejected, or the stake is not permitted, the trade is
abandoned. The stake is **never** silently increased to make a proposal work —
that is the single most common way a "safe" bot becomes an unsafe one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import Config
from deriv_client import (
    ApiError,
    DerivClient,
    DerivError,
    ProposalRejected,
    RateLimited,
)
from logger_config import get_logger
from risk_manager import RiskManager
from storage import Storage, TradeRecord
from strategy import Signal

log = get_logger("trade_executor")


@dataclass
class ExecutionResult:
    ok: bool
    reason: str = ""
    contract_id: Optional[int] = None
    receipt: Optional[Dict[str, Any]] = None
    proposal: Optional[Dict[str, Any]] = None


class TradeExecutor:
    def __init__(self, client: DerivClient, cfg: Config,
                 risk: RiskManager, storage: Storage) -> None:
        self.client = client
        self.cfg = cfg
        self.risk = risk
        self.storage = storage
        self._contracts_cache: Dict[str, set] = {}

    # ------------------------------------------------------------------
    def available_contract_types(self, symbol: str) -> set:
        """Contract types offered for a symbol (cached per run)."""
        if symbol in self._contracts_cache:
            return self._contracts_cache[symbol]
        try:
            available = self.client.contracts_for(symbol)
        except Exception as exc:
            log.warning("contracts_for(%s) failed: %s", symbol, exc)
            return set()
        types = {c.get("contract_type") for c in available
                 if c.get("contract_type")}
        self._contracts_cache[symbol] = types
        return types

    # ------------------------------------------------------------------
    def validate_proposal(self, prop: Dict[str, Any], symbol: str,
                          contract_type: str) -> tuple:
        """
        Check a proposal against the configuration.

        Returns (ok, reason). Any mismatch aborts the trade — we do not
        "fix" the stake to match the broker.
        """
        if not prop.get("id"):
            return False, "proposal has no id"

        ask = prop.get("ask_price")
        if ask is None:
            return False, "proposal has no ask_price"
        # The ask price is what we actually pay. It must equal the stake.
        if abs(float(ask) - self.cfg.stake) > 0.01:
            return False, (
                f"proposal ask_price {ask} != configured stake "
                f"{self.cfg.stake} — refusing to trade at a different size"
            )

        payout = prop.get("payout")
        if payout is None or float(payout) <= 0:
            return False, "proposal has no valid payout"
        if float(payout) <= float(ask):
            return False, (
                f"payout {payout} <= stake {ask} — negative expected value "
                f"at this price, refusing"
            )

        # Duration check: date_expiry - date_start should match the config.
        ds, de = prop.get("date_start"), prop.get("date_expiry")
        if ds and de:
            expected = self.cfg.duration * {
                "s": 1, "m": 60, "h": 3600, "d": 86400
            }.get(self.cfg.duration_unit, 60)
            actual = int(de) - int(ds)
            # Allow a small tolerance for server-side rounding.
            if abs(actual - expected) > max(5, expected * 0.05):
                return False, (
                    f"proposal duration {actual}s != configured "
                    f"{expected}s"
                )

        # Currency check where the API exposes it.
        cur = prop.get("currency")
        if cur and str(cur).upper() != self.cfg.currency.upper():
            return False, f"proposal currency {cur} != {self.cfg.currency}"

        return True, "proposal valid"

    # ------------------------------------------------------------------
    def execute(self, signal: Signal, symbol: str,
                same_scan_batch: bool = False) -> ExecutionResult:
        """Run the full gate and, if everything passes, buy."""
        if not signal.actionable:
            return ExecutionResult(False, "signal is not actionable")
        contract_type = signal.direction
        if contract_type not in ("CALL", "PUT"):
            return ExecutionResult(False, f"unsupported signal direction: {contract_type}")

        # 1. Risk gate.
        decision = self.risk.can_trade(
            stake=self.cfg.stake, same_scan_batch=same_scan_batch
        )
        if not decision.allowed:
            log.info("Trade blocked by risk: %s", decision.reason)
            return ExecutionResult(False, f"risk: {decision.reason}")

        # 2. Contract type must be offered.
        types = self.available_contract_types(symbol)
        if not types:
            return ExecutionResult(
                False, f"could not confirm contract types offered for {symbol}"
            )
        if contract_type not in types:
            return ExecutionResult(
                False,
                f"contract type {contract_type} not offered for "
                f"{symbol} (available: {sorted(types)[:8]})",
            )

        # 3. Proposal.
        try:
            prop = self.client.proposal(
                symbol=symbol,
                contract_type=contract_type,
                stake=self.cfg.stake,
                duration=self.cfg.duration,
                duration_unit=self.cfg.duration_unit,
            )
        except ProposalRejected as exc:
            log.warning("Proposal rejected for %s: %s", symbol, exc)
            return ExecutionResult(False, f"proposal rejected: {exc}")
        except RateLimited as exc:
            log.warning("Rate limited while requesting proposal: %s", exc)
            return ExecutionResult(False, f"rate limited: {exc}")
        except DerivError as exc:
            log.warning("Proposal error for %s: %s", symbol, exc)
            return ExecutionResult(False, f"proposal error: {exc}")

        # 4. Validate.
        ok, reason = self.validate_proposal(
            prop, symbol, contract_type
        )
        if not ok:
            log.warning("Proposal failed validation for %s: %s", symbol, reason)
            return ExecutionResult(False, f"invalid proposal: {reason}",
                                   proposal=prop)

        # 5. Buy.
        proposal_id = prop["id"]
        ask = float(prop["ask_price"])
        log.info(
            "Buying %s %s stake=%.2f payout=%.2f duration=%d%s",
            symbol, contract_type, ask, float(prop["payout"]),
            self.cfg.duration, self.cfg.duration_unit,
        )
        try:
            receipt = self.client.buy_contract(proposal_id, ask)
        except DerivError as exc:
            log.error("Buy failed for %s: %s", symbol, exc)
            return ExecutionResult(False, f"buy failed: {exc}", proposal=prop)

        contract_id = receipt.get("contract_id")
        if contract_id is None:
            return ExecutionResult(
                False, "buy receipt had no contract_id", proposal=prop
            )

        # 6. Persist.
        rec = TradeRecord(
            contract_id=int(contract_id),
            symbol=symbol,
            index_value=signal.index_value,
            direction=contract_type,
            stake=float(receipt.get("buy_price", ask)),
            duration=self.cfg.duration,
            duration_unit=self.cfg.duration_unit,
            status="pending",
            payout=float(receipt.get("payout", prop.get("payout", 0))),
            buy_price=float(receipt.get("buy_price", ask)),
            entry_spot=float(prop.get("spot")) if prop.get("spot") else None,
            signal_score=signal.score,
            signal_reasons=" | ".join(signal.reasons),
        )
        self.storage.insert_trade(rec)
        self.storage.append_csv(rec)
        self.risk.record_entry(self.cfg.stake)

        log.info(
            "Contract opened: id=%s %s %s stake=%.2f payout=%.2f",
            contract_id, symbol, contract_type,
            rec.stake, rec.payout or 0,
        )
        return ExecutionResult(True, "purchased", contract_id=int(contract_id),
                               receipt=receipt, proposal=prop)
