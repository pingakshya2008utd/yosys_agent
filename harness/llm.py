"""Anthropic client wrapper: strict-JSON calls, token accounting, .env loading.

API notes that shaped this module
--------------------------------
* The plan asks for "temperature 0.1 with top-k and nucleus disabled". That is
  NOT possible on current models: `temperature` / `top_p` / `top_k` are removed
  on Claude Opus 5 and Sonnet 5 and return HTTP 400. Determinism instead comes
  from (a) a strict `output_config.format` JSON schema and (b) a fixed
  `effort` level. Documented in README.md.
* Thinking is on by default (adaptive) on Opus 5; we leave it alone.
* Assistant prefill is rejected on these models, so JSON is forced with
  `output_config.format`, not with a `{` prefill.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

import anthropic

# Reasoning roles get Opus; cheap parsing roles get Sonnet (per the plan).
MODEL_REASON = "claude-opus-5"
MODEL_PARSE = "claude-sonnet-5"

# Rates in $/MTok for the cost line in the final report.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
}


def load_dotenv(path: str | pathlib.Path) -> None:
    """Minimal .env loader - avoids a python-dotenv dependency."""
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


class Usage:
    """Thread-safe token/cost accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.by_model: dict[str, dict[str, int]] = {}

    def add(self, model: str, inp: int, out: int, cache_read: int = 0) -> None:
        with self._lock:
            self.calls += 1
            d = self.by_model.setdefault(
                model, {"input": 0, "output": 0, "cache_read": 0}
            )
            d["input"] += inp
            d["output"] += out
            d["cache_read"] += cache_read

    def cost_usd(self) -> float:
        total = 0.0
        for model, d in self.by_model.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            total += d["input"] / 1e6 * pin + d["output"] / 1e6 * pout
            total += d["cache_read"] / 1e6 * pin * 0.1
        return total

    def summary(self) -> dict[str, Any]:
        return {
            "llm_calls": self.calls,
            "by_model": self.by_model,
            "est_cost_usd": round(self.cost_usd(), 4),
        }


class LLM:
    def __init__(self, usage: Usage, enabled: bool = True, max_retries: int = 3):
        self.usage = usage
        self.enabled = enabled
        self.client = (
            anthropic.Anthropic(max_retries=max_retries) if enabled else None
        )

    def json_call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        model: str = MODEL_REASON,
        effort: str = "medium",
        max_tokens: int = 16000,
    ) -> dict[str, Any] | None:
        """One strict-JSON request. Returns None on any failure (callers all
        have a non-LLM fallback path, so the loop never hard-stops on the API).
        """
        if not self.enabled or self.client is None:
            return None
        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.AuthenticationError:
            print("  [llm] authentication failed - check ANTHROPIC_API_KEY")
            self.enabled = False
            return None
        except anthropic.BadRequestError as exc:
            print(f"  [llm] bad request: {exc}")
            return None
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            print(f"  [llm] api error: {exc}")
            return None

        u = resp.usage
        self.usage.add(
            model,
            getattr(u, "input_tokens", 0) or 0,
            getattr(u, "output_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0,
        )

        if resp.stop_reason == "refusal":
            print("  [llm] request refused")
            return None

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print("  [llm] response was not valid JSON")
            return None
