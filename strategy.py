"""
strategy.py — multi-timeframe trend-following with momentum and S/R.

The strategy answers one question per market: *is there a high-quality
directional setup right now, and in which direction?* It returns a
:class:`Signal` carrying a score and the reasons behind it.

Scoring
-------
The score is a **confluence count**, not a probability. It is the sum of
weighted conditions that are true. A score of 80 means "most of the things I
look for lined up", NOT "80% chance of winning". The bot never presents it as
a win probability, and neither should any UI built on top of it.

Components (weights sum to 100):
    EMA agreement (1m)         25
    HTF (1h) confirmation      20
    RSI momentum band          15
    S/R reaction               25
    Candlestick confirmation   15

A signal is only actionable when:
  * the 1m trend is not sideways,
  * RSI is inside the configured band,
  * the 1h trend agrees if REQUIRE_HTF_CONFIRMATION,
  * a candle confirmation is present (if REQUIRE_CANDLE_CONFIRMATION),
  * the weighted confluence score meets MIN_SIGNAL_SCORE (S/R is one score
    component, not an unconditional veto).

No look-ahead
-------------
Every input comes from ``CandleSeries.completed()``. The decision is made on
closed bars only, and the entry is assumed to occur at the next bar's open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Config
from indicators import (
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
)
from logger_config import get_logger
from market_data import Candle, CandleSeries

log = get_logger("strategy")


# Weights — must sum to 100.
W_EMA = 25
W_HTF = 20
W_RSI = 15
W_SR = 25
W_CANDLE = 15
assert W_EMA + W_HTF + W_RSI + W_SR + W_CANDLE == 100


@dataclass
class Signal:
    symbol: str
    index_value: Optional[int]
    direction: str                 # "CALL" (rise) or "PUT" (fall) or "NONE"
    score: int
    reasons: List[str] = field(default_factory=list)
    ema_fast: Optional[float] = None
    ema_mid: Optional[float] = None
    ema_slow: Optional[float] = None
    rsi_value: Optional[float] = None
    atr_value: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    price: Optional[float] = None
    candle_epoch: Optional[int] = None
    trend_1m: str = "sideways"
    trend_1h: str = "sideways"

    @property
    def actionable(self) -> bool:
        return self.direction in ("CALL", "PUT")

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.direction} score={self.score} "
            f"trend1m={self.trend_1m} trend1h={self.trend_1h} "
            f"rsi={self.rsi_value:.1f}" if self.rsi_value is not None
            else f"{self.symbol} {self.direction} score={self.score}"
        )


class Strategy:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------
    def evaluate(self, symbol: str, series: Dict[str, CandleSeries],
                 index_value: Optional[int] = None,
                 now: Optional[float] = None) -> Signal:
        """Evaluate one market across the three timeframes."""
        s1h, s5m, s1m = series["1h"], series["5m"], series["1m"]

        c1m = s1m.completed(now)
        c5m = s5m.completed(now)
        c1h = s1h.completed(now)

        if not c1m or not c5m or not c1h:
            return Signal(symbol, index_value, "NONE", 0,
                          ["insufficient completed candles"])

        closes_1m = [c.close for c in c1m]
        highs_1m = [c.high for c in c1m]
        lows_1m = [c.low for c in c1m]
        closes_1h = [c.close for c in c1h]

        price = closes_1m[-1]
        last = c1m[-1]

        # --- indicators on 1m -----------------------------------------
        ema_f = ema(closes_1m, self.cfg.ema_fast)
        ema_m = ema(closes_1m, self.cfg.ema_mid)
        ema_s = ema(closes_1m, self.cfg.ema_slow)
        rsi_vals = rsi(closes_1m, self.cfg.rsi_period)
        atr_vals = atr(highs_1m, lows_1m, closes_1m, 14)

        ef, em, es = ema_f[-1], ema_m[-1], ema_s[-1]
        rv = rsi_vals[-1]
        av = atr_vals[-1]

        if ef is None or em is None or es is None or rv is None:
            return Signal(symbol, index_value, "NONE", 0,
                          ["indicators not warmed up"],
                          ema_fast=ef, ema_mid=em, ema_slow=es,
                          rsi_value=rv, price=price,
                          candle_epoch=last.epoch)

        trend_1m = classify_trend(ef, em, es, price)

        # --- 1h context ------------------------------------------------
        ema_f_1h = ema(closes_1h, self.cfg.ema_fast)
        ema_s_1h = ema(closes_1h, self.cfg.ema_slow)
        trend_1h = "sideways"
        if ema_f_1h[-1] is not None and ema_s_1h[-1] is not None:
            if ema_f_1h[-1] > ema_s_1h[-1]:
                trend_1h = "up"
            elif ema_f_1h[-1] < ema_s_1h[-1]:
                trend_1h = "down"

        # --- S/R -------------------------------------------------------
        supports, resistances = find_swing_levels(
            highs_1m, lows_1m, lookback=self.cfg.sr_lookback, strength=2
        )
        sup = nearest_level(price, supports)
        res = nearest_level(price, resistances)

        reasons: List[str] = []
        score = 0

        # --- 1. EMA agreement -----------------------------------------
        if trend_1m == "up":
            score += W_EMA
            reasons.append(f"EMA stack bullish (20>50>200) +{W_EMA}")
        elif trend_1m == "down":
            score += W_EMA
            reasons.append(f"EMA stack bearish (20<50<200) +{W_EMA}")
        else:
            reasons.append("EMA stack sideways/contradictory +0")

        # Direction is decided by the 1m trend. No trend => no trade.
        if trend_1m == "sideways":
            return Signal(
                symbol, index_value, "NONE", score, reasons,
                ema_fast=ef, ema_mid=em, ema_slow=es, rsi_value=rv,
                atr_value=av, support=sup, resistance=res, price=price,
                candle_epoch=last.epoch, trend_1m=trend_1m, trend_1h=trend_1h,
            )

        direction = "CALL" if trend_1m == "up" else "PUT"

        # --- 2. HTF confirmation --------------------------------------
        htf_ok = (trend_1h == trend_1m)
        if htf_ok:
            score += W_HTF
            reasons.append(f"1h trend agrees ({trend_1h}) +{W_HTF}")
        else:
            reasons.append(f"1h trend disagrees ({trend_1h}) +0")

        # --- 3. RSI momentum band -------------------------------------
        rsi_ok = self.cfg.rsi_min <= rv <= self.cfg.rsi_max
        if direction == "PUT":
            # For a fall we want momentum in the lower half of the band.
            rsi_ok = (100 - self.cfg.rsi_max) <= rv <= (100 - self.cfg.rsi_min)
        if rsi_ok:
            score += W_RSI
            reasons.append(f"RSI {rv:.1f} inside momentum band +{W_RSI}")
        else:
            reasons.append(f"RSI {rv:.1f} outside momentum band +0")

        # --- 4. S/R reaction ------------------------------------------
        sr_ok, sr_reason = self._sr_reaction(
            direction, price, last, sup, res, av
        )
        if sr_ok:
            score += W_SR
            reasons.append(f"{sr_reason} +{W_SR}")
        else:
            reasons.append(f"{sr_reason} +0")

        # --- 5. Candlestick confirmation ------------------------------
        candle_ok, candle_reason = self._candle_confirmation(direction, last)
        if candle_ok:
            score += W_CANDLE
            reasons.append(f"{candle_reason} +{W_CANDLE}")
        else:
            reasons.append(f"{candle_reason} +0")

        # --- Gate ------------------------------------------------------
        actionable = True
        if self.cfg.require_htf_confirmation and not htf_ok:
            actionable = False
            reasons.append("BLOCKED: HTF confirmation required")
        if self.cfg.require_candle_confirmation and not candle_ok:
            actionable = False
            reasons.append("BLOCKED: candle confirmation required")
        if score < self.cfg.min_signal_score:
            actionable = False
            reasons.append(
                f"BLOCKED: score {score} < min {self.cfg.min_signal_score}"
            )

        return Signal(
            symbol=symbol,
            index_value=index_value,
            direction=direction if actionable else "NONE",
            score=score,
            reasons=reasons,
            ema_fast=ef, ema_mid=em, ema_slow=es,
            rsi_value=rv, atr_value=av,
            support=sup, resistance=res, price=price,
            candle_epoch=last.epoch,
            trend_1m=trend_1m, trend_1h=trend_1h,
        )

    # ------------------------------------------------------------------
    def _sr_reaction(self, direction: str, price: float, last: Candle,
                     support: Optional[float], resistance: Optional[float],
                     atr_value: Optional[float]) -> tuple:
        """
        Require a *reaction* at a level, never a bare touch.

        For a CALL: price is testing support and has rejected it (closed back
        above the level, with a bullish rejection or strong close).
        For a PUT: price is testing resistance and has rejected it.

        A "touch" alone — price merely reaching the level — is explicitly not
        enough, because on a random-walk index a touch is noise.
        """
        if atr_value is None or atr_value <= 0:
            return False, "S/R: no ATR for proximity test"

        prox = self.cfg.sr_proximity_atr * atr_value

        if direction == "CALL":
            if support is None:
                return False, "S/R: no support level identified"
            near = abs(price - support) <= prox or (
                last.low <= support + prox and last.close > support
            )
            if not near:
                return False, "S/R: price not at support"
            rejected = (last.close > support
                        and (is_bullish_rejection(last.open, last.high,
                                                  last.low, last.close)
                             or is_strong_bullish_close(last.open, last.high,
                                                        last.low, last.close)))
            if not rejected:
                return False, "S/R: at support but no bullish rejection"
            return True, f"S/R: bullish rejection at support {support:.2f}"

        # PUT
        if resistance is None:
            return False, "S/R: no resistance level identified"
        near = abs(price - resistance) <= prox or (
            last.high >= resistance - prox and last.close < resistance
        )
        if not near:
            return False, "S/R: price not at resistance"
        rejected = (last.close < resistance
                    and (is_bearish_rejection(last.open, last.high,
                                              last.low, last.close)
                         or is_strong_bearish_close(last.open, last.high,
                                                    last.low, last.close)))
        if not rejected:
            return False, "S/R: at resistance but no bearish rejection"
        return True, f"S/R: bearish rejection at resistance {resistance:.2f}"

    # ------------------------------------------------------------------
    def _candle_confirmation(self, direction: str, last: Candle) -> tuple:
        o, h, l, c = last.open, last.high, last.low, last.close
        if direction == "CALL":
            if is_bullish_rejection(o, h, l, c):
                return True, "candle: bullish rejection"
            if is_strong_bullish_close(o, h, l, c):
                return True, "candle: strong bullish close"
            return False, "candle: no bullish confirmation"
        if is_bearish_rejection(o, h, l, c):
            return True, "candle: bearish rejection"
        if is_strong_bearish_close(o, h, l, c):
            return True, "candle: strong bearish close"
        return False, "candle: no bearish confirmation"

    # ------------------------------------------------------------------
    def rank(self, signals: List[Signal]) -> List[Signal]:
        """Actionable signals, strongest first."""
        return sorted(
            [s for s in signals if s.actionable],
            key=lambda s: s.score,
            reverse=True,
        )
