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
