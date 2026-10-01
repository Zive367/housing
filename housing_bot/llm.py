"""Thin wrapper around the Claude API: structured outputs, refusal fallback, spend tracking + daily cap."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Config
from .store import Store

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output). Anthropic list prices, Sept 2026.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Models that take output_config.effort and the server-side refusal fallback ("default" form).
SUPPORTS_EFFORT_AND_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Config, store: Store, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.store = store
        self.client = client or anthropic.Anthropic(
            api_key=cfg.secrets.anthropic_api_key or None, max_retries=3, timeout=180.0)

    def model_for(self, bulk: bool) -> str:
        return self.cfg.llm.bulk_model if bulk else self.cfg.llm.model

    def check_budget(self) -> None:
        spent = self.store.spend_today()
        if spent >= self.cfg.llm.daily_budget_usd:
            raise BudgetExceeded(f"daily Claude budget used up (${spent:.2f})")

    def _base_kwargs(self, model: str, effort: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model}
        if model in SUPPORTS_EFFORT_AND_FALLBACK:
            # Opt into Anthropic's recommended fallback model if a safety classifier declines.
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
            if effort:
                kwargs["output_config"] = {"effort": effort}
        return kwargs

    def _record(self, model: str, usage: Any) -> None:
        price_in, price_out = PRICES.get(model, (4.0, 20.0))
        fresh = getattr(usage, "input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        usd = (fresh * price_in + written * price_in * 1.25 + read * price_in * 0.1 + out * price_out) / 1e6
        self.store.add_usage(model, usd, fresh + written + read, out)

    def parse(self, schema: type[T], system: str, user: str | list, *, bulk: bool = False,
              effort: str | None = "low", max_tokens: int = 8000) -> T:
        """One request, one validated pydantic object back."""
        self.check_budget()
        model = self.model_for(bulk)
        kwargs = self._base_kwargs(model, effort)
        try:
            response = self.client.beta.messages.parse(
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                **kwargs,
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e
        self._record(model, response.usage)
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude output was cut off (max_tokens)")
        if response.parsed_output is None:
            raise LLMError("Claude returned no structured output")
        return response.parsed_output

    def create(self, *, effort: str | None = "low", **request: Any) -> Any:
        """Raw beta messages.create for tool loops (the browser agent). Tracks spend like parse()."""
        self.check_budget()
        model = request.pop("model", None) or self.cfg.llm.model
        kwargs = self._base_kwargs(model, effort) | request
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e
        self._record(model, response.usage)
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined the request")
        return response
