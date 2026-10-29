"""
Tests for trade execution, demo protection, API errors and unresolved handling.

All Deriv responses are mocked. No network call is made and no real-money
purchase is possible from this file.
"""

import os
import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from deriv_client import (  # noqa: E402
    ApiError,
    AuthError,
    DemoAccountRequired,
    DerivClient,
    DerivError,
    ProposalRejected,
    RateLimited,
    redact_url,
)
from risk_manager import RiskManager  # noqa: E402
from storage import Storage, TradeRecord  # noqa: E402
from strategy import Signal  # noqa: E402
from trade_executor import TradeExecutor  # noqa: E402
from trade_monitor import TradeMonitor  # noqa: E402


def make_cfg(**over) -> Config:
    base = dict(
        app_id="app", api_token="tok", stake=1.0, currency="USD",
        contract_type="CALL", duration=1, duration_unit="m",
        max_open_contracts=1, max_consecutive_losses=3,
        daily_loss_limit=5.0, cooldown_seconds=0, max_trades_per_day=20,
        loss_cooldown_minutes=60, trading_day_timezone="UTC",
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def store(tmp_path):
    s = Storage(str(tmp_path / "t.db"), "", "UTC")
    yield s
    s.close()


def good_proposal(**over):
    p = {
        "id": "e36109ca-2b45-492a-99f3-c387444fd694",
        "ask_price": 1.0,
        "payout": 1.95,
        "spot": 569.11,
        "date_start": 1793188898,
        "date_expiry": 1793188958,
        "longcode": "Win payout if ...",
    }
    p.update(over)
    return p


def good_receipt(**over):
    r = {
        "contract_id": 12345678,
        "buy_price": 1.0,
        "payout": 1.95,
        "balance_after": 9999.0,
        "purchase_time": 1793188898,
        "start_time": 1793188898,
        "transaction_id": 999,
        "longcode": "Win payout if ...",
        "shortcode": "CALL_R_100_1.00_...",
    }
    r.update(over)
    return r


def actionable_signal(direction="CALL"):
    return Signal(symbol="R_100", index_value=100, direction=direction,
                  score=85, reasons=["test"], candle_epoch=1793188800)


# ===========================================================================
# Demo-only protection
# ===========================================================================
class TestDemoProtection:
    def _client(self, accounts):
        c = DerivClient(make_cfg())
        c.list_accounts = MagicMock(return_value=accounts)
        return c

    def test_real_only_account_is_rejected(self):
        c = self._client([{
            "account_id": "CR123", "account_type": "real",
            "status": "active", "currency": "USD", "balance": 100.0,
        }])
        with pytest.raises(DemoAccountRequired) as exc:
            c.connect()
        assert "DEMO-ONLY" in str(exc.value)

    def test_unknown_account_type_is_rejected(self):
        c = self._client([{
            "account_id": "XX1", "account_type": "unknown",
            "status": "active", "currency": "USD", "balance": 1.0,
        }])
        with pytest.raises(DemoAccountRequired):
            c.connect()

    def test_empty_account_list_is_rejected(self):
        c = self._client([])
        with pytest.raises(AuthError):
            c.connect()

    def test_demo_account_is_accepted(self):
        c = self._client([{
            "account_id": "DOT90004580", "account_type": "demo",
            "status": "active", "currency": "USD", "balance": 10000.0,
        }])
        c.request_otp_url = MagicMock(
            return_value="wss://api.derivws.com/trading/v1/options/ws/demo?otp=abc"
        )
        c._open_socket = MagicMock()
        info = c.connect()
        assert info["account_type"] == "demo"
        assert info["account_id"] == "DOT90004580"

    def test_demo_preferred_when_both_present(self):
        c = self._client([
            {"account_id": "CR1", "account_type": "real", "status": "active",
             "currency": "USD", "balance": 500.0},
            {"account_id": "DOT1", "account_type": "demo", "status": "active",
             "currency": "USD", "balance": 10000.0},
        ])
        c.request_otp_url = MagicMock(
            return_value="wss://api.derivws.com/trading/v1/options/ws/demo?otp=x"
        )
        c._open_socket = MagicMock()
        info = c.connect()
        assert info["account_id"] == "DOT1"

    def test_otp_url_pointing_at_real_is_rejected(self):
        c = self._client([{
            "account_id": "DOT1", "account_type": "demo", "status": "active",
            "currency": "USD", "balance": 10000.0,
        }])
        c.request_otp_url = MagicMock(
            return_value="wss://api.derivws.com/trading/v1/options/ws/real?otp=x"
        )
        c._open_socket = MagicMock()
        with pytest.raises(DemoAccountRequired):
            c.connect()


# ===========================================================================
# Proposal validation — a bad proposal must never result in a purchase
# ===========================================================================
class TestProposalValidation:
    def _exec(self, store, client=None):
        cfg = make_cfg()
        client = client or MagicMock()
        return TradeExecutor(client, cfg, RiskManager(cfg, store), store), client

    def test_valid_proposal_passes(self, store):
        ex, _ = self._exec(store)
        ok, reason = ex.validate_proposal(good_proposal(), "R_100", "CALL")
        assert ok, reason

    def test_missing_id_fails(self, store):
        ex, _ = self._exec(store)
        ok, _ = ex.validate_proposal(good_proposal(id=None), "R_100", "CALL")
        assert not ok

    def test_stake_mismatch_fails(self, store):
        ex, _ = self._exec(store)
        ok, reason = ex.validate_proposal(
            good_proposal(ask_price=5.0), "R_100", "CALL")
        assert not ok
        assert "stake" in reason.lower()

    def test_payout_below_stake_fails(self, store):
        ex, _ = self._exec(store)
        ok, reason = ex.validate_proposal(
            good_proposal(payout=0.5), "R_100", "CALL")
        assert not ok
        assert "payout" in reason.lower()

    def test_wrong_duration_fails(self, store):
        ex, _ = self._exec(store)
        ok, reason = ex.validate_proposal(
            good_proposal(date_expiry=1793188898 + 300), "R_100", "CALL")
        assert not ok
        assert "duration" in reason.lower()

    def test_wrong_currency_fails(self, store):
        ex, _ = self._exec(store)
        ok, _ = ex.validate_proposal(
            good_proposal(currency="EUR"), "R_100", "CALL")
        assert not ok

    def test_rejected_proposal_never_buys(self, store):
        ex, client = self._exec(store)
        client.proposal.side_effect = ProposalRejected("ContractBuyValidationError")
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        res = ex.execute(actionable_signal(), "R_100")
        assert not res.ok
        client.buy_contract.assert_not_called()

    def test_invalid_proposal_never_buys(self, store):
        ex, client = self._exec(store)
        client.proposal.return_value = good_proposal(ask_price=99.0)
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        res = ex.execute(actionable_signal(), "R_100")
        assert not res.ok
        client.buy_contract.assert_not_called()

    def test_contract_type_not_offered_never_buys(self, store):
        ex, client = self._exec(store)
        client.contracts_for.return_value = [{"contract_type": "PUT"}]
        res = ex.execute(actionable_signal("CALL"), "R_100")
        assert not res.ok
        client.buy_contract.assert_not_called()

    def test_risk_block_never_buys(self, store):
        ex, client = self._exec(store)
        store.add_realised_pnl(-5.0)  # daily limit hit
        res = ex.execute(actionable_signal(), "R_100")
        assert not res.ok
        client.buy_contract.assert_not_called()

    def test_non_actionable_signal_never_buys(self, store):
        ex, client = self._exec(store)
        sig = Signal("R_100", 100, "NONE", 10, ["no"])
        res = ex.execute(sig, "R_100")
        assert not res.ok
        client.buy_contract.assert_not_called()


# ===========================================================================
# Successful execution
# ===========================================================================
class TestExecution:
    def test_successful_buy_records_trade(self, store):
        cfg = make_cfg()
        client = MagicMock()
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        client.proposal.return_value = good_proposal()
        client.buy_contract.return_value = good_receipt()
        ex = TradeExecutor(client, cfg, RiskManager(cfg, store), store)

        res = ex.execute(actionable_signal(), "R_100")
        assert res.ok
        assert res.contract_id == 12345678
        assert store.count_open_trades() == 1
        assert store.get_daily_state()["trades_count"] == 1

    def test_buy_failure_records_nothing(self, store):
        cfg = make_cfg()
        client = MagicMock()
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        client.proposal.return_value = good_proposal()
        client.buy_contract.side_effect = DerivError("network down")
        ex = TradeExecutor(client, cfg, RiskManager(cfg, store), store)

        res = ex.execute(actionable_signal(), "R_100")
        assert not res.ok
        assert store.count_open_trades() == 0
        assert store.get_daily_state()["trades_count"] == 0

    def test_receipt_without_contract_id_fails(self, store):
        cfg = make_cfg()
        client = MagicMock()
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        client.proposal.return_value = good_proposal()
        client.buy_contract.return_value = {"buy_price": 1.0}
        ex = TradeExecutor(client, cfg, RiskManager(cfg, store), store)

        res = ex.execute(actionable_signal(), "R_100")
        assert not res.ok
        assert store.count_open_trades() == 0


# ===========================================================================
# Buy timeout — must not double-buy
# ===========================================================================
class TestBuyTimeout:
    def test_timeout_with_contract_found_returns_it(self):
        c = DerivClient(make_cfg())
        c._connected = True
        c._ws_conn = MagicMock()
        c._send = MagicMock(side_effect=DerivError("Timed out after 30s"))
        c.portfolio = MagicMock(return_value=[
            {"contract_id": 555, "buy_price": 1.0}
        ])
        receipt = c.buy_contract("prop-id", 1.0)
        assert receipt["contract_id"] == 555
        # Exactly one send attempt — no retry.
        assert c._send.call_count == 1

    def test_timeout_without_contract_raises(self):
        c = DerivClient(make_cfg())
        c._connected = True
        c._ws_conn = MagicMock()
        c._send = MagicMock(side_effect=DerivError("Timed out after 30s"))
        c.portfolio = MagicMock(return_value=[])
        with pytest.raises(DerivError) as exc:
            c.buy_contract("prop-id", 1.0)
        assert "NOT purchased" in str(exc.value)
        assert c._send.call_count == 1


# ===========================================================================
# API errors
# ===========================================================================
class TestApiErrors:
    def test_api_error_carries_code(self):
        e = ApiError("ContractBuyValidationError", "bad", {"field": "amount"})
        assert e.code == "ContractBuyValidationError"
        assert e.details["field"] == "amount"

    def test_rate_limit_is_distinct(self):
        assert issubclass(RateLimited, DerivError)

    def test_redact_url(self):
        u = "wss://api.derivws.com/trading/v1/options/ws/demo?otp=SECRET123"
        r = redact_url(u)
        assert "SECRET123" not in r
        assert "***REDACTED***" in r

    def test_redact_url_without_otp(self):
        u = "wss://api.derivws.com/trading/v1/options/ws/public"
        assert redact_url(u) == u

    def test_rest_401_raises_auth_error(self):
        c = DerivClient(make_cfg())
        resp = MagicMock()
        resp.status_code = 401
        c._http = MagicMock()
        c._http.request.return_value = resp
        with pytest.raises(AuthError):
            c.list_accounts()

    def test_rest_429_raises_rate_limited(self):
        c = DerivClient(make_cfg())
        resp = MagicMock()
        resp.status_code = 429
        c._http = MagicMock()
        c._http.request.return_value = resp
        with pytest.raises(RateLimited):
            c.list_accounts()

    def test_send_when_disconnected_raises(self):
        c = DerivClient(make_cfg())
        c._connected = False
        with pytest.raises(DerivError):
            c._send({"ping": 1})


# ===========================================================================
# Unresolved contract handling
# ===========================================================================
class TestUnresolvedContracts:
    def _monitor(self, store, client):
        cfg = make_cfg()
        return TradeMonitor(client, cfg, RiskManager(cfg, store), store)

    def test_terminal_win_is_recorded(self, store):
        client = MagicMock()
        client.open_contract.return_value = {
            "status": "won", "profit": "0.95", "payout": "1.95",
            "exit_tick": "570.00",
        }
        store.insert_trade(TradeRecord(
            contract_id=1, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m", status="pending"))
        m = self._monitor(store, client)
        res = m.wait_for_result(1, timeout=5, poll_interval=0.01)
        assert res["resolved"] is True
        assert res["status"] == "won"
        assert res["profit"] == pytest.approx(0.95)
        assert store.get_daily_state()["realised_pnl"] == pytest.approx(0.95)

    def test_terminal_loss_updates_streak(self, store):
        client = MagicMock()
        client.open_contract.return_value = {
            "status": "lost", "profit": "-1.00", "payout": "0",
        }
        store.insert_trade(TradeRecord(
            contract_id=2, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m", status="pending"))
        m = self._monitor(store, client)
        m.wait_for_result(2, timeout=5, poll_interval=0.01)
        assert store.get_consecutive_losses() == 1
        assert store.get_daily_state()["realised_pnl"] == pytest.approx(-1.0)

    def test_timeout_marks_unresolved_not_guessed(self, store):
        client = MagicMock()
        client.open_contract.return_value = {"status": "open", "profit": "0"}
        store.insert_trade(TradeRecord(
            contract_id=3, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m", status="pending"))
        m = self._monitor(store, client)
        res = m.wait_for_result(3, timeout=0.2, poll_interval=0.05)
        assert res["resolved"] is False
        assert res["status"] == "unresolved"
        assert res["profit"] is None
        # No P/L was invented.
        assert store.get_daily_state()["realised_pnl"] == 0.0

    def test_unresolved_blocks_new_trades(self, store):
        client = MagicMock()
        client.open_contract.return_value = {"status": "open"}
        store.insert_trade(TradeRecord(
            contract_id=4, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m", status="pending"))
        m = self._monitor(store, client)
        m.wait_for_result(4, timeout=0.2, poll_interval=0.05)
        rm = RiskManager(make_cfg(), store)
        assert not rm.can_trade(stake=1.0).allowed

    def test_terminal_without_profit_is_unresolved(self, store):
        client = MagicMock()
        client.open_contract.return_value = {"status": "won"}
        store.insert_trade(TradeRecord(
            contract_id=5, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m", status="pending"))
        m = self._monitor(store, client)
        res = m.wait_for_result(5, timeout=0.2, poll_interval=0.05)
        assert res["resolved"] is False

    def test_reconcile_resolves_previous_unresolved(self, store):
        client = MagicMock()
        client.open_contract.return_value = {
            "status": "won", "profit": "0.95", "payout": "1.95",
        }
        store.insert_trade(TradeRecord(
            contract_id=6, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m",
            status="unresolved", resolved=0))
        m = self._monitor(store, client)
        n = m.reconcile()
        assert n == 1
        assert store.get_trade(6)["status"] == "won"

    def test_reconcile_leaves_genuinely_open_pending(self, store):
        client = MagicMock()
        client.open_contract.return_value = {"status": "open"}
        store.insert_trade(TradeRecord(
            contract_id=7, symbol="R_100", index_value=100, direction="CALL",
            stake=1.0, duration=1, duration_unit="m",
            status="unresolved", resolved=0))
        m = self._monitor(store, client)
        m.reconcile()
        assert store.get_trade(7)["status"] == "pending"


# ===========================================================================
# Duplicate-entry prevention
# ===========================================================================
class TestDuplicatePrevention:
    def test_second_trade_blocked_while_one_open(self, store):
        cfg = make_cfg()
        client = MagicMock()
        client.contracts_for.return_value = [{"contract_type": "CALL"}]
        client.proposal.return_value = good_proposal()
        client.buy_contract.return_value = good_receipt()
        ex = TradeExecutor(client, cfg, RiskManager(cfg, store), store)

        r1 = ex.execute(actionable_signal(), "R_100")
        assert r1.ok
        r2 = ex.execute(actionable_signal(), "R_100")
        assert not r2.ok
        assert "risk" in r2.reason
        assert client.buy_contract.call_count == 1
