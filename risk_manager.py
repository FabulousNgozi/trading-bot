"""
risk_manager.py — the gate every trade must pass.

This module is deliberately the most conservative part of the bot. It answers
one question: *may a new contract be opened right now?* It never opens one.

All state is read from :class:`storage.Storage`, so a restart cannot reset a
limit. The manager holds no authoritative state of its own — it is a pure
function of persisted state plus the current clock, which makes it trivially
testable and impossible to "forget" a loss.

What this does NOT do
---------------------
It does not eliminate risk. It caps the damage from a bad day and from a
losing streak. A gap, a broker outage, or a strategy that is simply wrong can
still lose money up to the configured limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from config import Config
from storage import Storage


@dataclass
class RiskDecision:
    """Outcome of a risk check."""
    allowed: bool
    reason: str = ""
    # Machine-readable code so callers/tests can assert on the cause.
    code: str = "ok"

    def __bool__(self) -> bool:
        return self.allowed


class RiskManager:
    def __init__(self, cfg: Config, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    def check_open_contracts(self) -> RiskDecision:
        open_count = self.storage.count_open_trades()
        if open_count >= self.cfg.max_open_contracts:
            return RiskDecision(
                False,
                f"{open_count} contract(s) already open "
                f"(max {self.cfg.max_open_contracts})",
                "max_open_contracts",
            )
        return RiskDecision(True)

    def check_unresolved(self) -> RiskDecision:
        """
        Refuse to trade while any contract's outcome is unknown.

        An unresolved contract means we do not know our real exposure or our
        real P/L. Trading on top of that is how a bot doubles a loss it never
        recorded.
        """
        unresolved = self.storage.get_unresolved_trades()
        if unresolved:
            ids = ", ".join(str(t.get("contract_id")) for t in unresolved[:5])
            return RiskDecision(
                False,
                f"{len(unresolved)} unresolved contract(s) [{ids}] — "
                f"reconcile before trading",
                "unresolved_contracts",
            )
        return RiskDecision(True)

    def check_daily_loss(self) -> RiskDecision:
        state = self.storage.get_daily_state()
        pnl = state["realised_pnl"]
        if pnl <= -abs(self.cfg.daily_loss_limit):
            return RiskDecision(
                False,
                f"daily loss limit reached: {pnl:.2f} "
                f"(limit -{self.cfg.daily_loss_limit:.2f})",
                "daily_loss_limit",
            )
        return RiskDecision(True)

    def check_daily_profit_target(self) -> RiskDecision:
        target = self.cfg.daily_profit_target
        if target <= 0:
            return RiskDecision(True)
        pnl = self.storage.get_daily_state()["realised_pnl"]
        if pnl >= target:
            return RiskDecision(
                False,
                f"daily take-profit target reached: {pnl:.2f} "
                f"(target +{target:.2f})",
                "daily_profit_target",
            )
        return RiskDecision(True)

    def check_daily_trade_count(self) -> RiskDecision:
        if self.cfg.max_trades_per_day <= 0:
            return RiskDecision(True)
        state = self.storage.get_daily_state()
        if state["trades_count"] >= self.cfg.max_trades_per_day:
            return RiskDecision(
                False,
                f"daily trade limit reached: {state['trades_count']}"
                f"/{self.cfg.max_trades_per_day}",
                "max_trades_per_day",
            )
        return RiskDecision(True)

    def check_consecutive_losses(self) -> RiskDecision:
        losses = self.storage.get_consecutive_losses()
        if losses >= self.cfg.max_consecutive_losses:
            return RiskDecision(
                False,
                f"{losses} consecutive losses "
                f"(limit {self.cfg.max_consecutive_losses})",
                "consecutive_losses",
            )
        return RiskDecision(True)

    def check_pause(self) -> RiskDecision:
        paused_until = self.storage.get_paused_until()
        if paused_until is None:
            return RiskDecision(True)
        now = self.storage.now()
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=now.tzinfo)
        if now < paused_until:
            remaining = int((paused_until - now).total_seconds())
            return RiskDecision(
                False,
                f"paused for another {remaining}s "
                f"(until {paused_until.isoformat(timespec='seconds')})",
                "paused",
            )
        return RiskDecision(True)

    def check_cooldown(self) -> RiskDecision:
        last = self.storage.get_last_trade_ts()
        if last is None:
            return RiskDecision(True)
        now = self.storage.now()
        if last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        elapsed = (now - last).total_seconds()
        if elapsed < self.cfg.cooldown_seconds:
            remaining = int(self.cfg.cooldown_seconds - elapsed)
            return RiskDecision(
                False,
                f"cooldown: {remaining}s remaining",
                "cooldown",
            )
        return RiskDecision(True)

    def check_stake(self, stake: float) -> RiskDecision:
        """
        The stake must equal the configured fixed stake.

        This is the anti-martingale guard: no code path may pass a larger
        stake, and there is no "recovery" multiplier anywhere in the bot.
        """
        if abs(stake - self.cfg.stake) > 1e-9:
            return RiskDecision(
                False,
                f"stake {stake} does not match configured fixed stake "
                f"{self.cfg.stake} — stake escalation is not permitted",
                "stake_mismatch",
            )
        return RiskDecision(True)

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    def can_trade(self, stake: Optional[float] = None,
                  same_scan_batch: bool = False) -> RiskDecision:
        """
        Run every check in a fixed order and return the first failure.

        Order matters for the log: the most fundamental blocker is reported
        first so the operator sees the real reason, not a downstream symptom.
        """
        checks = [
            self.check_unresolved,
            self.check_open_contracts,
            self.check_pause,
            self.check_consecutive_losses,
            self.check_daily_loss,
            self.check_daily_profit_target,
            self.check_daily_trade_count,
        ]
        for check in checks:
            d = check()
            if not d.allowed:
                return d
        # Additional contracts from one ranked scan are a single entry batch;
        # the open-contract cap still limits the aggregate exposure.
        if not same_scan_batch:
            d = self.check_cooldown()
            if not d.allowed:
                return d
        if stake is not None:
            d = self.check_stake(stake)
            if not d.allowed:
                return d
        return RiskDecision(True, "all risk checks passed", "ok")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def record_entry(self, stake: float) -> None:
        """Call immediately after a purchase is confirmed."""
        now = self.storage.now()
        self.storage.increment_trade_count()
        self.storage.set_last_trade_ts(now)

    def record_result(self, profit: float) -> None:
        """
        Call once a contract reaches a final state.

        Updates realised P/L, the consecutive-loss counter, and — if the
        streak limit is hit — sets the pause window.
        """
        self.storage.add_realised_pnl(profit)

        if profit < 0:
            losses = self.storage.get_consecutive_losses() + 1
            self.storage.set_consecutive_losses(losses)
            if losses >= self.cfg.max_consecutive_losses:
                if self.cfg.loss_cooldown_minutes > 0:
                    until = self.storage.now() + timedelta(
                        minutes=self.cfg.loss_cooldown_minutes
                    )
                    self.storage.set_paused_until(until)
                else:
                    # 0 means "require a manual reset": pause indefinitely.
                    self.storage.set_paused_until(
                        self.storage.now() + timedelta(days=3650)
                    )
        elif profit > 0:
            self.storage.set_consecutive_losses(0)
        # profit == 0 (a push) leaves the streak unchanged.

    def manual_reset(self) -> None:
        """Clear the loss streak and any pause. Operator action only."""
        self.storage.set_consecutive_losses(0)
        self.storage.set_paused_until(None)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> dict:
        state = self.storage.get_daily_state()
        paused_until = self.storage.get_paused_until()
        return {
            "trading_day": state["trading_day"],
            "realised_pnl": round(state["realised_pnl"], 2),
            "trades_today": state["trades_count"],
            "trades_remaining": (
                max(0, self.cfg.max_trades_per_day - state["trades_count"])
                if self.cfg.max_trades_per_day > 0 else None
            ),
            "consecutive_losses": self.storage.get_consecutive_losses(),
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "daily_loss_limit": self.cfg.daily_loss_limit,
            "daily_profit_target": self.cfg.daily_profit_target,
            "open_contracts": self.storage.count_open_trades(),
            "max_open_contracts": self.cfg.max_open_contracts,
            "paused_until": paused_until.isoformat() if paused_until else None,
            "can_trade": self.can_trade().allowed,
            "blocked_by": self.can_trade().code,
        }
