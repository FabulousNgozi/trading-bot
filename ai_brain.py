"""Bounded strategy learner and outcome-based signal ranker.

It studies settled demo trades, records which strategy variant produced each
trade, and selects between a small fixed set of strategy profiles. It never
changes money management or the risk/execution gates.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from logger_config import get_logger
from openai_advisor import OpenAIAdvisor
from storage import Storage
from strategy import Signal, Strategy

log = get_logger("ai_brain")


class AIBrain:
    """Learn strategy profile performance from resolved demo outcomes."""

    PRIOR_TRADES = 20.0
    MAX_ADJUSTMENT = 8.0
    MIN_PROFILE_TRADES = 20
    EXPERIMENT_EVERY_SCANS = 40
    MIN_SETUP_TRADES = 8
    PROFILES = ("baseline", "selective", "wide_momentum")

    def __init__(self, cfg: Config, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self.model_advisor = OpenAIAdvisor(cfg)
        self._selection_counter = 0
        self._duration_lock = threading.Lock()
        self._duration_research_active = False
        self._duration_thread: Optional[threading.Thread] = None
        self._duration_queue = deque()
        self._duration_queued = set()
        self._duration_active_key = None
        self._closing = False

    def select_strategy(self, strategy_class: type = Strategy
                        ) -> Tuple[str, Strategy, Dict[str, object]]:
        """Choose a bounded profile and return it with explainable research stats."""
        self._selection_counter += 1
        strategy_id = getattr(strategy_class, "strategy_id", "trend_confluence_v1")
        stats = self.storage.resolved_policy_performance(strategy_id)
        counts = {name: int(stats.get(name, {}).get("trades", 0))
                  for name in self.PROFILES}
        under_studied = [name for name in self.PROFILES
                         if counts[name] < self.MIN_PROFILE_TRADES]

        if under_studied:
            least_seen = min(counts[name] for name in under_studied)
            choices = [name for name in under_studied
                       if counts[name] == least_seen]
            profile = choices[(self._selection_counter - 1) % len(choices)]
            phase = "learning"
        elif self._selection_counter % self.EXPERIMENT_EVERY_SCANS == 0:
            # Keep a small, occasional demo-only experiment running so the
            # selected profile can be challenged as new results accumulate.
            least_seen = min(counts.values())
            choices = [name for name in self.PROFILES
                       if counts[name] == least_seen]
            profile = choices[(self._selection_counter // self.EXPERIMENT_EVERY_SCANS)
                              % len(choices)]
            phase = "periodic experiment"
        else:
            profile = max(
                self.PROFILES,
                key=lambda name: self._selection_value(stats.get(name, {})),
            )
            phase = "selected from resolved results"

        report: Dict[str, object] = {
            "enabled": True,
            "selected_policy": profile,
            "phase": phase,
            "status": self._status(profile, phase, counts),
            "policy_stats": self._format_stats(stats),
            "price_action_stats": self._format_price_action_stats(),
            "shadow_stats": self._format_shadow_stats(),
            "martingale_comparison": self.storage.martingale_vs_flat_performance(),
            "model_advisor": self.model_advisor.status(),
        }
        log.info("AI strategy study: profile=%s phase=%s settled=%s",
                 profile, phase, counts)
        return profile, strategy_class(self._profile_config(profile)), report

    def research_state(self, strategy_id: str = "trend_confluence_v1"
                       ) -> Dict[str, object]:
        """Summarize the learned profiles without changing the selection state."""
        stats = self.storage.resolved_policy_performance(strategy_id)
        return {
            "policy_stats": self._format_stats(stats),
            "sample_target": self.MIN_PROFILE_TRADES,
            "price_action_stats": self._format_price_action_stats(),
            "shadow_stats": self._format_shadow_stats(),
            "martingale_comparison": self.storage.martingale_vs_flat_performance(),
            "model_advisor": self.model_advisor.status(),
        }

    def _format_price_action_stats(self) -> List[Dict[str, object]]:
        """Label per-market setups from settled trade evidence, with a sample floor."""
        result = []
        for row in self.storage.resolved_price_action_performance():
            trades, wins = int(row["trades"]), int(row["wins"])
            rate = 100 * wins / trades if trades else 0.0
            net = float(row["flat_net_profit"])
            result.append({**row, "trades": trades, "wins": wins,
                "win_rate": round(rate, 1), "net_profit": round(float(row["net_profit"]), 2),
                "flat_net_profit": round(net, 2),
                "average_win": round(float(row.get("average_win", 0.0)), 2),
                "average_loss": round(float(row.get("average_loss", 0.0)), 2),
                "max_losing_streak": int(row.get("max_losing_streak", 0)),
                "assessment": ("positive sample" if trades >= self.MIN_SETUP_TRADES and net > 0
                               else "negative sample" if trades >= self.MIN_SETUP_TRADES and net <= 0
                               else f"learning ({trades}/{self.MIN_SETUP_TRADES})")})
        result.sort(key=lambda item: (item["trades"], item["net_profit"]), reverse=True)
        return result[:30]

    def _format_shadow_stats(self) -> List[Dict[str, object]]:
        result = self.storage.shadow_signal_performance()
        for row in result:
            n = int(row["trades"])
            row["assessment"] = "directional sample" if n >= 20 else f"learning ({n}/20)"
        return result[:30]

    def select_duration(self, symbol: str, series: Dict[str, object],
                        strategy: Strategy, strategy_variant: str
                        ) -> Dict[str, object]:
        """Use same-market validation results, then settled trades, to choose expiry."""
        default = {"duration": self.cfg.duration,
                   "duration_unit": self.cfg.duration_unit,
                   "source": "configured fallback",
                   "note": "No reliable duration comparison yet."}
        strategy_id = getattr(strategy, "strategy_id", "trend_confluence_v1")
        if strategy_id == "boom_crash_spike_fade_v1":
            return {
                "duration": 300, "duration_unit": "s",
                "source": "multiplier auto-management",
                "note": "Positions close at +/-50% of stake or after 300 seconds.",
            }
        if not self.cfg.ai_brain_enabled or self.cfg.duration_unit != "m":
            return default

        s1m = series.get("1m")
        s5m = series.get("5m")
        s1h = series.get("1h")
        if s1m is None or s5m is None or s1h is None:
            return default
        candles_1m = s1m.completed()
        candles_5m = s5m.completed()
        candles_1h = s1h.completed()
        if len(candles_1m) < self.cfg.ema_slow + 100:
            return default

        latest_epoch = candles_1m[-1].epoch
        cached = self.storage.get_duration_study(
            symbol, strategy_id, strategy_variant
        )
        cached_end = max((int(row["data_end_epoch"]) for row in cached), default=0)
        if latest_epoch > cached_end:
            self._schedule_duration_research(
                symbol, strategy_id, strategy_variant, latest_epoch,
                candles_1m, candles_5m, candles_1h, strategy,
            )

        fresh = [row for row in cached
                 if latest_epoch - int(row["data_end_epoch"]) <= 300]
        if not fresh:
            return default

        live = self.storage.resolved_duration_performance(
            symbol, strategy_id, strategy_variant
        )
        live_ready = {
            duration: row for duration, row in live.items()
            if int(row["trades"]) >= 12 and float(row["flat_net_profit"]) > 0
        }
        if live_ready:
            duration = max(
                live_ready,
                key=lambda d: (float(live_ready[d]["flat_net_profit"]) /
                    max(self.cfg.stake,
                        float(live_ready[d].get("flat_max_drawdown", 0.0))),
                    float(live_ready[d]["flat_net_profit"]), -d),
            )
            row = live_ready[duration]
            return {
                "duration": duration, "duration_unit": "m",
                "source": "settled trades",
                "note": (f"{row['wins']}/{row['trades']} settled wins; "
                         f"flat-stake P/L {row['flat_net_profit']:+.2f}; "
                         f"drawdown {row.get('flat_max_drawdown', 0.0):.2f}"),
            }

        study_by_duration = {int(row["duration_minutes"]): row for row in fresh}
        eligible = {}
        for duration, row in study_by_duration.items():
            n = int(row["validation_trades"])
            train_n = int(row["train_trades"])
            if (n < 12 or train_n < 8
                    or float(row["train_net_profit"]) <= 0
                    or float(row["validation_net_profit"]) <= 0
                    or float(row["validation_drawdown"]) > max(
                        self.cfg.stake * 3,
                        float(row["validation_net_profit"]) * 1.5,
                    )):
                continue
            eligible[duration] = row

        if not eligible:
            return {
                **default,
                "source": "backtest fallback",
                "note": "No duration passed the train/validation sample and return checks.",
                "study": self._public_duration_stats(fresh),
            }

        duration = max(eligible, key=lambda d: (
            float(eligible[d]["validation_net_profit"]) /
            max(self.cfg.stake, float(eligible[d]["validation_drawdown"])),
            float(eligible[d]["validation_net_profit"]), -d))
        baseline = study_by_duration.get(self.cfg.duration)
        selected = eligible[duration]
        selected_efficiency = float(selected["validation_net_profit"]) / max(
            self.cfg.stake, float(selected["validation_drawdown"]))
        baseline_efficiency = (
            float(baseline["validation_net_profit"]) / max(
                self.cfg.stake, float(baseline["validation_drawdown"]))
            if baseline and float(baseline.get("validation_trades", 0)) >= 12
            else None
        )
        if (duration != self.cfg.duration and baseline_efficiency is not None
                and selected_efficiency < baseline_efficiency):
            return {
                **default,
                "source": "backtest fallback",
                "note": "Alternative did not beat the configured duration on return relative to drawdown.",
                "study": self._public_duration_stats(fresh),
            }
        return {
            "duration": duration, "duration_unit": "m",
            "source": "walk-forward backtest",
            "note": (f"validation {selected['validation_win_rate']:.1f}% over "
                     f"{selected['validation_trades']} trades; "
                     f"P/L {selected['validation_net_profit']:+.2f}; "
                     f"drawdown {selected['validation_drawdown']:.2f}; "
                     f"payout {selected.get('payout_multiple', 1.95):.2f}x "
                     f"({selected.get('payout_samples', 0)} observed wins)"),
            "study": self._public_duration_stats(fresh),
        }

    def _schedule_duration_research(self, symbol: str, strategy_id: str,
                                    strategy_variant: str, data_end_epoch: int,
                                    candles_1m: list, candles_5m: list,
                                    candles_1h: list, strategy: Strategy) -> None:
        key = (symbol, strategy_id, strategy_variant)
        with self._duration_lock:
            if self._closing:
                return
            if key == self._duration_active_key:
                return
            if key in self._duration_queued or len(self._duration_queue) >= 50:
                return
            self._duration_queue.append((
                key, data_end_epoch, candles_1m, candles_5m, candles_1h,
                strategy,
            ))
            self._duration_queued.add(key)
            if self._duration_research_active:
                return
            self._duration_research_active = True
            self._duration_thread = threading.Thread(
                target=self._duration_worker, name="duration-study", daemon=True
            )
            self._duration_thread.start()

    def _duration_worker(self) -> None:
        while True:
            with self._duration_lock:
                if self._closing or not self._duration_queue:
                    self._duration_research_active = False
                    self._duration_active_key = None
                    self._duration_thread = None
                    return
                (key, data_end_epoch, candles_1m, candles_5m, candles_1h,
                 strategy) = self._duration_queue.popleft()
                self._duration_queued.discard(key)
                self._duration_active_key = key

            symbol, strategy_id, strategy_variant = key
            try:
                from backtest import Backtester

                tester = Backtester(strategy.cfg, strategy=strategy)
                payout_estimates = self.storage.resolved_payout_estimates(symbol)
                results = tester.run_duration_study(
                    symbol, candles_1m, candles_5m, candles_1h,
                    payout_estimates=payout_estimates,
                )
                self.storage.save_duration_study(
                    symbol, strategy_id, strategy_variant,
                    data_end_epoch, results,
                )
                log.info("Duration backtest updated for %s/%s/%s (%d bars)",
                         symbol, strategy_id, strategy_variant, len(candles_1m))
            except Exception as exc:
                log.warning("Duration backtest failed for %s: %s", symbol, exc)
            finally:
                with self._duration_lock:
                    self._duration_active_key = None

    def close(self) -> None:
        """Finish a pending local study before its SQLite store is closed."""
        with self._duration_lock:
            self._closing = True
            self._duration_queue.clear()
            self._duration_queued.clear()
            thread = self._duration_thread
        if thread is not None and thread.is_alive():
            thread.join()

    @staticmethod
    def _public_duration_stats(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        return [{
            "duration_minutes": int(row["duration_minutes"]),
            "validation_trades": int(row["validation_trades"]),
            "validation_wins": int(row["validation_wins"]),
            "validation_win_rate": float(row["validation_win_rate"]
                                          if "validation_win_rate" in row
                                          else 0.0),
            "validation_net_profit": float(row["validation_net_profit"]),
            "validation_drawdown": float(row.get("validation_drawdown", 0.0)),
            "payout_multiple": float(row.get("payout_multiple", 1.95)),
            "payout_samples": int(row.get("payout_samples", 0)),
        } for row in rows]

    def _profile_config(self, profile: str) -> Config:
        """Make small, predefined changes; confirmation and risk gates stay on."""
        if profile == "selective":
            low = min(99.9, self.cfg.rsi_min + 2.0)
            high = min(100.0, max(low + 0.1, self.cfg.rsi_max - 2.0))
            if high <= low:
                low = max(0.0, high - 0.1)
            return replace(
                self.cfg,
                min_signal_score=min(100, self.cfg.min_signal_score + 8),
                rsi_min=low,
                rsi_max=high,
            )
        if profile == "wide_momentum":
            low = max(0.0, self.cfg.rsi_min - 3.0)
            high = min(100.0, self.cfg.rsi_max + 3.0)
            return replace(
                self.cfg,
                min_signal_score=max(0, self.cfg.min_signal_score - 5),
                rsi_min=low,
                rsi_max=high,
            )
        return self.cfg

    def _selection_value(self, row: Dict[str, float]) -> float:
        """Choose by shrunk flat-stake return, penalized for drawdown."""
        trades = int(row.get("trades", 0))
        if trades <= 0:
            return -1.0
        stake_total = float(row.get("flat_stake_total", 0.0))
        if stake_total <= 0:
            return -1.0
        sample_weight = min(1.0, trades / self.PRIOR_TRADES)
        return_ratio = float(row.get("flat_net_profit", 0.0)) / stake_total
        drawdown_ratio = float(row.get("flat_max_drawdown", 0.0)) / stake_total
        return return_ratio * sample_weight - 0.25 * drawdown_ratio

    @staticmethod
    def _wilson_lower_bound(wins: int, trades: int, z: float = 1.96) -> float:
        if trades <= 0:
            return 0.0
        p = wins / trades
        z2 = z * z
        return ((p + z2 / (2 * trades) - z * math.sqrt(
            (p * (1 - p) + z2 / (4 * trades)) / trades
        )) / (1 + z2 / trades))

    def _format_stats(self, stats: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, object]]:
        result: Dict[str, Dict[str, object]] = {}
        for name in self.PROFILES:
            row = stats.get(name, {})
            trades = int(row.get("trades", 0))
            wins = int(row.get("wins", 0))
            result[name] = {
                "trades": trades,
                "wins": wins,
                "win_rate": round(100 * wins / trades, 1) if trades else None,
                "net_profit": round(float(row.get("net_profit", 0.0)), 2),
                "flat_net_profit": round(float(row.get("flat_net_profit", 0.0)), 2),
                "max_drawdown": round(float(row.get("flat_max_drawdown", 0.0)), 2),
                "average_win": round(float(row.get("average_win", 0.0)), 2),
                "average_loss": round(float(row.get("average_loss", 0.0)), 2),
                "max_losing_streak": int(row.get("max_losing_streak", 0)),
                "profit_factor": (round(float(row["profit_factor"]), 2)
                                  if math.isfinite(float(row.get("profit_factor", 0.0)))
                                  else None),
                "lower_bound": round(self._wilson_lower_bound(wins, trades), 3),
            }
        return result

    def _status(self, selected: str, phase: str,
                counts: Dict[str, int]) -> str:
        if phase == "learning":
            return (f"Learning {selected}: {counts[selected]}/"
                    f"{self.MIN_PROFILE_TRADES} settled demo trades.")
        if phase == "periodic experiment":
            return f"Testing {selected} against the currently selected profile."
        return f"Using {selected}; periodically testing alternatives on demo trades."

    def rank(self, signals: List[Signal]) -> List[Signal]:
        actionable = [signal for signal in signals if signal.actionable]
        if not actionable:
            return []

        history = self.storage.resolved_trade_performance(
            actionable[0].strategy_id
        )
        if actionable[0].strategy_id == "boom_crash_spike_fade_v1":
            normalized = {}
            for key, row in history.items():
                symbol, direction = key.rsplit(":", 1)
                direction = {"MULTUP": "CALL", "MULTDOWN": "PUT"}.get(
                    direction, direction
                )
                normalized[f"{symbol}:{direction}"] = row
            history = normalized
        total_trades = sum(row["trades"] for row in history.values())
        total_wins = sum(row["wins"] for row in history.values())
        baseline = (total_wins / total_trades) if total_trades else 0.5
        total_flat_stake = sum(row.get("base_stake_total", 0.0)
                               for row in history.values())
        baseline_return = (
            sum(row.get("flat_net_profit", 0.0) for row in history.values()) /
            total_flat_stake if total_flat_stake else 0.0
        )
        setup_history = {
            (row["symbol"], row["strategy_id"], row["policy"], row["direction"], row["setup_key"]): row
            for row in self.storage.resolved_price_action_performance()
        }

        def brain_score(signal: Signal) -> float:
            row = history.get(f"{signal.symbol}:{signal.direction}", {})
            count = row.get("trades", 0)
            wins = row.get("wins", 0)
            evidence_stake = float(row.get("base_stake_total", 0.0))
            learned_return = (
                float(row.get("flat_net_profit", 0.0)) +
                self.PRIOR_TRADES * self.cfg.stake * baseline_return
            ) / max(0.01, evidence_stake + self.PRIOR_TRADES * self.cfg.stake)
            adjustment = max(-self.MAX_ADJUSTMENT,
                min(self.MAX_ADJUSTMENT, (learned_return - baseline_return) * 50))
            setup = setup_history.get((signal.symbol, signal.strategy_id,
                signal.strategy_variant, signal.contract_type or signal.direction,
                signal.price_action.get("setup_key")))
            setup_note = "setup learning; "
            if setup and int(setup["trades"]) >= self.MIN_SETUP_TRADES:
                setup_stake = float(setup.get("flat_stake_total", 0.0))
                setup_return = (float(setup.get("flat_net_profit", 0.0)) /
                                setup_stake if setup_stake else baseline_return)
                # Small, bounded influence: descriptive evidence should not overpower
                # the strategy's signal score or the existing risk gates.
                setup_adjustment = max(-2.0, min(2.0,
                    (setup_return - baseline_return) * 20))
                adjustment += setup_adjustment
                setup_note = (f"setup {setup['wins']}/{setup['trades']} wins, "
                              f"P/L {float(setup['net_profit']):+.2f}; ")
            signal.ai_score = round(signal.score + adjustment, 2)
            history_note = (f"market history {wins}/{count} wins"
                            if count else "no resolved market history")
            signal.ai_note = (
                f"{setup_note}{history_note}; adjustment {adjustment:+.1f}"
            )
            return signal.ai_score

        ranked = sorted(actionable, key=brain_score, reverse=True)
        context: Dict[str, Dict[str, Any]] = {}
        for signal in ranked:
            direction = signal.contract_type or signal.direction
            history_row = history.get(f"{signal.symbol}:{direction}", {})
            setup_row = setup_history.get((signal.symbol, signal.strategy_id,
                signal.strategy_variant, direction,
                signal.price_action.get("setup_key")), {})
            context[signal.symbol] = {
                "market_trades": int(history_row.get("trades", 0)),
                "market_wins": int(history_row.get("wins", 0)),
                "market_flat_pnl": round(float(history_row.get("flat_net_profit", 0.0)), 2),
                "setup_trades": int(setup_row.get("trades", 0)),
                "setup_wins": int(setup_row.get("wins", 0)),
                "setup_flat_pnl": round(float(setup_row.get("flat_net_profit", 0.0)), 2),
            }
        ranked, advisor_status = self.model_advisor.rank(ranked, context)
        log.info("Model advisor state=%s detail=%s",
                 advisor_status.get("state"), advisor_status.get("detail"))
        for signal in ranked:
            log.info("Brain ranked %s %s score=%.1f (%s)", signal.symbol,
                     signal.direction, signal.ai_score, signal.ai_note)
        return ranked
