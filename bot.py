"""
bot.py — the main loop.

Startup sequence (any failure here stops the bot before it can trade):

    1. load + validate config
    2. set up logging (with token redaction)
    3. open storage, reconcile any unresolved contracts
    4. connect to Deriv and POSITIVELY confirm the account is demo
    5. resolve the real symbol codes for the configured indices
    6. verify the contract type is offered on each market
    7. enter the scan loop

Scan loop, every SCAN_INTERVAL_SECONDS:

    * refresh every open contract once
    * if risk blocks trading, log why and skip
    * evaluate the next rotating batch of discovered markets
    * choose a bounded strategy profile and per-market researched expiry
    * rank actionable signals by score and settled outcomes
    * open the strongest supported signals up to the configured open-contract limit
    * never re-enter on the same completed candle

Run with:  python bot.py
Stop with: Ctrl-C (SIGINT) — the bot keeps monitoring open contracts through
           settlement before exiting, so results are recorded when possible.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Dict, List, Optional

from config import Config, ConfigError, ensure_data_dir, load_config
from ai_brain import AIBrain
from deriv_client import (
    AuthError,
    DemoAccountRequired,
    DerivClient,
    DerivError,
    RateLimited,
)
from logger_config import get_logger, setup_logging
from market_data import MarketData
from risk_manager import RiskManager
from storage import Storage
from strategy import Signal, Strategy, strategy_for_market
from trade_executor import TradeExecutor
from trade_monitor import TradeMonitor

log = get_logger("bot")


class TradingBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.storage = Storage(cfg.db_path, cfg.csv_path,
                               cfg.trading_day_timezone)
        self.client = DerivClient(cfg)
        self.market = MarketData(self.client, cfg)
        self.strategy = Strategy(cfg)
        self.ai_brain = AIBrain(cfg, self.storage)
        self.risk = RiskManager(cfg, self.storage)
        self.executor = TradeExecutor(self.client, cfg, self.risk, self.storage)
        self.monitor = TradeMonitor(self.client, cfg, self.risk, self.storage)

        self._running = False
        self._last_signal_epoch: Dict[str, int] = {}

    # ==================================================================
    # Startup
    # ==================================================================
    def startup(self) -> bool:
        log.info("=" * 68)
        log.info("Deriv Volatility Bot — DEMO ONLY")
        log.info("=" * 68)

        # --- connect + demo verification ------------------------------
        try:
            info = self.client.connect()
        except DemoAccountRequired as exc:
            log.error("")
            log.error("  *** DEMO-ONLY PROTECTION TRIGGERED ***")
            log.error("  %s", exc)
            log.error("  No purchase will be attempted. Exiting.")
            log.error("")
            return False
        except AuthError as exc:
            log.error("Authentication failed: %s", exc)
            return False
        except DerivError as exc:
            log.error("Could not connect to Deriv: %s", exc)
            return False

        # Re-assert the invariant one final time before any trading code runs.
        if info.get("account_type") != "demo":
            log.error("DEMO-ONLY: account type is %r — refusing to run",
                      info.get("account_type"))
            return False

        log.info("DEMO MODE CONFIRMED — account %s, balance %s %s",
                 info["account_id"], info["balance"], info["currency"])

        # --- reconcile any contracts left over from a previous run -----
        try:
            n = self.monitor.reconcile()
            if n:
                log.info("Reconciled %d contract(s) from a previous run", n)
        except Exception as exc:
            log.warning("Reconciliation error: %s", exc)

        # --- symbols ---------------------------------------------------
        symbols = self.market.resolve_symbols()
        if not symbols:
            log.error("No tradable markets — exiting")
            return False

        log.info("Available markets: %s",
                 ", ".join(symbols.values()))
        log.info("Stake: %.2f %s | Duration: %d%s | Max open: %d",
                 self.cfg.stake, self.cfg.currency, self.cfg.duration,
                 self.cfg.duration_unit, self.cfg.max_open_contracts)
        log.info("Risk: daily loss %.2f | daily profit target %.2f | "
                 "%d consecutive losses | "
                 "%d trades/day | %ds cooldown",
                 self.cfg.daily_loss_limit, self.cfg.daily_profit_target,
                 self.cfg.max_consecutive_losses,
                 self.cfg.max_trades_per_day, self.cfg.cooldown_seconds)
        log.info("Trading day boundary: midnight %s",
                 self.cfg.trading_day_timezone)
        log.info("Adaptive AI brain: %s",
                 "enabled" if self.cfg.ai_brain_enabled else "disabled")
        return True

    # ==================================================================
    # Main loop
    # ==================================================================
    def run(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    self.scan_once()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log.exception("Scan error: %s", exc)
                self._sleep(self.cfg.scan_interval_seconds)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down")
        finally:
            self.shutdown()

    def _sleep(self, seconds: float) -> None:
        """Sleep in small slices so Ctrl-C is responsive."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(0.5, max(0.0, end - time.time())))

    # ==================================================================
    # One scan
    # ==================================================================
    def scan_once(self) -> None:
        # Refresh every pending contract without blocking scans for the others.
        self._monitor_open_contracts()
        available_slots = max(
            0, self.cfg.max_open_contracts - self.storage.count_open_trades()
        )
        if available_slots == 0:
            log.info("Open-contract limit reached (%d/%d)",
                     self.storage.count_open_trades(), self.cfg.max_open_contracts)
            return

        # 2. Risk gate.
        decision = self.risk.can_trade(stake=self.cfg.stake)
        if not decision.allowed:
            log.info("Paused — %s", decision.reason)
            return

        # 3. Route each market through its own strategy family.
        signals: List[Signal] = []
        for idx, symbol in self.market.next_scan_batch().items():
            try:
                series = self.market.fetch_multi_timeframe(symbol)
            except RateLimited:
                log.warning("Rate limited fetching %s — skipping this scan",
                            symbol)
                return
            except DerivError as exc:
                log.warning("Could not fetch candles for %s: %s", symbol, exc)
                continue

            if not self.market.warmup_ok(series):
                log.debug("%s: not enough history yet", symbol)
                continue

            market_strategy = strategy_for_market(symbol, self.cfg)
            if self.cfg.ai_brain_enabled:
                policy, scan_strategy, study = self.ai_brain.select_strategy(
                    type(market_strategy)
                )
                log.info("AI study for %s: %s", symbol, study["status"])
            else:
                policy, scan_strategy = "baseline", market_strategy

            sig = scan_strategy.evaluate(symbol, series, index_value=idx)
            sig.strategy_variant = policy
            sig.strategy_id = getattr(scan_strategy, "strategy_id",
                                      "trend_confluence_v1")
            if self.cfg.ai_brain_enabled:
                duration_choice = self.ai_brain.select_duration(
                    symbol, series, scan_strategy, policy
                )
                sig.selected_duration = int(duration_choice["duration"])
                sig.selected_duration_unit = str(
                    duration_choice["duration_unit"]
                )
                if sig.actionable:
                    log.info("AI duration for %s: %d%s (%s)", symbol,
                             sig.selected_duration,
                             sig.selected_duration_unit,
                             duration_choice["note"])
            elif sig.strategy_id == "boom_crash_spike_fade_v1":
                sig.selected_duration = 300
                sig.selected_duration_unit = "s"
            self._log_signal(sig)

            # 5. Never re-enter on the same completed candle.
            if sig.actionable and sig.candle_epoch is not None:
                if self._last_signal_epoch.get(symbol) == sig.candle_epoch:
                    log.info("%s: already acted on candle %s — skipping",
                             symbol, sig.candle_epoch)
                    continue
            signals.append(sig)

        # 6. Fill available slots with the strongest supported signals.
        ranked = (
            self.ai_brain.rank(signals) if self.cfg.ai_brain_enabled
            else self.strategy.rank(signals)
        )
        if not ranked:
            log.info("No actionable signals this scan")
            return

        opened = 0
        for candidate in ranked:
            if opened >= available_slots:
                break
            offered = self.executor.available_contract_types(candidate.symbol)
            if not offered:
                log.info("Skipping %s: could not confirm offered contract types",
                         candidate.symbol)
                continue
            if candidate.direction not in offered:
                log.info("Skipping %s: %s is not offered (available: %s)",
                         candidate.symbol, candidate.direction,
                         ", ".join(sorted(offered)))
                continue
            if candidate.ai_score is not None:
                log.info("Brain selected: %s (strategy %d, brain %.1f; %s)",
                         candidate.summary(), candidate.score,
                         candidate.ai_score, candidate.ai_note)
            else:
                log.info("Selected signal: %s (score %d)",
                         candidate.summary(), candidate.score)
            result = self.executor.execute(
                candidate, candidate.symbol, same_scan_batch=(opened > 0)
            )
            if not result.ok:
                log.info("Trade not taken for %s: %s",
                         candidate.symbol, result.reason)
                if "rate limited" in result.reason.lower():
                    break
                continue

            if candidate.candle_epoch is not None:
                self._last_signal_epoch[candidate.symbol] = candidate.candle_epoch
            opened += 1
        if opened:
            log.info("Opened %d contract(s) this scan; now %d/%d open",
                     opened, self.storage.count_open_trades(),
                     self.cfg.max_open_contracts)

    # ==================================================================
    def _monitor_open_contracts(self) -> None:
        for trade in self.storage.get_open_trades():
            cid = int(trade["contract_id"])
            result = self.monitor.poll_result(cid)
            if result["resolved"]:
                log.info("Result: contract %s %s  P/L %+.2f", cid,
                         result["status"].upper(), result["profit"] or 0.0)
            elif result["status"] == "unresolved":
                log.error("Contract %s could not be resolved — trading blocked "
                          "until reconciled", cid)

    def _finish_open_contracts(self) -> None:
        """On shutdown, keep polling pending contracts through settlement."""
        if not getattr(self.client, "_connected", False):
            return
        pending = self.storage.get_open_trades()
        if not pending:
            return
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "t": 2}
        max_wait = max(
            int(t.get("duration") or self.cfg.duration)
            * units.get(t.get("duration_unit") or self.cfg.duration_unit, 60)
            + 90
            for t in pending
        )
        deadline = time.monotonic() + max_wait
        log.info("Finishing %d open contract(s) before shutdown", len(pending))
        while self.storage.get_open_trades() and time.monotonic() < deadline:
            self._monitor_open_contracts()
            if self.storage.get_open_trades():
                time.sleep(2)
        remaining = self.storage.get_open_trades()
        if remaining:
            log.error("Shutdown deadline reached with %d contract(s) still open; "
                      "they will be reconciled on next startup", len(remaining))

    # ==================================================================
    def _log_signal(self, sig: Signal) -> None:
        if sig.ema_fast is None:
            log.info("%s: %s", sig.symbol, "; ".join(sig.reasons))
            return
        log.info(
            "%s  price=%.2f  EMA20=%.2f EMA50=%.2f EMA200=%.2f  RSI=%.1f  "
            "S=%.2f R=%.2f  trend=%s/%s  score=%d  -> %s",
            sig.symbol, sig.price or 0, sig.ema_fast, sig.ema_mid or 0,
            sig.ema_slow or 0, sig.rsi_value or 0,
            sig.support or 0, sig.resistance or 0,
            sig.trend_1m, sig.trend_1h, sig.score, sig.direction,
        )
        if not sig.actionable:
            log.info("%s blocked by: %s", sig.symbol, "; ".join(sig.reasons))
        for r in sig.reasons:
            log.debug("    %s", r)

    # ==================================================================
    def shutdown(self) -> None:
        self._running = False
        try:
            self._finish_open_contracts()
        except Exception as exc:
            log.exception("Could not finish open contracts: %s", exc)
        try:
            self.client.close()
        except Exception:
            pass
        try:
            self.ai_brain.close()
        except Exception as exc:
            log.warning("Could not finish AI research before shutdown: %s", exc)
        try:
            self.storage.export_csv()
        except Exception:
            pass
        self.storage.close()
        log.info("Shutdown complete")


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        print("Copy .env.example to .env and fill in your values.\n",
              file=sys.stderr)
        return 2

    ensure_data_dir(cfg)
    setup_logging(cfg.log_level, cfg.log_file or None)

    bot = TradingBot(cfg)

    def _handle_sigint(signum, frame):
        log.info("SIGINT received — finishing current contract then exiting")
        bot._running = False

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    if not bot.startup():
        bot.shutdown()
        return 1

    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
