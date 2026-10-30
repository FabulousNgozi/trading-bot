"""Optional OpenAI ranking advisor for already-approved demo signals.

The model may order or skip the supplied candidates. It cannot create a
direction, change a stake or expiry, place orders, or bypass local risk checks.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import requests

from config import Config
from logger_config import get_logger
from strategy import Signal

log = get_logger("openai_advisor")


class OpenAIAdvisor:
    ENDPOINT = "https://api.openai.com/v1/responses"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._last_request_at = 0.0
        self._status = self._initial_status()

    def _initial_status(self) -> Dict[str, str]:
        if not self.cfg.openai_advisor_enabled:
            return {"state": "disabled", "detail": "OpenAI advisor is disabled."}
        if not self.cfg.openai_api_key:
            return {"state": "missing_key", "detail": "Set OPENAI_API_KEY to enable the model advisor."}
        return {"state": "ready", "detail": f"OpenAI advisor ready ({self.cfg.openai_model})."}

    def status(self) -> Dict[str, str]:
        return dict(self._status)

    def rank(self, signals: List[Signal], context: Dict[str, Dict[str, Any]]
             ) -> Tuple[List[Signal], Dict[str, str]]:
        """Return model ordering when valid; otherwise preserve local ordering."""
        if not signals:
            self._status = {"state": "no_candidates", "detail": "No approved signals for OpenAI review."}
            return signals, self.status()
        if not self.cfg.openai_advisor_enabled or not self.cfg.openai_api_key:
            self._status = self._initial_status()
            return signals, self.status()

        now = time.monotonic()
        remaining = self.cfg.openai_min_interval_seconds - (now - self._last_request_at)
        if remaining > 0:
            self._status = {"state": "throttled",
                            "detail": f"Using local ranking; next model review in {int(remaining)}s."}
            return signals, self.status()

        candidates = []
        for signal in signals:
            candidates.append({
                "symbol": signal.symbol,
                "direction": signal.direction,
                "contract_type": signal.contract_type or signal.direction,
                "strategy_id": signal.strategy_id,
                "strategy_profile": signal.strategy_variant,
                "confluence_score": signal.score,
                "local_historical_score": signal.ai_score,
                "duration_minutes": (signal.selected_duration
                    if signal.selected_duration_unit == "m" else
                    max(1, round((signal.selected_duration or 60) / 60))),
                "trend_1m": signal.trend_1m,
                "trend_1h": signal.trend_1h,
                "rsi": signal.rsi_value,
                "price_action": signal.price_action,
                "local_history": context.get(signal.symbol, {}),
            })
        payload = {
            "task": "Rank these already-approved demo trade candidates by strength of supplied evidence, or skip all if evidence is weak or conflicting.",
            "constraints": [
                "Only use supplied data; do not claim certainty or invent market facts.",
                "Confluence scores are not win probabilities.",
                "Do not alter direction, contract type, stake, duration, or risk limits.",
                "Return only candidate symbols from the supplied list, ordered best first.",
                "A skip decision must return an empty symbol list.",
                "These are synthetic indices; price patterns may have no predictive edge.",
            ],
            "candidates": candidates,
        }
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["rank", "skip"]},
                "ranked_symbols": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            "required": ["decision", "ranked_symbols", "summary"],
            "additionalProperties": False,
        }
        request_body = {
            "model": self.cfg.openai_model,
            "store": False,
            "max_output_tokens": 220,
            "input": [
                {"role": "system", "content": (
                    "You are a cautious market-signal reviewer for a demo-only bot. "
                    "Use only the supplied candidate evidence. Never imply certainty. "
                    "Return the requested structured decision; do not produce trading instructions beyond ranking or skipping."
                )},
                {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
            ],
            "text": {"format": {"type": "json_schema", "name": "candidate_review",
                                  "strict": True, "schema": schema}},
        }
        self._last_request_at = now
        try:
            response = requests.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self.cfg.openai_api_key}",
                         "Content-Type": "application/json"},
                json=request_body,
                timeout=self.cfg.openai_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "completed":
                raise ValueError(f"response status was {body.get('status', 'unknown')}")
            output = body.get("output_text")
            if not output:
                output = self._extract_output_text(body)
            if not output:
                raise ValueError("response contained no structured output")
            result = json.loads(output)
            return self._apply_result(signals, result)
        except Exception as exc:
            self._status = {"state": "fallback",
                            "detail": f"OpenAI review failed; local ranking used ({type(exc).__name__})."}
            log.warning("OpenAI advisor unavailable; preserving local ranking (%s)",
                        type(exc).__name__)
            return signals, self.status()

    @staticmethod
    def _extract_output_text(body: Dict[str, Any]) -> str:
        chunks = []
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    chunks.append(part["text"])
                elif part.get("type") == "refusal":
                    raise ValueError("model refused the candidate review")
        return "".join(chunks)

    def _apply_result(self, signals: List[Signal], result: Dict[str, Any]
                      ) -> Tuple[List[Signal], Dict[str, str]]:
        known = {signal.symbol: signal for signal in signals}
        decision = result.get("decision")
        symbols = result.get("ranked_symbols")
        if decision not in ("rank", "skip") or not isinstance(symbols, list):
            raise ValueError("model output did not match the expected decision")
        if decision == "skip":
            if symbols:
                raise ValueError("skip decision included candidate symbols")
            self._status = {"state": "skipped",
                            "detail": str(result.get("summary", "OpenAI skipped all candidates."))[:240]}
            return [], self.status()
        if not symbols or any(not isinstance(symbol, str) or symbol not in known
                              for symbol in symbols):
            raise ValueError("model returned an unknown or empty candidate ranking")
        ordered = []
        seen = set()
        for symbol in symbols:
            if symbol not in seen:
                ordered.append(known[symbol])
                seen.add(symbol)
        ordered.extend(signal for signal in signals if signal.symbol not in seen)
        summary = str(result.get("summary", "Model-ranked approved candidates."))[:240]
        self._status = {"state": "active", "detail": summary}
        log.info("OpenAI advisor ranked %d approved signal(s): %s",
                 len(ordered), summary)
        return ordered, self.status()
