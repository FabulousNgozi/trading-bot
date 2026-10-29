"""
backtest.py — replay historical candles through the same strategy code.

Why this module exists
----------------------
The live bot and the backtester must share one strategy implementation, or the
backtest is fiction. This module therefore imports :class:`strategy.Strategy`
and :mod:`indicators` unchanged, and only replaces the *data source* and the
*execution model*.

Execution model (and its assumptions)
-------------------------------------
* A signal is evaluated on **closed** bars only.
* The entry is filled at the **next bar's open**, not the signal bar's close.
  Filling at the signal bar's close is the classic look-ahead bug and inflates
  results substantially.
* A 1-minute Rise/Fall contract wins if the exit price is above (CALL) or
  below (PUT) the entry price at expiry. Ties are a loss (the broker keeps
  the stake) — this is the conservative assumption.
* Payout is modelled as a fixed multiple of stake (default 1.95x, i.e. a
  95% return), which matches the live proposal observed on R_100 at $1 stake.
  Real payouts vary by symbol, volatility and time; a single constant is an
  approximation and is stated as such in the report.
* No spread is charged separately because the payout multiple already embeds
  the broker's margin. Slippage is not modelled — synthetic indices have no
  order book, but the entry price is the next open, which is the honest
  approximation.

What this cannot tell you
-------------------------
A backtest on synthetic indices measures the *strategy's* behaviour on a
random walk with drift. It does not predict live results. Sample size,
period, market and payout assumption are always reported alongside any win
rate, and the report explicitly says so.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from config import Config
from indicators import atr, classify_trend, ema, find_swing_levels, nearest_level, rsi
from logger_config import get_logger
from market_data import Candle, CandleSeries
from strategy import Strategy

log = get_logger("backtest")


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_epoch: int
    entry_price: float
    exit_epoch: int
    exit_price: float
    stake: float
    payout: float
    won: bool
    profit: float
    score: int
    reasons: str


@dataclass
class BacktestResult:
    symbol: str
    trades: List[BacktestTrade] = field(default_factory=list)
    start_epoch: int = 0
    end_epoch: int = 0
    granularity: int = 60
    payout_multiple: float = 1.95
    stake: float = 1.0
    initial_balance: float = 0.0

    # ---- derived metrics ------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def losses(self) -> int:
        return self.n - self.wins

    @property
    def win_rate(self) -> float:
        return (self.wins / self.n * 100.0) if self.n else 0.0

    @property
    def net_profit(self) -> float:
        return sum(t.profit for t in self.trades)

    @property
    def return_pct(self) -> float:
        if not self.initial_balance:
            return 0.0
        return self.net_profit / self.initial_balance * 100.0

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough on the cumulative P/L curve, in currency."""
        equity = self.initial_balance
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.profit
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @property
    def longest_losing_streak(self) -> int:
        best = cur = 0
        for t in self.trades:
            if t.won:
                cur = 0
            else:
                cur += 1
                best = max(best, cur)
        return best

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.profit for t in self.trades if t.profit > 0)
        gross_loss = -sum(t.profit for t in self.trades if t.profit < 0)
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "period": {
                "start": datetime.fromtimestamp(self.start_epoch, timezone.utc)
                .strftime("%Y-%m-%d %H:%M"),
                "end": datetime.fromtimestamp(self.end_epoch, timezone.utc)
                .strftime("%Y-%m-%d %H:%M"),
                "granularity_seconds": self.granularity,
            },
            "sample_size": self.n,
            "trades": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate, 2),
            "net_profit": round(self.net_profit, 2),
            "return_pct": round(self.return_pct, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "longest_losing_streak": self.longest_losing_streak,
            "profit_factor": (round(self.profit_factor, 2)
                              if self.profit_factor != float("inf") else None),
            "assumptions": {
                "stake": self.stake,
                "payout_multiple": self.payout_multiple,
                "entry": "next bar open",
                "tie_handling": "tie = loss",
                "slippage_modelled": False,
            },
        }


class Backtester:
    def __init__(self, cfg: Config, payout_multiple: float = 1.95) -> None:
        self.cfg = cfg
        self.strategy = Strategy(cfg)
        self.payout_multiple = payout_multiple

    # ------------------------------------------------------------------
    def run(self, symbol: str, candles_1m: Sequence[Candle],
            candles_5m: Optional[Sequence[Candle]] = None,
            candles_1h: Optional[Sequence[Candle]] = None,
            initial_balance: float = 1000.0) -> BacktestResult:
        """
        Replay a candle history.

        If 5m/1h series are not supplied they are aggregated from the 1m
        series, so a single 1m download is enough to exercise the full
        multi-timeframe logic.
        """
        c1m = list(candles_1m)
        if len(c1m) < self.cfg.ema_slow + 10:
            raise ValueError(
                f"Need at least {self.cfg.ema_slow + 10} 1m candles, "
                f"got {len(c1m)}"
            )

        c5m = list(candles_5m) if candles_5m else aggregate(c1m, 300)
        c1h = list(candles_1h) if candles_1h else aggregate(c1m, 3600)

        result = BacktestResult(
            symbol=symbol,
            start_epoch=c1m[0].epoch,
            end_epoch=c1m[-1].epoch,
            granularity=60,
            payout_multiple=self.payout_multiple,
            stake=self.cfg.stake,
            initial_balance=initial_balance,
        )

        warmup = max(self.cfg.ema_slow, self.cfg.sr_lookback) + 5
        last_signal_epoch: Optional[int] = None

        # Walk forward. At index i we may only use data up to and including i.
        for i in range(warmup, len(c1m) - 1):
            window_1m = c1m[: i + 1]
            now = c1m[i].epoch + 60  # the bar at i has just closed

            w5 = [c for c in c5m if c.epoch + 300 <= now]
            w1h = [c for c in c1h if c.epoch + 3600 <= now]
            if len(w5) < 10 or len(w1h) < 10:
                continue

            series = {
                "1m": CandleSeries(symbol, 60, window_1m),
                "5m": CandleSeries(symbol, 300, w5),
                "1h": CandleSeries(symbol, 3600, w1h),
            }

            sig = self.strategy.evaluate(symbol, series, now=now)
            if not sig.actionable:
                continue
            if sig.candle_epoch is not None and sig.candle_epoch == last_signal_epoch:
                continue

            # Entry at the NEXT bar's open — no look-ahead.
            entry_bar = c1m[i + 1]
            entry_price = entry_bar.open
            entry_epoch = entry_bar.epoch

            # 1-minute expiry: exit at the close of the entry bar.
            exit_price = entry_bar.close
            exit_epoch = entry_bar.epoch + 60

            if sig.direction == "CALL":
                won = exit_price > entry_price
            else:
                won = exit_price < entry_price

            stake = self.cfg.stake
            payout = round(stake * self.payout_multiple, 2)
            profit = round(payout - stake, 2) if won else -stake

            result.trades.append(BacktestTrade(
                symbol=symbol,
                direction=sig.direction,
                entry_epoch=entry_epoch,
                entry_price=entry_price,
                exit_epoch=exit_epoch,
                exit_price=exit_price,
                stake=stake,
                payout=payout,
                won=won,
                profit=profit,
                score=sig.score,
                reasons=" | ".join(sig.reasons),
            ))
            last_signal_epoch = sig.candle_epoch

        return result

    # ------------------------------------------------------------------
    def run_multi(self, data: Dict[str, Sequence[Candle]],
                  initial_balance: float = 1000.0) -> List[BacktestResult]:
        out = []
        for symbol, candles in data.items():
            try:
                out.append(self.run(symbol, candles,
                                    initial_balance=initial_balance))
            except ValueError as exc:
                log.warning("Skipping %s: %s", symbol, exc)
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def export_csv(results: Sequence[BacktestResult], path: str) -> str:
        """Write every trade from every result to one CSV."""
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "symbol", "direction", "entry_time_utc", "entry_price",
                "exit_time_utc", "exit_price", "stake", "payout", "won",
                "profit", "score", "reasons",
            ])
            for r in results:
                for t in r.trades:
                    w.writerow([
                        t.symbol, t.direction,
                        datetime.fromtimestamp(t.entry_epoch, timezone.utc)
                        .strftime("%Y-%m-%d %H:%M:%S"),
                        t.entry_price,
                        datetime.fromtimestamp(t.exit_epoch, timezone.utc)
                        .strftime("%Y-%m-%d %H:%M:%S"),
                        t.exit_price, t.stake, t.payout,
                        "1" if t.won else "0", t.profit, t.score, t.reasons,
                    ])
        return path


# ---------------------------------------------------------------------------
def aggregate(candles: Sequence[Candle], seconds: int) -> List[Candle]:
    """Aggregate a 1m series into a coarser granularity."""
    if not candles:
        return []
    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        key = (c.epoch // seconds) * seconds
        buckets.setdefault(key, []).append(c)
    out: List[Candle] = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append(Candle(
            epoch=key,
            open=group[0].open,
            high=max(g.high for g in group),
            low=min(g.low for g in group),
            close=group[-1].close,
        ))
    return out


def format_report(results: Sequence[BacktestResult]) -> str:
    """Human-readable multi-market report with explicit caveats."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("BACKTEST REPORT")
    lines.append("=" * 78)
    lines.append("")
    lines.append("ASSUMPTIONS (read before interpreting any number below):")
    lines.append("  * Entry at the NEXT bar's open (no look-ahead).")
    lines.append("  * 1-minute expiry; tie counts as a LOSS.")
    lines.append("  * Payout modelled as a constant multiple of stake.")
    lines.append("  * Slippage not modelled. Synthetic indices have no book.")
    lines.append("  * Results are historical simulation, NOT a forecast.")
    lines.append("")

    for r in results:
        s = r.summary()
        lines.append("-" * 78)
        lines.append(f"{s['symbol']}   {s['period']['start']} -> "
                     f"{s['period']['end']}  (1m bars)")
        lines.append("-" * 78)
        lines.append(f"  Sample size      : {s['sample_size']} trades")
        lines.append(f"  Wins / Losses    : {s['wins']} / {s['losses']}")
        lines.append(f"  Win rate         : {s['win_rate_pct']:.2f}%")
        lines.append(f"  Net profit       : {s['net_profit']:+.2f}")
        lines.append(f"  Return           : {s['return_pct']:+.2f}%")
        lines.append(f"  Max drawdown     : {s['max_drawdown']:.2f}")
        lines.append(f"  Longest loss run : {s['longest_losing_streak']}")
        pf = s["profit_factor"]
        lines.append(f"  Profit factor    : {pf if pf is not None else 'n/a'}")
        lines.append(f"  Payout multiple  : {s['assumptions']['payout_multiple']}x")
        lines.append("")

    lines.append("=" * 78)
    lines.append("A backtest describes what this strategy WOULD have done on this")
    lines.append("particular history. It does not predict future performance, and")
    lines.append("a small sample is not evidence of an edge.")
    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _fetch_public(symbol: str, granularity: int, count: int) -> List[Candle]:
    """
    Download candles from the PUBLIC endpoint (no auth, no account).

    Used by the CLI so the backtester can be run without credentials.
    """
    import json
    import ssl

    import websocket

    from config import DERIV_WS_PUBLIC

    ws = websocket.create_connection(
        DERIV_WS_PUBLIC, timeout=30, sslopt={"cert_reqs": ssl.CERT_NONE}
    )
    try:
        ws.send(json.dumps({
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": min(count, 1000),
            "end": "latest",
            "req_id": 1,
        }))
        raw = json.loads(ws.recv())
    finally:
        ws.close()
    from market_data import parse_candles
    return parse_candles(raw.get("candles", []))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    from config import ConfigError, ensure_data_dir, load_config

    p = argparse.ArgumentParser(
        description="Backtest the Deriv Volatility strategy on historical candles."
    )
    p.add_argument("--symbols", default="R_10,R_25,R_50,R_75,R_100",
                   help="Comma-separated Deriv symbols (default: all 5 standard "
                        "Volatility indices)")
    p.add_argument("--count", type=int, default=1000,
                   help="Candles per timeframe (max 1000, default 1000)")
    p.add_argument("--payout", type=float, default=1.95,
                   help="Payout multiple, e.g. 1.95 for a 95%% return")
    p.add_argument("--balance", type=float, default=1000.0,
                   help="Starting balance for return/drawdown (default 1000)")
    p.add_argument("--csv", default="data/backtest_report.csv",
                   help="CSV output path")
    p.add_argument("--offline", action="store_true",
                   help="Use synthetic random-walk data instead of downloading")
    args = p.parse_args(argv)

    try:
        cfg = load_config(require_credentials=False)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    ensure_data_dir(cfg)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bt = Backtester(cfg, payout_multiple=args.payout)
    results: List[BacktestResult] = []

    for sym in symbols:
        try:
            if args.offline:
                c1m = _synthetic_candles(1000)
                c5m = aggregate(c1m, 300)
                c1h = aggregate(c1m, 3600)
                print(f"{sym}: SYNTHETIC data (not real market history)")
            else:
                c1m = _fetch_public(sym, 60, args.count)
                c5m = _fetch_public(sym, 300, args.count)
                c1h = _fetch_public(sym, 3600, args.count)
                print(f"{sym}: 1m={len(c1m)} 5m={len(c5m)} 1h={len(c1h)}")
            if len(c1m) < cfg.ema_slow + 10:
                print(f"  skipped: only {len(c1m)} 1m candles")
                continue
            results.append(bt.run(sym, c1m, c5m, c1h,
                                  initial_balance=args.balance))
        except Exception as exc:
            print(f"  {sym}: failed — {exc}")

    if not results:
        print("\nNo results produced.", file=sys.stderr)
        return 1

    print()
    print(format_report(results))
    path = bt.export_csv(results, args.csv)
    print(f"\nCSV written: {path}")
    return 0


def _synthetic_candles(n: int, seed: int = 42) -> List[Candle]:
    """Deterministic random-walk candles for offline smoke-testing only."""
    import random
    rng = random.Random(seed)
    price = 1000.0
    out: List[Candle] = []
    start = 1_700_000_000
    for i in range(n):
        o = price
        price += rng.gauss(0, 0.6)
        c = price
        h = max(o, c) + abs(rng.gauss(0, 0.3))
        l = min(o, c) - abs(rng.gauss(0, 0.3))
        out.append(Candle(epoch=start + i * 60, open=o, high=h, low=l, close=c))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
