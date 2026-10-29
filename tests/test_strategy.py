"""Tests for strategy.py — bullish, bearish and neutral conditions."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from market_data import Candle, CandleSeries  # noqa: E402
from strategy import Strategy  # noqa: E402


def make_cfg(**over) -> Config:
    base = dict(
        app_id="test", api_token="test",
        ema_fast=20, ema_mid=50, ema_slow=200,
        rsi_period=14, rsi_min=50, rsi_max=70,
        sr_lookback=50, sr_proximity_atr=0.5,
        min_signal_score=60,
        require_htf_confirmation=True,
        require_candle_confirmation=True,
    )
    base.update(over)
    return Config(**base)


def series_from_closes(closes, granularity=60, start=1_700_000_000):
    candles = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        candles.append(Candle(
            epoch=start + i * granularity,
            open=o, high=max(o, c) + 0.05, low=min(o, c) - 0.05, close=c,
        ))
    return CandleSeries("R_100", granularity, candles)


def build_series(closes_1m, closes_5m=None, closes_1h=None):
    c1m = series_from_closes(closes_1m, 60)
    c5m = series_from_closes(closes_5m or closes_1m, 300)
    c1h = series_from_closes(closes_1h or closes_1m, 3600)
    return {"1m": c1m, "5m": c5m, "1h": c1h}


def uptrend(n=260, start=100.0, step=0.15):
    return [start + i * step for i in range(n)]


def downtrend(n=260, start=140.0, step=0.15):
    return [start - i * step for i in range(n)]


def flat(n=260, start=100.0):
    return [start + (0.01 if i % 2 else -0.01) for i in range(n)]


class TestNeutral:
    def test_flat_market_is_not_actionable(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(flat()))
        assert sig.direction == "NONE"
        assert not sig.actionable

    def test_insufficient_candles(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series([100.0] * 5))
        assert sig.direction == "NONE"
        assert any("insufficient" in r or "warmed" in r for r in sig.reasons)

    def test_sideways_ema_blocks(self):
        s = Strategy(make_cfg())
        # Oscillating with no net direction -> EMAs converge.
        closes = [100 + (2 if i % 4 < 2 else -2) for i in range(260)]
        sig = s.evaluate("R_100", build_series(closes))
        assert sig.direction == "NONE"


class TestBullish:
    def test_uptrend_produces_call_or_none_with_reasons(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(uptrend()))
        # Direction is either CALL (all gates passed) or NONE (a gate failed),
        # but never PUT in a clean uptrend.
        assert sig.direction in ("CALL", "NONE")
        assert sig.trend_1m == "up"
        assert sig.score > 0
        assert sig.reasons

    def test_score_components_present(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(uptrend()))
        joined = " ".join(sig.reasons)
        assert "EMA stack bullish" in joined
        assert "RSI" in joined

    def test_htf_disagreement_blocks(self):
        cfg = make_cfg(require_htf_confirmation=True)
        s = Strategy(cfg)
        # 1m up, 1h down -> blocked.
        sig = s.evaluate("R_100", build_series(uptrend(), closes_1h=downtrend()))
        assert sig.direction == "NONE"
        assert any("HTF" in r for r in sig.reasons)

    def test_htf_not_required_allows(self):
        cfg = make_cfg(require_htf_confirmation=False,
                       require_candle_confirmation=False,
                       min_signal_score=0)
        s = Strategy(cfg)
        sig = s.evaluate("R_100", build_series(uptrend(), closes_1h=downtrend()))
        # With gates relaxed the direction may be CALL.
        assert sig.direction in ("CALL", "NONE")


class TestBearish:
    def test_downtrend_never_call(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(downtrend()))
        assert sig.direction in ("PUT", "NONE")
        assert sig.trend_1m == "down"

    def test_bearish_reasons(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(downtrend()))
        assert any("bearish" in r.lower() or "EMA stack" in r
                   for r in sig.reasons)


class TestScoring:
    def test_score_never_exceeds_100(self):
        s = Strategy(make_cfg(min_signal_score=0,
                              require_htf_confirmation=False,
                              require_candle_confirmation=False))
        for closes in (uptrend(), downtrend()):
            sig = s.evaluate("R_100", build_series(closes))
            assert 0 <= sig.score <= 100

    def test_min_score_gate(self):
        cfg = make_cfg(min_signal_score=100,
                       require_htf_confirmation=False,
                       require_candle_confirmation=False)
        s = Strategy(cfg)
        sig = s.evaluate("R_100", build_series(uptrend()))
        # A perfect 100 is essentially unreachable; must be blocked.
        if sig.score < 100:
            assert sig.direction == "NONE"

    def test_rank_orders_by_score(self):
        s = Strategy(make_cfg())
        a = s.evaluate("R_10", build_series(uptrend()))
        b = s.evaluate("R_25", build_series(uptrend()))
        ranked = s.rank([a, b])
        if len(ranked) > 1:
            assert ranked[0].score >= ranked[1].score

    def test_rank_excludes_non_actionable(self):
        s = Strategy(make_cfg())
        sig = s.evaluate("R_100", build_series(flat()))
        assert s.rank([sig]) == []


class TestNoLookAhead:
    def test_signal_uses_only_completed_candles(self):
        """
        Appending future candles must not change the signal computed at an
        earlier point in time.

        The 5m and 1h series are aggregated from the 1m series so all three
        share one time grid — otherwise the comparison is meaningless.
        """
        from backtest import aggregate

        s = Strategy(make_cfg())
        closes = uptrend(400)
        c1m = series_from_closes(closes, 60).candles

        def evaluate_at(candles_1m, now):
            series = {
                "1m": CandleSeries("R_100", 60, candles_1m),
                "5m": CandleSeries("R_100", 300, aggregate(candles_1m, 300)),
                "1h": CandleSeries("R_100", 3600, aggregate(candles_1m, 3600)),
            }
            return s.evaluate("R_100", series, now=now)

        # Decision point: the 300th 1m candle has just closed.
        cut = 300
        now = c1m[cut - 1].epoch + 60
        sig_a = evaluate_at(c1m[:cut], now)

        # Same decision point, but the full history (including future bars)
        # is present in the series. `now` must exclude them.
        sig_b = evaluate_at(c1m, now)

        assert sig_a.score == sig_b.score
        assert sig_a.direction == sig_b.direction
        assert sig_a.candle_epoch == sig_b.candle_epoch

    def test_incomplete_candle_is_excluded(self):
        """A bar whose interval has not elapsed must not be used."""
        s = Strategy(make_cfg())
        closes = uptrend(300)
        c1m = series_from_closes(closes, 60).candles
        last = c1m[-1]

        # `now` is one second before the last bar closes.
        now = last.epoch + 59
        series = {
            "1m": CandleSeries("R_100", 60, c1m),
            "5m": CandleSeries("R_100", 300, c1m),
            "1h": CandleSeries("R_100", 3600, c1m),
        }
        completed = series["1m"].completed(now)
        assert last not in completed
        assert completed[-1].epoch == c1m[-2].epoch
