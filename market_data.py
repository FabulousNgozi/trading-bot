"""
market_data.py — candle retrieval, validation and multi-timeframe assembly.

Responsibilities
----------------
* Discover all active Deriv synthetic-index symbols from ``active_symbols``.
* Fetch 1h / 5m / 1m candles and normalise them into a single ``Candle`` type.
* Drop the still-forming candle. Deriv returns the in-progress bar as the last
  element; treating it as final is the classic look-ahead bug.
* Enforce ordering and de-duplicate by epoch.

No look-ahead
-------------
``CandleSeries.completed()`` returns only bars whose close time has passed.
The strategy always consumes ``completed()``, never the raw list.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from config import (
    Config,
    MARKET_CATALOG,
)
from logger_config import get_logger

log = get_logger("market_data")


@dataclass(frozen=True)
class Candle:
    epoch: int          # open time, seconds UTC
    open: float
    high: float
    low: float
    close: float

    @property
    def close_time(self) -> int:
        return self.epoch  # caller supplies granularity separately

    def is_bullish(self) -> bool:
        return self.close > self.open

    def is_bearish(self) -> bool:
        return self.close < self.open

    def range(self) -> float:
        return self.high - self.low

    def body(self) -> float:
        return abs(self.close - self.open)


class CandleSeries:
    """An ordered candle series for one symbol at one granularity."""

    def __init__(self, symbol: str, granularity: int,
                 candles: Sequence[Candle]) -> None:
        self.symbol = symbol
        self.granularity = granularity
        self.candles: List[Candle] = list(candles)

    def __len__(self) -> int:
        return len(self.candles)

    def completed(self, now: Optional[float] = None) -> List[Candle]:
        """
        Candles whose interval has fully elapsed.

        A candle opened at epoch E with granularity G closes at E+G. If
        ``now < E+G`` the bar is still forming and must be excluded.
        """
        now = now if now is not None else time.time()
        return [c for c in self.candles if c.epoch + self.granularity <= now]

    def closes(self, now: Optional[float] = None) -> List[float]:
        return [c.close for c in self.completed(now)]

    def highs(self, now: Optional[float] = None) -> List[float]:
        return [c.high for c in self.completed(now)]

    def lows(self, now: Optional[float] = None) -> List[float]:
        return [c.low for c in self.completed(now)]

    def opens(self, now: Optional[float] = None) -> List[float]:
        return [c.open for c in self.completed(now)]

    def last_completed(self, now: Optional[float] = None) -> Optional[Candle]:
        done = self.completed(now)
        return done[-1] if done else None

    def last_completed_epoch(self, now: Optional[float] = None) -> Optional[int]:
        c = self.last_completed(now)
        return c.epoch if c else None


def parse_candles(raw: Sequence[dict]) -> List[Candle]:
    """
    Convert API candle dicts into Candle objects.

    Sorts by epoch and drops duplicates (the API can repeat the last bar
    across a reconnect). Malformed rows are skipped rather than crashing the
    scan — one bad bar must not stop the bot.
    """
    out: Dict[int, Candle] = {}
    for row in raw:
        try:
            epoch = int(row["epoch"])
            c = Candle(
                epoch=epoch,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if c.high < c.low:
            continue
        out[epoch] = c
    return [out[k] for k in sorted(out)]


class MarketData:
    """Fetches candles for discovered synthetic-index markets."""

    # Granularities in seconds: 1h context, 5m intermediate, 1m entry.
    GRAN_1H = 3600
    GRAN_5M = 300
    GRAN_1M = 60

    def __init__(self, client, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self._symbol_map: Dict[int, str] = {}
        self._available: Dict[int, bool] = {}
        self._scan_cursor = 0

    # ------------------------------------------------------------------
    # Symbol resolution
    # ------------------------------------------------------------------
    def resolve_symbols(self) -> Dict[int, str]:
        """
        Discover every active synthetic-index market from active_symbols.
        The symbol code and display name come directly from Deriv, so new
        market families are included without hard-coding each product.
        """
        try:
            symbols = self.client.active_symbols()
        except Exception as exc:
            log.error("Could not fetch active symbols: %s", exc)
            return {}

        known_ids = {code: key for key, (code, _) in MARKET_CATALOG.items()}
        active = sorted(
            (entry for entry in symbols
             if entry.get("market") == "synthetic_index"
             and not entry.get("is_trading_suspended")
             and entry.get("underlying_symbol")),
            key=lambda entry: str(entry.get("underlying_symbol")),
        )
        resolved: Dict[int, str] = {}
        used_ids = set()
        for offset, entry in enumerate(active):
            code = str(entry["underlying_symbol"])
            idx = known_ids.get(code, 2000 + offset)
            while idx in used_ids:
                idx += 10000
            used_ids.add(idx)
            resolved[idx] = code
            self._available[idx] = True

        self._symbol_map = resolved
        if resolved:
            log.info(
                "Resolved %d market(s): %s",
                len(resolved),
                ", ".join(resolved.values()),
            )
        else:
            log.error("No active synthetic-index markets resolved")
        return resolved

    @property
    def symbol_map(self) -> Dict[int, str]:
        return dict(self._symbol_map)

    def next_scan_batch(self, size: int = 5) -> Dict[int, str]:
        """Return the next rotating batch so a large market list is rate-safe."""
        markets = list(self._symbol_map.items())
        if not markets:
            return {}
        size = max(1, min(size, len(markets)))
        start = self._scan_cursor % len(markets)
        batch = [markets[(start + i) % len(markets)] for i in range(size)]
        self._scan_cursor = (start + size) % len(markets)
        return dict(batch)

    def is_available(self, idx: int) -> bool:
        return self._available.get(idx, False)

    # ------------------------------------------------------------------
    # Candle fetching
    # ------------------------------------------------------------------
    def fetch(self, symbol: str, granularity: int,
              count: int = 300) -> CandleSeries:
        raw = self.client.candles(symbol, granularity=granularity, count=count)
        return CandleSeries(symbol, granularity, parse_candles(raw))

    def fetch_multi_timeframe(self, symbol: str,
                              now: Optional[float] = None
                              ) -> Dict[str, CandleSeries]:
        """
        Fetch the three timeframes the strategy needs.

        Counts are chosen so the slowest indicator (EMA200 on 1h) has enough
        warm-up: 260 hourly bars, 300 five-minute, 300 one-minute.
        """
        return {
            "1h": self.fetch(symbol, self.GRAN_1H, count=260),
            "5m": self.fetch(symbol, self.GRAN_5M, count=300),
            "1m": self.fetch(symbol, self.GRAN_1M, count=300),
        }

    def warmup_ok(self, series: Dict[str, CandleSeries],
                  now: Optional[float] = None) -> bool:
        """
        True when every timeframe has enough completed bars for the slowest
        indicator it feeds. Prevents trading on a half-built EMA.
        """
        need_1h = self.cfg.ema_slow + 5
        need_5m = self.cfg.ema_slow + 5
        need_1m = max(self.cfg.ema_slow, self.cfg.sr_lookback) + 5
        return (
            len(series["1h"].completed(now)) >= need_1h
            and len(series["5m"].completed(now)) >= need_5m
            and len(series["1m"].completed(now)) >= need_1m
        )
