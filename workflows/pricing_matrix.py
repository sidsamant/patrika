"""Pricing matrix and cost calculation utilities for Google Gemini models."""
from __future__ import annotations

from typing import Any

# Pricing per 1M tokens in USD
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro": {
        "input_per_1m": 1.25,
        "output_per_1m": 5.00,
    },
    "gemini-1.5-pro": {
        "input_per_1m": 1.25,
        "output_per_1m": 5.00,
    },
    "gemini-2.5-flash": {
        "input_per_1m": 0.075,
        "output_per_1m": 0.30,
    },
    "gemini-1.5-flash": {
        "input_per_1m": 0.075,
        "output_per_1m": 0.30,
    },
    "gemini-3.5-flash": {
        "input_per_1m": 0.075,
        "output_per_1m": 0.30,
    },
}

DEFAULT_USD_TO_INR_RATE = 85.00

# Claude rates per 1M tokens in USD (Anthropic first-party API). Thinking tokens are billed as
# output. cache_write_per_1m is the 5-minute cache write (1.25x input).
CLAUDE_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input_per_1m": 4.00, "output_per_1m": 20.00,
                        "cache_read_per_1m": 0.20, "cache_write_per_1m": 5.00},
    "claude-sonnet-5-5": {"input_per_1m": 2.00, "output_per_1m": 10.00,
                          "cache_read_per_1m": 0.10, "cache_write_per_1m": 2.50},
    # Possible server-side fallback models (cache rates: 0.1x read, 1.25x write)
    "claude-opus-5": {"input_per_1m": 5.00, "output_per_1m": 25.00,
                      "cache_read_per_1m": 0.50, "cache_write_per_1m": 6.25},
    "claude-opus-4-8": {"input_per_1m": 5.00, "output_per_1m": 25.00,
                        "cache_read_per_1m": 0.50, "cache_write_per_1m": 6.25},
}

# Claude web search server tool: $10 per 1,000 searches, plus the search results as input tokens.
# A search that fails with an error is not billed.
CLAUDE_WEB_SEARCH_USD_PER_SEARCH = 0.01


def calculate_claude_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_searches: int = 0,
    usd_to_inr_rate: float = DEFAULT_USD_TO_INR_RATE,
) -> dict[str, Any]:
    """Cost of one Claude call in USD and INR. Raises KeyError for a model with no rates,
    so a missing row is noticed instead of being priced as some other model."""
    rates = CLAUDE_PRICING[(model_name or "").strip().lower()]
    usd = (
        input_tokens * rates["input_per_1m"]
        + output_tokens * rates["output_per_1m"]
        + cache_read_tokens * rates["cache_read_per_1m"]
        + cache_write_tokens * rates["cache_write_per_1m"]
    ) / 1_000_000.0
    search_usd = web_searches * CLAUDE_WEB_SEARCH_USD_PER_SEARCH
    usd += search_usd
    return {
        "web_search_cost_usd": round(search_usd, 6),
        "model_name": model_name,
        "rates": rates,
        "total_cost_usd": round(usd, 6),
        "total_cost_inr": round(usd * usd_to_inr_rate, 2),
        "usd_to_inr_rate": usd_to_inr_rate,
    }


def calculate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int = 1000,
    usd_to_inr_rate: float = DEFAULT_USD_TO_INR_RATE,
) -> dict[str, Any]:
    """Calculate input, output, and total costs in USD and INR for a given model and token counts."""
    normalized_model = (model_name or "gemini-2.5-flash").strip().lower()
    rates = MODEL_PRICING.get(normalized_model, MODEL_PRICING["gemini-2.5-flash"])

    input_cost_usd = (input_tokens / 1_000_000.0) * rates["input_per_1m"]
    output_cost_usd = (output_tokens / 1_000_000.0) * rates["output_per_1m"]
    total_cost_usd = input_cost_usd + output_cost_usd

    input_cost_inr = input_cost_usd * usd_to_inr_rate
    output_cost_inr = output_cost_usd * usd_to_inr_rate
    total_cost_inr = total_cost_usd * usd_to_inr_rate

    return {
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "rates": rates,
        "input_cost_usd": round(input_cost_usd, 6),
        "output_cost_usd": round(output_cost_usd, 6),
        "total_cost_usd": round(total_cost_usd, 6),
        "input_cost_inr": round(input_cost_inr, 2),
        "output_cost_inr": round(output_cost_inr, 2),
        "total_cost_inr": round(total_cost_inr, 2),
        "usd_to_inr_rate": usd_to_inr_rate,
    }
