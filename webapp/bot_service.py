"""
bot_service.py — the supervisor that runs the engine in a background thread.

Architecture
------------
``WebBot`` subclasses :class:`bot.TradingBot`. It publishes engine state for
the dashboard and applies the same bounded adaptive brain as the CLI bot: the
brain selects a predefined strategy profile, researches expiry per market,
and ranks signals already approved by Strategy. RiskManager, TradeExecutor
and TradeMonitor remain the only paths for risk and execution.

The subclass adds three things:

  1. **Publication.** After each scan it writes the per-market signal table and
     the risk snapshot into ``webapp.state.STATE`` so HTTP handlers can read a
     consistent view without touching engine objects across threads.
  2. **A cooperative pause.** Between scans the loop waits on a threading
     Event; "pause" sets it, "resume" clears it. A paused bot keeps its
     WebSocket but places no trades.
  3. **Log tee.** A logging handler copies every redacted record into a ring
     buffer that the UI tails.

What this class deliberately does NOT add
-----------------------------------------
* No method that buys, sells or sizes a trade. The brain may select only a
  historically studied 1, 2, 3 or 5 minute expiry for the current market.
* No way to relax a risk check, raise a stake, or skip the demo guard.
* No way to bypass ``RiskManager.can_trade`` — the executor calls it itself.

Stop semantics
--------------
``stop()`` is synchronous and cooperative: it asks the loop to exit, waits for
the current contract to be monitored to a terminal state (bounded), closes the
WebSocket, exports the CSV, and closes storage. The open-contract slot is
released only when a final state was actually confirmed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bot import TradingBot
from config import Config, ConfigError, ensure_data_dir, load_config
from deriv_client import (
    AuthError,
    DemoAccountRequired,
    DerivError,
    RateLimited,
)
from logger_config import RedactionFilter, get_logger, setup_logging
from storage import Storage
from strategy import Signal, strategy_for_market

from .state import (
    ERROR,
    NOT_CONFIGURED,
    PAUSED,
    RUNNING,
    STARTING,
    STATE,
    STOPPED,
    STOPPING,
)

log = get_logger("webapp")


# ---------------------------------------------------------------------------
# Log tee: every record the bot emits also lands in the UI ring buffer.
# ---------------------------------------------------------------------------
class UiLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.addFilter(RedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        STATE.add_log(record.levelname, msg)


_ui_handler: Optional[UiLogHandler] = None
_ui_handler_lock = threading.Lock()


def attach_ui_logging(level: str = "INFO") -> None:
    """Install console/file logging plus the UI tee, exactly once."""
    global _ui_handler
    setup_logging(level, os.environ.get("LOG_FILE", "data/bot.log"))
    with _ui_handler_lock:
        root = logging.getLogger()
        if _ui_handler is None:
            _ui_handler = UiLogHandler()
            _ui_handler.setLevel(logging.INFO)
            _ui_handler._deriv_bot = True  # type: ignore[attr-defined]
            root.addHandler(_ui_handler)
        # Keep the tee alive if setup_logging removed our handlers.
        if _ui_handler not in root.handlers:
            root.addHandler(_ui_handler)


# ---------------------------------------------------------------------------
# The engine, observed
# ---------------------------------------------------------------------------
class WebBot(TradingBot):
    """``TradingBot`` with publication hooks. No trading logic added."""

    def __init__(self, cfg: Config, pause_event: threading.Event) -> None:
        super().__init__(cfg)
        self._pause_event = pause_event
        STATE.set_brain({
            "enabled": cfg.ai_brain_enabled,
            "status": ("Enabled — waiting for the first scan." if cfg.ai_brain_enabled
                       else "Disabled — using strategy score only."),
            "ranked": [],
            "policy_stats": {},
            "selected_policy": None,
        })

    # -- startup: mirrors bot.py's sequence, but records the failure reason --
    def startup(self) -> bool:
        log.info("=" * 68)
        log.info("Deriv Volatility Bot — DEMO ONLY (web control panel)")
        log.info("=" * 68)

        try:
            info = self.client.connect()
        except DemoAccountRequired as exc:
            log.error("*** DEMO-ONLY PROTECTION TRIGGERED ***")
            log.error("%s", exc)
            STATE.set_status(ERROR, "Demo-only guard blocked the connection.",
                             error=str(exc))
            return False
        except AuthError as exc:
            log.error("Authentication failed: %s", exc)
            STATE.set_status(ERROR, "Authentication failed.", error=str(exc))
            return False
        except DerivError as exc:
            log.error("Could not connect to Deriv: %s", exc)
            STATE.set_status(ERROR, "Could not connect to Deriv.", error=str(exc))
            return False

        # Re-assert the invariant before any trading code can run.
        if info.get("account_type") != "demo":
            msg = (f"DEMO-ONLY: account type is {info.get('account_type')!r} "
                   f"— refusing to run")
            log.error(msg)
            STATE.set_status(ERROR, "Account is not a demo account.", error=msg)
            return False

        STATE.set_account(info)
        log.info("DEMO MODE CONFIRMED — account %s, balance %s %s",
                 info["account_id"], info["balance"], info["currency"])

        try:
            n = self.monitor.reconcile()
            if n:
                log.info("Reconciled %d contract(s) from a previous run", n)
        except Exception as exc:
            log.warning("Reconciliation error: %s", exc)

        symbols = self.market.resolve_symbols()
        if not symbols:
            msg = "No active synthetic-index markets were returned by Deriv."
            log.error(msg)
            STATE.set_status(ERROR, "No tradable markets.", error=msg)
            return False

        log.info("Available markets: %s", ", ".join(symbols.values()))
        log.info("Stake: %.2f %s | Duration: %d%s | Max open: %d",
                 self.cfg.stake, self.cfg.currency, self.cfg.duration,
                 self.cfg.duration_unit, self.cfg.max_open_contracts)
        trade_cap = (f"{self.cfg.max_trades_per_day} trades/day"
                     if self.cfg.max_trades_per_day > 0
                     else "unlimited daily entries")
        log.info("Risk: daily loss %.2f | daily profit target %.2f | "
                 "%d consecutive losses | %s | %ds cooldown",
                 self.cfg.daily_loss_limit, self.cfg.daily_profit_target,
                 self.cfg.max_consecutive_losses, trade_cap,
                 self.cfg.cooldown_seconds)
        return True

    # -- the loop, with a pause gate between iterations -------------------
    def run(self) -> None:
        self._running = True
        try:
            while self._running:
                # A pause blocks new scans but keeps the connection open.
                while self._running and self._pause_event.is_set():
                    time.sleep(0.4)
                if not self._running:
                    break
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

    # -- one scan: shared strategy, bounded brain ranking and publication -
    def scan_once(self) -> None:
        self._monitor_open_contracts()
        available_slots = max(
            0, self.cfg.max_open_contracts - self.storage.count_open_trades()
        )
        if available_slots == 0:
            log.info("Open-contract limit reached (%d/%d)",
                     self.storage.count_open_trades(), self.cfg.max_open_contracts)
            return

        decision = self.risk.can_trade(stake=self.risk.next_stake())
        STATE.set_risk(self.risk.status())
        if not decision.allowed:
            log.info("Paused — %s", decision.reason)
            self._publish_scanner_skipped(decision.reason)
            research = self.ai_brain.research_state(
                getattr(self.strategy, "strategy_id", "trend_confluence_v1")
            )
            STATE.set_brain({
                "enabled": self.cfg.ai_brain_enabled,
                "status": (f"Waiting — risk manager blocks entries: {decision.reason}"
                           if self.cfg.ai_brain_enabled else
                           "Disabled — using strategy score only."),
                "ranked": [],
                **research,
                "selected_policy": None,
            })
            return

        study = (self.ai_brain.research_state()
                 if self.cfg.ai_brain_enabled else
                 {"enabled": False,
                  "status": "Disabled — using strategy score only.",
                  "policy_stats": {}})

        signals: List[Signal] = []
        rows: List[Dict[str, Any]] = []
        duration_choices: Dict[str, Dict[str, object]] = {}

        for idx, symbol in self.market.next_scan_batch().items():
            try:
                series = self.market.fetch_multi_timeframe(symbol)
            except RateLimited:
                log.warning("Rate limited fetching %s — skipping this scan",
                            symbol)
                return
            except DerivError as exc:
                log.warning("Could not fetch candles for %s: %s", symbol, exc)
                rows.append(self._row_errored(idx, symbol, str(exc)))
                continue

            if not self.market.warmup_ok(series):
                log.debug("%s: not enough history yet", symbol)
                rows.append(self._row_errored(
                    idx, symbol, "warming up — not enough completed candles"))
                continue

            completed_1m = series["1m"].completed()
            self.storage.resolve_shadow_signals(symbol, completed_1m)

            market_strategy = strategy_for_market(symbol, self.cfg)
            if self.cfg.ai_brain_enabled:
                policy, scan_strategy, market_study = self.ai_brain.select_strategy(
                    type(market_strategy)
                )
                study = market_study
            else:
                policy, scan_strategy = "baseline", market_strategy

            sig = scan_strategy.evaluate(symbol, series, index_value=idx)
            sig.strategy_variant = policy
            sig.strategy_id = getattr(scan_strategy, "strategy_id",
                                      "trend_confluence_v1")
            if self.cfg.ai_brain_enabled:
                choice = self.ai_brain.select_duration(
                    symbol, series, scan_strategy, policy
                )
                sig.selected_duration = int(choice["duration"])
                sig.selected_duration_unit = str(choice["duration_unit"])
                duration_choices[symbol] = choice
            elif sig.strategy_id == "boom_crash_spike_fade_v1":
                sig.selected_duration = 300
                sig.selected_duration_unit = "s"
                duration_choices[symbol] = {
                    "source": "multiplier auto-management",
                    "note": "Close at +/-50% of stake or after 300 seconds.",
                }
            if sig.actionable:
                sig.shadow_id = self.storage.record_shadow_signal(
                    sig, sig.selected_duration or self.cfg.duration
                )
            self._log_signal(sig)

            repeat = False
            if sig.actionable and sig.candle_epoch is not None:
                if self._last_signal_epoch.get(symbol) == sig.candle_epoch:
                    log.info("%s: already acted on candle %s — skipping",
                             symbol, sig.candle_epoch)
                    repeat = True
            if not repeat:
                signals.append(sig)
            row = self._row(idx, symbol, sig, repeat)
            row["duration_note"] = duration_choices.get(symbol, {}).get("note", "")
            rows.append(row)

        ranked = (self.ai_brain.rank(signals) if self.cfg.ai_brain_enabled
                  else self.strategy.rank(signals))
        ranked_symbols = {sig.symbol: sig for sig in ranked}
        for row in rows:
            sig = ranked_symbols.get(row["symbol"])
            if sig:
                row["ai_score"] = sig.ai_score
                row["ai_note"] = sig.ai_note
                row["duration"] = sig.selected_duration or self.cfg.duration
                row["duration_unit"] = (
                    sig.selected_duration_unit
                    if sig.selected_duration is not None
                    else self.cfg.duration_unit
                )
                row["duration_note"] = duration_choices.get(
                    sig.symbol, {}).get("note", "")
        STATE.merge_scanner(rows)
        STATE.set_risk(self.risk.status())
        study["status"] = (
            f"{study['status']} Top approved signals ranked."
            if ranked and self.cfg.ai_brain_enabled else
            "Scanning — no strategy-approved signals to rank."
            if self.cfg.ai_brain_enabled else
            "Disabled — using strategy score only."
        )
        study["ranked"] = [
            {"symbol": sig.symbol, "direction": sig.direction,
             "contract_type": sig.contract_type or sig.direction,
             "strategy_score": sig.score, "ai_score": sig.ai_score,
             "note": sig.ai_note, "policy": sig.strategy_variant,
             "duration": sig.selected_duration or self.cfg.duration,
             "duration_unit": (sig.selected_duration_unit
                               if sig.selected_duration is not None
                               else self.cfg.duration_unit),
             "duration_source": duration_choices.get(
                 sig.symbol, {}).get("source", "configured fallback"),
             "duration_study": duration_choices.get(
                 sig.symbol, {}).get("study", [])}
            for sig in ranked[:3]
        ]
        study["enabled"] = self.cfg.ai_brain_enabled
        study["model_advisor"] = self.ai_brain.model_advisor.status()
        STATE.set_brain(study)
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
            order_type = candidate.contract_type or candidate.direction
            if order_type not in offered:
                log.info("Skipping %s: %s is not offered (available: %s)",
                         candidate.symbol, order_type,
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
            self.storage.mark_shadow_selected(candidate.shadow_id)
            opened += 1
            self._refresh_trades()
            self._publish_open_contracts()
            STATE.set_risk(self.risk.status())

        if opened:
            log.info("Opened %d contract(s) this scan; now %d/%d open",
                     opened, self.storage.count_open_trades(),
                     self.cfg.max_open_contracts)

    # -- monitor and publish every open contract --------------------------
    def _monitor_open_contracts(self) -> None:
        before = self.storage.count_open_trades()
        super()._monitor_open_contracts()
        after = self.storage.count_open_trades()
        if after != before:
            self._refresh_trades()
        self._publish_open_contracts()
        STATE.set_risk(self.risk.status())

    # -- publication helpers ----------------------------------------------
    def _publish_open_contracts(self) -> None:
        rows = []
        for rec in self.storage.get_open_trades():
            opened = rec.get("opened_at")
            entry_ts: Optional[float] = None
            if opened:
                try:
                    entry_ts = datetime.fromisoformat(opened).timestamp()
                except ValueError:
                    entry_ts = None
            unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(
                rec.get("duration_unit", "m"), 60)
            duration_s = int(rec.get("duration") or self.cfg.duration) * unit
            rows.append({
                "contract_id": rec.get("contract_id"),
                "symbol": rec.get("symbol"),
                "index_value": rec.get("index_value"),
                "direction": rec.get("direction"),
                "stake": rec.get("stake"),
                "payout": rec.get("payout"),
                "buy_price": rec.get("buy_price"),
                "duration": rec.get("duration"),
                "duration_unit": rec.get("duration_unit"),
                "entry_time": opened,
                "entry_ts": entry_ts,
                "expires_ts": (entry_ts + duration_s) if entry_ts else None,
                "entry_spot": rec.get("entry_spot"),
                "signal_score": rec.get("signal_score"),
            })
        STATE.set_open_contracts(rows)

    def _refresh_trades(self) -> None:
        try:
            STATE.set_trades(self.storage.recent_trades(200))
        except Exception as exc:
            log.debug("Could not refresh trades: %s", exc)

    def _publish_scanner_skipped(self, reason: str) -> None:
        """Keep last-known prices but mark every row as risk-blocked."""
        rows = []
        for idx, symbol in self.market.symbol_map.items():
            rows.append(self._row_errored(idx, symbol, f"risk: {reason}"))
        if rows:
            STATE.set_scanner(rows)

    def _row_errored(self, idx: int, symbol: str, reason: str) -> Dict[str, Any]:
        return {
            "index_value": idx,
            "symbol": symbol,
            "price": None, "ema_fast": None, "ema_mid": None,
            "ema_slow": None, "rsi": None, "support": None,
            "resistance": None, "score": 0, "direction": "NONE",
            "trend_1m": "sideways", "trend_1h": "sideways",
            "actionable": False, "reasons": [reason], "status": reason,
        }

    def _row(self, idx: int, symbol: str, sig: Signal,
             repeat: bool) -> Dict[str, Any]:
        status = "ready"
        if sig.actionable:
            status = "signal"
        elif repeat:
            status = "duplicate candle — already traded"
        else:
            blocked = [r for r in sig.reasons if r.startswith("BLOCKED")]
            if blocked:
                status = blocked[0].replace("BLOCKED: ", "")
            elif "sideways" in sig.trend_1m:
                status = "no trend — sideways"
            elif sig.score:
                status = f"score {sig.score} below threshold"
            else:
                status = "no setup"
        return {
            "index_value": idx,
            "symbol": symbol,
            "price": sig.price,
            "ema_fast": sig.ema_fast,
            "ema_mid": sig.ema_mid,
            "ema_slow": sig.ema_slow,
            "rsi": sig.rsi_value,
            "support": sig.support,
            "resistance": sig.resistance,
            "score": sig.score,
            "ai_score": sig.ai_score,
            "ai_note": sig.ai_note,
            "strategy_variant": sig.strategy_variant,
            "strategy_id": sig.strategy_id,
            "contract_type": sig.contract_type or sig.direction,
            "duration": sig.selected_duration,
            "duration_unit": sig.selected_duration_unit,
            "direction": sig.direction,
            "trend_1m": sig.trend_1m,
            "trend_1h": sig.trend_1h,
            "actionable": bool(sig.actionable and not repeat),
            "reasons": list(sig.reasons),
            "status": status,
            "candle_epoch": sig.candle_epoch,
        }


# ---------------------------------------------------------------------------
# Lifecycle supervisor
# ---------------------------------------------------------------------------
class BotService:
    """
    Owns at most one running bot. All lifecycle calls are serialised.

    States: not_configured -> starting -> running <-> paused -> stopping ->
    stopped, with ``error`` for a failed start.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bot: Optional[WebBot] = None
        self._thread: Optional[threading.Thread] = None
        self._pause = threading.Event()
        self._running = False
        self._cfg: Optional[Config] = None
        self._backtest_thread: Optional[threading.Thread] = None

    # -- introspection ----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def config_state(self) -> Dict[str, Any]:
        """
        Report whether credentials exist — WITHOUT reading or returning them.

        Only booleans leave this function. The token value is never copied
        into a local that could be logged, and never crosses into a response.
        """
        import os

        # .env is normally loaded as a side effect of load_config(). The
        # startup banner runs before any config load, so without this the
        # panel would report "NOT CONFIGURED" even when .env is present.
        # load_dotenv(override=False) never clobbers a real environment var.
        try:
            from config import PROJECT_ROOT, load_dotenv
            env_path = PROJECT_ROOT / ".env"
            if load_dotenv is not None and env_path.exists():
                load_dotenv(str(env_path), override=False)
        except Exception:
            pass

        has_app_id = bool((os.getenv("DERIV_APP_ID") or "").strip())
        has_token = bool((os.getenv("DERIV_API_TOKEN") or "").strip())
        dotenv_present = False
        try:
            from config import PROJECT_ROOT
            dotenv_present = (PROJECT_ROOT / ".env").exists()
        except Exception:
            pass
        return {
            "configured": has_app_id and has_token,
            "app_id_present": has_app_id,
            "token_present": has_token,
            "dotenv_present": dotenv_present,
        }

    # -- start ------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Bot is already running."}

            STATE.clear_runtime()

            # Load config. Missing credentials is a normal, expected state.
            try:
                cfg = load_config(require_credentials=True)
            except ConfigError as exc:
                msg = str(exc)
                log.warning("Cannot start: %s", msg)
                STATE.set_status(NOT_CONFIGURED, "Not configured.", error=msg)
                return {"ok": False, "error": msg, "not_configured": True}

            ensure_data_dir(cfg)
            attach_ui_logging(cfg.log_level)
            self._cfg = cfg

            STATE.set_status(STARTING, "Connecting to Deriv …")
            self._pause.clear()
            self._running = True

            bot = WebBot(cfg, self._pause)
            self._bot = bot

            def _run() -> None:
                try:
                    if not bot.startup():
                        # startup() already published a specific error.
                        STATE.set_status(
                            ERROR,
                            STATE.status_detail or "Startup failed.",
                            error=STATE.error,
                        )
                        return
                    STATE.set_status(RUNNING, "Trading loop active.")
                    bot.run()
                except Exception as exc:
                    log.exception("Bot thread crashed: %s", exc)
                    STATE.set_status(ERROR, "Bot crashed.", error=str(exc))
                finally:
                    with self._lock:
                        self._running = False
                    if STATE.status not in (ERROR, NOT_CONFIGURED):
                        STATE.set_status(STOPPED, "Stopped.")

            self._thread = threading.Thread(
                target=_run, name="bot-loop", daemon=True
            )
            self._thread.start()

        # Give the connection a moment so a bad token surfaces immediately
        # rather than as a spinner in the UI.
        for _ in range(120):
            if STATE.status in (RUNNING, ERROR, NOT_CONFIGURED):
                break
            time.sleep(0.25)

        if STATE.status in (ERROR, NOT_CONFIGURED):
            return {
                "ok": False,
                "error": STATE.error or "Could not start the bot.",
                "not_configured": STATE.status == NOT_CONFIGURED,
            }
        return {"ok": True}

    # -- stop -------------------------------------------------------------
    def stop(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            if not self._running or self._bot is None:
                STATE.set_status(STOPPED, "Already stopped.")
                return {"ok": True, "already_stopped": True}
            bot = self._bot
            thread = self._thread

        if timeout is None:
            unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "t": 2}
            pending = bot.storage.get_open_trades()
            contract_seconds = max((
                int(trade.get("duration") or bot.cfg.duration)
                * unit_seconds.get(trade.get("duration_unit") or
                                    bot.cfg.duration_unit, 60)
                for trade in pending
            ), default=(int(bot.cfg.duration)
                        * unit_seconds.get(bot.cfg.duration_unit, 60)))
            # A scan already in progress may open a 300-second managed
            # multiplier after this snapshot, so keep the join window large
            # enough for that newly opened position to be closed and recorded.
            timeout = max(contract_seconds + 120, 420)

        STATE.set_status(STOPPING, "Stopping — finishing open contract …")
        self._pause.clear()
        bot._running = False

        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                STATE.set_status(
                    ERROR,
                    "Stop timed out.",
                    error=("The bot thread did not exit within "
                           f"{timeout:.0f}s. It is still monitoring a contract. "
                           "Re-check before starting again."),
                )
                return {"ok": False, "error": "Stop timed out; bot still running."}

        # bot.run()'s finally block called shutdown(); make it idempotent.
        try:
            bot.shutdown()
        except Exception:
            pass

        with self._lock:
            self._running = False
            self._bot = None
            self._thread = None

        STATE.set_open_contract(None)
        STATE.set_status(STOPPED, "Stopped by operator.")
        log.info("Bot stopped by operator")
        return {"ok": True}

    # -- pause / resume ---------------------------------------------------
    def pause(self) -> Dict[str, Any]:
        if not self._running:
            return {"ok": False, "error": "Bot is not running."}
        self._pause.set()
        STATE.set_status(PAUSED, "Paused — no new trades. Connection kept.")
        log.info("Paused by operator")
        return {"ok": True}

    def resume(self) -> Dict[str, Any]:
        if not self._running:
            return {"ok": False, "error": "Bot is not running."}
        self._pause.clear()
        STATE.set_status(RUNNING, "Resumed.")
        log.info("Resumed by operator")
        return {"ok": True}

    # -- manual reset of the loss pause -----------------------------------
    def reset_risk(self) -> Dict[str, Any]:
        """
        Clear the consecutive-loss counter and any pause window.

        This is the same operator action the CLI exposes. It does NOT reset
        the daily P/L or the daily trade count — those are facts about the
        day and resetting them would defeat the daily loss limit.
        """
        cfg = self._cfg
        try:
            if cfg is None:
                cfg = load_config(require_credentials=False)
            st = Storage(cfg.db_path, cfg.csv_path, cfg.trading_day_timezone)
            before = st.get_consecutive_losses()
            from risk_manager import RiskManager
            RiskManager(cfg, st).manual_reset()
            after = st.get_consecutive_losses()
            STATE.set_risk(RiskManager(cfg, st).status())
            st.close()
        except Exception as exc:
            log.error("Manual reset failed: %s", exc)
            return {"ok": False, "error": f"Reset failed: {exc}"}

        log.warning(
            "Operator manually reset the loss pause (consecutive losses %d -> %d)",
            before, after,
        )
        if self._bot is not None:
            STATE.set_risk(self._bot.risk.status())
        return {"ok": True, "consecutive_losses_before": before,
                "consecutive_losses_after": after}

    # -- backtest ---------------------------------------------------------
    def start_backtest(self, symbols: Optional[List[str]] = None,
                       count: int = 1000, payout: float = 1.95) -> Dict[str, Any]:
        if STATE.backtest_running:
            return {"ok": False, "error": "A backtest is already running."}

        symbols = symbols or ["R_10", "R_25", "R_50", "R_75", "R_100"]
        STATE.set_backtest(None, running=True)
        log.info("Backtest started: %s (count=%d, payout=%.2f)",
                 ",".join(symbols), count, payout)

        def _run() -> None:
            try:
                payload = run_backtest(symbols, count, payout)
                STATE.set_backtest(payload, running=False)
                log.info("Backtest finished: %d trade(s)",
                         payload.get("total_trades", 0))
            except Exception as exc:
                log.exception("Backtest failed: %s", exc)
                STATE.set_backtest(
                    {"error": str(exc), "finished_at": time.time()},
                    running=False,
                )

        self._backtest_thread = threading.Thread(
            target=_run, name="backtest", daemon=True
        )
        self._backtest_thread.start()
        return {"ok": True, "started": True}

    # -- trades -----------------------------------------------------------
    def trades_csv_path(self) -> Optional[str]:
        cfg = self._cfg
        try:
            if cfg is None:
                cfg = load_config(require_credentials=False)
            st = Storage(cfg.db_path, cfg.csv_path, cfg.trading_day_timezone)
            path = st.export_csv()
            st.close()
            return path
        except Exception as exc:
            log.warning("Could not export trades CSV: %s", exc)
            return None

    def backtest_csv_path(self) -> Optional[str]:
        cfg = self._cfg
        try:
            if cfg is None:
                cfg = load_config(require_credentials=False)
            return getattr(cfg, "_last_backtest_csv", None) or "data/backtest_report.csv"
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Backtest runner (uses the unmodified Backtester)
# ---------------------------------------------------------------------------
def run_backtest(symbols: List[str], count: int = 1000,
                 payout: float = 1.95) -> Dict[str, Any]:
    """
    Run the existing Backtester against public candle data.

    No credentials are needed: ``backtest._fetch_public`` uses the public
    WebSocket endpoint, which requires no account.
    """
    from backtest import Backtester, _fetch_public
    from storage import payout_estimates_from_db

    cfg = load_config(require_credentials=False)
    bt = Backtester(cfg, payout_multiple=payout)

    results = []
    market_rows = []
    for sym in symbols:
        try:
            c1m = _fetch_public(sym, 60, count)
            c5m = _fetch_public(sym, 300, count)
            c1h = _fetch_public(sym, 3600, count)
        except Exception as exc:
            market_rows.append({
                "symbol": sym, "error": str(exc),
                "trades": 0, "win_rate": None, "net_profit": None,
            })
            continue

        if len(c1m) < cfg.ema_slow + 10:
            market_rows.append({
                "symbol": sym, "error": f"only {len(c1m)} 1m candles",
                "trades": 0, "win_rate": None, "net_profit": None,
            })
            continue

        estimate = payout_estimates_from_db(cfg.db_path, sym).get(1, {})
        observed = int(estimate.get("samples", 0)) >= 8
        effective_payout = (float(estimate["payout_multiple"])
                            if observed else payout)
        res = bt.run(
            sym, c1m, c5m, c1h, payout_multiple=effective_payout,
            payout_source=("recent settled demo wins" if observed
                           else "configured fallback"),
            payout_samples=int(estimate.get("samples", 0)),
        )
        results.append(res)
        s = res.summary()
        # BacktestResult.summary() already reports win_rate_pct in percent.
        market_rows.append({
            "symbol": sym,
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": s["win_rate_pct"] if s["trades"] else None,
            "net_profit": s["net_profit"],
            "return_pct": s["return_pct"],
            "max_drawdown": s["max_drawdown"],
            "longest_losing_streak": s["longest_losing_streak"],
            "profit_factor": round(s["profit_factor"], 2)
            if s["trades"] else None,
            "payout_multiple": effective_payout,
            "payout_samples": int(estimate.get("samples", 0)),
            "payout_source": ("recent settled demo wins" if observed
                              else "configured fallback"),
            "bars_1m": len(c1m),
            "error": None,
        })

    if not results:
        raise RuntimeError("No market produced a backtest result.")

    csv_path = bt.export_csv(results, "data/backtest_report.csv")

    total_trades = sum(r.n for r in results)
    total_wins = sum(r.wins for r in results)
    total_net = round(sum(r.net_profit for r in results), 2)
    start = min(r.start_epoch for r in results)
    end = max(r.end_epoch for r in results)

    # Breakeven win rate for the assumed payout — the honest yardstick.
    breakeven = round(sum((1.0 / r.payout_multiple) * r.n
                          for r in results) / max(1, total_trades) * 100, 2)

    return {
        "markets": market_rows,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_trades - total_wins,
        "overall_win_rate": round(total_wins / total_trades * 100, 2)
        if total_trades else None,
        "net_profit": total_net,
        "payout_multiple": payout,
        "payout_multiples_by_market": {
            r.symbol: {"multiple": r.payout_multiple,
                       "source": r.payout_source,
                       "samples": r.payout_samples}
            for r in results
        },
        "breakeven_win_rate": breakeven,
        "period_start": datetime.fromtimestamp(start, timezone.utc)
        .strftime("%Y-%m-%d %H:%M"),
        "period_end": datetime.fromtimestamp(end, timezone.utc)
        .strftime("%Y-%m-%d %H:%M"),
        "csv_path": csv_path,
        "finished_at": time.time(),
        "assumptions": [
            "Entry at the NEXT bar's open — no look-ahead.",
            "1-minute expiry; a tie counts as a LOSS.",
            "Payout per market uses recent settled demo wins when at least 8 "
            f"observations exist; otherwise uses the {payout}x configured fallback.",
            "Spread and slippage are NOT modelled.",
            "Synthetic indices have no order book, so fills are assumed exact.",
            "Position sizing is a flat stake; compounding is not simulated.",
            "A backtest describes one history. It is not a forecast, and a "
            "small sample is not evidence of an edge.",
        ],
        "limitations": [
            "Survivorship: the symbols that exist TODAY are the ones tested.",
            f"Sample: {total_trades} trades is a small sample; the standard "
            f"error on a win rate near {breakeven}% is large.",
            "Regime: the window is short (see period above) and may contain "
            "only one market regime.",
            "Execution: real proposal prices widen on volatile ticks; the "
            "modelled payout is optimistic.",
        ],
    }


SERVICE = BotService()
