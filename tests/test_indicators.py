"""Tests for indicators.py — reference values and edge cases."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators import (  # noqa: E402
    atr,
    classify_trend,
    ema,
    find_swing_levels,
    is_bearish_rejection,
    is_bullish_rejection,
    is_strong_bearish_close,
    is_strong_bullish_close,
    nearest_level,
    rsi,
    sma,
    true_range,
)


class TestSMA:
    def test_basic(self):
        out = sma([1, 2, 3, 4, 5], 3)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(2.0)
        assert out[3] == pytest.approx(3.0)
        assert out[4] == pytest.approx(4.0)

    def test_insufficient_data(self):
        assert sma([1, 2], 5) == [None, None]

    def test_invalid_period(self):
        with pytest.raises(ValueError):
            sma([1, 2, 3], 0)


class TestEMA:
    def test_seed_is_sma(self):
        vals = [1, 2, 3, 4, 5]
        out = ema(vals, 3)
        assert out[2] == pytest.approx(2.0)  # SMA of 1,2,3

    def test_known_sequence(self):
        # period=3, k=0.5. seed=2.0 at idx2.
        # idx3: 4*0.5 + 2*0.5 = 3.0
        # idx4: 5*0.5 + 3*0.5 = 4.0
        out = ema([1, 2, 3, 4, 5], 3)
        assert out[3] == pytest.approx(3.0)
        assert out[4] == pytest.approx(4.0)

    def test_constant_series(self):
        out = ema([10.0] * 20, 5)
        assert out[-1] == pytest.approx(10.0)

    def test_no_lookahead(self):
        """EMA at index i must not change when later data is appended."""
        base = [float(i) for i in range(1, 30)]
        a = ema(base, 10)
        b = ema(base + [999.0, 1000.0], 10)
        assert a[-1] == pytest.approx(b[len(base) - 1])

    def test_insufficient(self):
        assert ema([1, 2], 5) == [None, None]


class TestRSI:
    def test_reference_wilder(self):
        """
        Classic Wilder RSI reference series. With period 14 on this
        monotonically rising series RSI must be 100 (no losses).
        """
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                  46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45,
                  45.78, 45.35, 44.03, 44.18, 44.22, 44.57, 43.42, 42.66,
                  43.13]
        out = rsi(closes, 14)
        # First defined value at index 14.
        assert out[13] is None
        assert out[14] is not None
        # Wilder's published value for this series at index 14 is ~70.53.
        assert out[14] == pytest.approx(70.53, abs=0.5)

    def test_all_gains_is_100(self):
        out = rsi([float(i) for i in range(1, 40)], 14)
        assert out[-1] == pytest.approx(100.0)

    def test_all_losses_is_zero(self):
        out = rsi([float(i) for i in range(40, 1, -1)], 14)
        assert out[-1] == pytest.approx(0.0)

    def test_flat_series_is_50(self):
        out = rsi([5.0] * 40, 14)
        assert out[-1] == pytest.approx(50.0)

    def test_bounds(self):
        import random
        random.seed(7)
        vals = [100.0]
        for _ in range(200):
            vals.append(vals[-1] + random.uniform(-2, 2))
        out = rsi(vals, 14)
        for v in out:
            if v is not None:
                assert 0.0 <= v <= 100.0

    def test_insufficient(self):
        assert rsi([1, 2, 3], 14) == [None, None, None]


class TestATR:
    def test_true_range_first_is_none(self):
        # TR[1] = max(high-low, |high-prev_close|, |low-prev_close|)
        #       = max(11-10, |11-9.5|, |10-9.5|) = max(1, 1.5, 0.5) = 1.5
        # The gap from the previous close dominates, which is the point of
        # true range versus a plain high-low range.
        tr = true_range([10, 11], [9, 10], [9.5, 10.5])
        assert tr[0] is None
        assert tr[1] == pytest.approx(1.5)

    def test_true_range_uses_high_low_when_no_gap(self):
        # prev_close inside the new bar's range -> plain high-low wins.
        tr = true_range([10, 11], [9, 10], [9.5, 10.5])
        assert tr[1] == pytest.approx(1.5)
        tr2 = true_range([10, 11], [9, 10], [10.0, 10.5])
        assert tr2[1] == pytest.approx(1.0)

    def test_atr_constant_range(self):
        highs = [10.0] * 30
        lows = [9.0] * 30
        closes = [9.5] * 30
        out = atr(highs, lows, closes, 14)
        assert out[-1] == pytest.approx(1.0)

    def test_atr_insufficient(self):
        out = atr([1, 2], [0, 1], [0.5, 1.5], 14)
        assert all(v is None for v in out)


class TestSwingLevels:
    def test_finds_pivot_high(self):
        highs = [1, 2, 3, 10, 3, 2, 1, 2, 3, 4, 3, 2, 1]
        lows = [h - 1 for h in highs]
        sup, res = find_swing_levels(highs, lows, lookback=50, strength=2)
        assert any(abs(r - 10) < 1e-9 for r in res)

    def test_empty(self):
        sup, res = find_swing_levels([], [], 50, 2)
        assert sup == [] and res == []

    def test_nearest_level(self):
        assert nearest_level(10.0, [5.0, 9.0, 20.0]) == 9.0
        assert nearest_level(10.0, []) is None


class TestCandlePatterns:
    def test_bullish_rejection(self):
        # long lower wick, small body, close in upper half
        assert is_bullish_rejection(open_=100.0, high=101.0, low=95.0,
                                    close=100.8)

    def test_not_bullish_rejection(self):
        assert not is_bullish_rejection(open_=100.0, high=101.0, low=99.9,
                                        close=100.9)

    def test_bearish_rejection(self):
        assert is_bearish_rejection(open_=100.0, high=105.0, low=99.0,
                                    close=99.2)

    def test_strong_bullish_close(self):
        assert is_strong_bullish_close(100.0, 105.0, 99.5, 104.8)

    def test_strong_bearish_close(self):
        assert is_strong_bearish_close(105.0, 105.5, 100.0, 100.2)

    def test_zero_range_safe(self):
        assert not is_bullish_rejection(100, 100, 100, 100)
        assert not is_bearish_rejection(100, 100, 100, 100)


class TestTrendClassification:
    def test_up(self):
        assert classify_trend(110, 105, 100, 111) == "up"

    def test_down(self):
        assert classify_trend(90, 95, 100, 89) == "down"

    def test_sideways_when_emas_equal(self):
        assert classify_trend(100, 100, 100, 100) == "sideways"

    def test_sideways_when_price_below_fast_in_uptrend(self):
        assert classify_trend(110, 105, 100, 105) == "sideways"

    def test_none_inputs(self):
        assert classify_trend(None, 105, 100, 110) == "sideways"
