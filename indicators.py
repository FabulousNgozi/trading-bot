"""
indicators.py — pure, dependency-free technical indicators.

Every function takes plain Python lists of floats and returns plain lists, so
the same code runs in the live bot and in the backtester with no divergence.

Conventions
-----------
* All series are ordered OLDEST -> NEWEST.
* Functions that need a warm-up return ``None`` for the leading positions
  where the indicator is not yet defined. This is deliberate: a caller must
  never be able to read a half-formed EMA as if it were real.
* No look-ahead: ``ema`` at index i uses only data[0..i].
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Simple moving average. First ``period-1`` entries are None."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """
    Exponential moving average, seeded with the SMA of the first ``period``
    values (the standard convention, and what most charting packages show).

    The first ``period-1`` entries are None. Index ``period-1`` holds the SMA
    seed; subsequent values use k = 2 / (period + 1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out

    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
def rsi(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """
    Wilder's RSI.

    Uses Wilder smoothing (not a simple average) after seeding with the mean
    of the first ``period`` gains/losses. This matches the reference values
    published by Wilder and used by TradingView's default RSI.

    Returns None for indices < ``period`` (the first value needs ``period``
    price changes, i.e. ``period+1`` prices).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta

    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi_from(ag: float, al: float) -> float:
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)

    return out


# ---------------------------------------------------------------------------
# Volatility / range
# ---------------------------------------------------------------------------
def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> List[Optional[float]]:
    """True range. Index 0 is None (no previous close)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        out[i] = max(hl, hc, lc)
    return out


def atr(highs: Sequence[float], lows: Sequence[float],
        closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder-smoothed Average True Range."""
    tr = true_range(highs, lows, closes)
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    seed_vals = [tr[i] for i in range(1, period + 1) if tr[i] is not None]
    if len(seed_vals) < period:
        return out
    prev = sum(seed_vals) / period
    out[period] = prev
    for i in range(period + 1, n):
        if tr[i] is None:
            continue
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Support / resistance
# ---------------------------------------------------------------------------
def find_swing_levels(highs: Sequence[float], lows: Sequence[float],
                      lookback: int = 50, strength: int = 2
                      ) -> Tuple[List[float], List[float]]:
    """
    Identify horizontal support and resistance levels from swing pivots.

    A pivot high at index i is a bar whose high is strictly greater than the
    ``strength`` bars on each side. Pivots are only confirmed ``strength``
    bars later — the caller must therefore only use levels derived from
    candles that had already closed at decision time (no look-ahead).

    Returns (supports, resistances), each sorted ascending, de-duplicated
    within a small tolerance.
    """
    n = len(highs)
    start = max(strength, n - lookback)
    resistances: List[float] = []
    supports: List[float] = []

    for i in range(start, n - strength):
        window_h = highs[i - strength:i + strength + 1]
        window_l = lows[i - strength:i + strength + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            resistances.append(highs[i])
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            supports.append(lows[i])

    def _dedupe(levels: List[float]) -> List[float]:
        if not levels:
            return []
        levels = sorted(levels)
        out = [levels[0]]
        for lv in levels[1:]:
            # Merge levels closer than 0.1% of price.
            if abs(lv - out[-1]) / max(abs(out[-1]), 1e-9) > 0.001:
                out.append(lv)
        return out

    return _dedupe(supports), _dedupe(resistances)


def nearest_level(price: float, levels: Sequence[float]) -> Optional[float]:
    """Closest level to ``price``, or None if there are no levels."""
    if not levels:
        return None
    return min(levels, key=lambda lv: abs(lv - price))


# ---------------------------------------------------------------------------
# Candlestick patterns
# ---------------------------------------------------------------------------
def is_bullish_rejection(open_: float, high: float, low: float, close: float,
                         body_ratio: float = 0.5) -> bool:
    """
    A bullish rejection candle: a long lower wick, a small body, and a close
    in the upper half of the range. Signals buyers defended a level.
    """
    rng = high - low
    if rng <= 0:
        return False
    body = abs(close - open_)
    lower_wick = min(open_, close) - low
    return (lower_wick >= body_ratio * rng
            and body <= 0.35 * rng
            and close >= low + 0.5 * rng)


def is_bearish_rejection(open_: float, high: float, low: float, close: float,
                         body_ratio: float = 0.5) -> bool:
    """Mirror of :func:`is_bullish_rejection`."""
    rng = high - low
    if rng <= 0:
        return False
    body = abs(close - open_)
    upper_wick = high - max(open_, close)
    return (upper_wick >= body_ratio * rng
            and body <= 0.35 * rng
            and close <= high - 0.5 * rng)


def is_strong_bullish_close(open_: float, high: float, low: float,
                            close: float, min_body_frac: float = 0.6) -> bool:
    """A wide-range bullish candle closing near its high."""
    rng = high - low
    if rng <= 0 or close <= open_:
        return False
    body = close - open_
    return body >= min_body_frac * rng and close >= high - 0.25 * rng


def is_strong_bearish_close(open_: float, high: float, low: float,
                            close: float, min_body_frac: float = 0.6) -> bool:
    """A wide-range bearish candle closing near its low."""
    rng = high - low
    if rng <= 0 or close >= open_:
        return False
    body = open_ - close
    return body >= min_body_frac * rng and close <= low + 0.25 * rng


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------
def classify_trend(ema_fast: Optional[float], ema_mid: Optional[float],
                   ema_slow: Optional[float], price: float,
                   min_separation: float = 0.0002) -> str:
    """
    Classify the EMA stack as 'up', 'down' or 'sideways'.

    ``min_separation`` is the minimum fractional gap between adjacent EMAs
    required to call a trend. Without it, three EMAs that are effectively
    equal would be reported as a trend and the bot would trade noise.
    """
    if ema_fast is None or ema_mid is None or ema_slow is None:
        return "sideways"

    def _sep(a: float, b: float) -> float:
        return abs(a - b) / max(abs(b), 1e-9)

    if ema_fast > ema_mid > ema_slow and _sep(ema_fast, ema_slow) >= min_separation:
        return "up" if price >= ema_fast else "sideways"
    if ema_fast < ema_mid < ema_slow and _sep(ema_fast, ema_slow) >= min_separation:
        return "down" if price <= ema_fast else "sideways"
    return "sideways"
