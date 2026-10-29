"""Tests for risk_manager.py — limits, persistence across restart, no martingale."""

import os
import sys
import tempfile
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from storage import Storage, TradeRecord  # noqa: E402


def make_cfg(**over) -> Config:
    base = dict(
        app_id="t", api_token="t",
        stake=1.0,
        max_open_contracts=1,
        max_consecutive_losses=3,
        daily_loss_limit=5.0,
        cooldown_seconds=60,
        max_trades_per_day=20,
        loss_cooldown_minutes=60,
        trading_day_timezone="UTC",
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def store(tmp_path):
    s = Storage(str(tmp_path / "t.db"), str(tmp_path / "t.csv"), "UTC")
    yield s
    s.close()


def add_open_trade(store, cid=1):
    store.insert_trade(TradeRecord(
        contract_id=cid, symbol="R_100", index_value=100, direction="CALL",
        stake=1.0, duration=1, duration_unit="m", status="pending",
    ))


class TestDailyLossLimit:
    def test_allows_below_limit(self, store):
        rm = RiskManager(make_cfg(), store)
        store.add_realised_pnl(-4.0)
        assert rm.check_daily_loss().allowed

    def test_blocks_at_limit(self, store):
        rm = RiskManager(make_cfg(), store)
        store.add_realised_pnl(-5.0)
        d = rm.check_daily_loss()
        assert not d.allowed
        assert d.code == "daily_loss_limit"

    def test_blocks_beyond_limit(self, store):
        rm = RiskManager(make_cfg(), store)
        store.add_realised_pnl(-9.0)
        assert not rm.check_daily_loss().allowed

    def test_profit_does_not_block(self, store):
        rm = RiskManager(make_cfg(), store)
        store.add_realised_pnl(10.0)
        assert rm.check_daily_loss().allowed

    def test_can_trade_false_when_limit_hit(self, store):
        rm = RiskManager(make_cfg(), store)
        store.add_realised_pnl(-5.0)
        d = rm.can_trade(stake=1.0)
        assert not d.allowed
        assert d.code == "daily_loss_limit"


class TestConsecutiveLosses:
    def test_counts_up(self, store):
        rm = RiskManager(make_cfg(), store)
        rm.record_result(-1.0)
        assert store.get_consecutive_losses() == 1
        rm.record_result(-1.0)
        assert store.get_consecutive_losses() == 2

    def test_win_resets(self, store):
        rm = RiskManager(make_cfg(), store)
        rm.record_result(-1.0)
        rm.record_result(-1.0)
        rm.record_result(0.95)
        assert store.get_consecutive_losses() == 0

    def test_blocks_at_limit(self, store):
        rm = RiskManager(make_cfg(), store)
        for _ in range(3):
            rm.record_result(-1.0)
        d = rm.check_consecutive_losses()
        assert not d.allowed
        assert d.code == "consecutive_losses"

    def test_sets_pause_at_limit(self, store):
        rm = RiskManager(make_cfg(), store)
        for _ in range(3):
            rm.record_result(-1.0)
        assert store.get_paused_until() is not None
        assert not rm.check_pause().allowed

    def test_zero_cooldown_pauses_indefinitely(self, store):
        rm = RiskManager(make_cfg(loss_cooldown_minutes=0), store)
        for _ in range(3):
            rm.record_result(-1.0)
        paused = store.get_paused_until()
        assert paused is not None
        assert paused > store.now() + timedelta(days=365)

    def test_manual_reset(self, store):
        rm = RiskManager(make_cfg(), store)
        for _ in range(3):
            rm.record_result(-1.0)
        rm.manual_reset()
        assert store.get_consecutive_losses() == 0
        assert store.get_paused_until() is None
        assert rm.check_consecutive_losses().allowed


class TestOpenContracts:
    def test_blocks_when_one_open(self, store):
        rm = RiskManager(make_cfg(), store)
        add_open_trade(store)
        d = rm.check_open_contracts()
        assert not d.allowed
        assert d.code == "max_open_contracts"

    def test_allows_when_none_open(self, store):
        rm = RiskManager(make_cfg(), store)
        assert rm.check_open_contracts().allowed

    def test_closed_trade_does_not_count(self, store):
        rm = RiskManager(make_cfg(), store)
        add_open_trade(store, cid=1)
        store.update_trade_result(1, "won", profit=0.95)
        assert rm.check_open_contracts().allowed


class TestUnresolved:
    def test_blocks_when_unresolved(self, store):
        rm = RiskManager(make_cfg(), store)
        store.insert_trade(TradeRecord(
            contract_id=99, symbol="R_100", index_value=100,
            direction="CALL", stake=1.0, duration=1, duration_unit="m",
            status="unresolved", resolved=0,
        ))
        d = rm.check_unresolved()
        assert not d.allowed
        assert d.code == "unresolved_contracts"

    def test_unresolved_blocks_can_trade(self, store):
        rm = RiskManager(make_cfg(), store)
        store.insert_trade(TradeRecord(
            contract_id=99, symbol="R_100", index_value=100,
            direction="CALL", stake=1.0, duration=1, duration_unit="m",
            status="unresolved", resolved=0,
        ))
        assert not rm.can_trade(stake=1.0).allowed


class TestTradeCount:
    def test_blocks_at_max(self, store):
        rm = RiskManager(make_cfg(max_trades_per_day=2), store)
        store.increment_trade_count()
        store.increment_trade_count()
        d = rm.check_daily_trade_count()
        assert not d.allowed
        assert d.code == "max_trades_per_day"

    def test_allows_below_max(self, store):
        rm = RiskManager(make_cfg(max_trades_per_day=2), store)
        store.increment_trade_count()
        assert rm.check_daily_trade_count().allowed


class TestCooldown:
    def test_blocks_immediately_after_trade(self, store):
        rm = RiskManager(make_cfg(cooldown_seconds=60), store)
        store.set_last_trade_ts(store.now())
        d = rm.check_cooldown()
        assert not d.allowed
        assert d.code == "cooldown"

    def test_allows_after_cooldown(self, store):
        rm = RiskManager(make_cfg(cooldown_seconds=60), store)
        store.set_last_trade_ts(store.now() - timedelta(seconds=120))
        assert rm.check_cooldown().allowed

    def test_allows_when_never_traded(self, store):
        rm = RiskManager(make_cfg(), store)
        assert rm.check_cooldown().allowed


class TestStakeGuard:
    def test_exact_stake_allowed(self, store):
        rm = RiskManager(make_cfg(stake=1.0), store)
        assert rm.check_stake(1.0).allowed

    def test_escalated_stake_blocked(self, store):
        rm = RiskManager(make_cfg(stake=1.0), store)
        d = rm.check_stake(2.0)
        assert not d.allowed
        assert d.code == "stake_mismatch"

    def test_martingale_blocked_via_can_trade(self, store):
        rm = RiskManager(make_cfg(stake=1.0), store)
        assert not rm.can_trade(stake=4.0).allowed


class TestPersistenceAcrossRestart:
    def test_daily_pnl_survives_restart(self, tmp_path):
        db = str(tmp_path / "p.db")
        s1 = Storage(db, "", "UTC")
        rm1 = RiskManager(make_cfg(), s1)
        rm1.record_result(-3.0)
        rm1.record_result(-2.0)
        s1.close()

        # Simulate a restart: brand-new Storage on the same file.
        s2 = Storage(db, "", "UTC")
        rm2 = RiskManager(make_cfg(), s2)
        assert s2.get_daily_state()["realised_pnl"] == pytest.approx(-5.0)
        assert not rm2.check_daily_loss().allowed
        s2.close()

    def test_consecutive_losses_survive_restart(self, tmp_path):
        db = str(tmp_path / "p2.db")
        s1 = Storage(db, "", "UTC")
        rm1 = RiskManager(make_cfg(), s1)
        rm1.record_result(-1.0)
        rm1.record_result(-1.0)
        s1.close()

        s2 = Storage(db, "", "UTC")
        rm2 = RiskManager(make_cfg(), s2)
        assert s2.get_consecutive_losses() == 2
        s2.close()

    def test_trade_count_survives_restart(self, tmp_path):
        db = str(tmp_path / "p3.db")
        s1 = Storage(db, "", "UTC")
        s1.increment_trade_count()
        s1.increment_trade_count()
        s1.close()

        s2 = Storage(db, "", "UTC")
        assert s2.get_daily_state()["trades_count"] == 2
        s2.close()

    def test_pause_survives_restart(self, tmp_path):
        db = str(tmp_path / "p4.db")
        s1 = Storage(db, "", "UTC")
        rm1 = RiskManager(make_cfg(), s1)
        for _ in range(3):
            rm1.record_result(-1.0)
        s1.close()

        s2 = Storage(db, "", "UTC")
        rm2 = RiskManager(make_cfg(), s2)
        assert not rm2.check_pause().allowed
        s2.close()


class TestStatus:
    def test_status_shape(self, store):
        rm = RiskManager(make_cfg(), store)
        st = rm.status()
        for key in ("trading_day", "realised_pnl", "trades_today",
                    "trades_remaining", "consecutive_losses",
                    "open_contracts", "can_trade", "blocked_by"):
            assert key in st

    def test_trades_remaining_never_negative(self, store):
        rm = RiskManager(make_cfg(max_trades_per_day=1), store)
        store.increment_trade_count()
        store.increment_trade_count()
        assert rm.status()["trades_remaining"] == 0
