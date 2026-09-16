"""Pre-execution token counting and cost estimation graph node for Google Gemini LLM API."""
from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import client as genai_client
from google.genai import types

from workflows.pricing_matrix import calculate_cost

logger = logging.getLogger(__name__)


class PreCallTokenCostEstimatorNode:
    """Graph node that measures prompt token count via Google GenAI count_tokens API and computes pricing proof."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    def _get_client(self) -> genai_client.Client | None:
        key = self.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            return None
        try:
            return genai_client.Client(api_key=key)
        except Exception:
            return None

    def estimate_prompt_cost(
        self,
        *,
        model_name: str,
        prompt_text: str,
        system_instruction: str | None = None,
        context_text: str | None = None,
        media_parts: list[dict[str, Any]] | None = None,
        max_output_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Calculates exact input tokens and upper-bound estimated output cost using Google GenAI SDK count_tokens."""
        cl = self._get_client()
        normalized_model = model_name or "gemini-2.5-flash"

        # Build content parts for token counting
        contents: list[Any] = []
        if context_text:
            contents.append(types.Part.from_text(text=f"Context:\n{context_text}"))
        if prompt_text:
            contents.append(types.Part.from_text(text=prompt_text))

        # Additional media assets
        if media_parts:
            for item in media_parts:
                mime_type = item.get("mime_type", "image/png")
                data_bytes = item.get("bytes")
                if data_bytes:
                    contents.append(types.Part.from_bytes(data=data_bytes, mime_type=mime_type))

        # Count tokens via Google GenAI client if available
        input_tokens = 0
        if cl:
            count_request_kwargs: dict[str, Any] = {"contents": contents}
            if system_instruction:
                count_request_kwargs["config"] = types.CountTokensConfig(
                    system_instruction=types.Content(
                        role="system", parts=[types.Part.from_text(text=system_instruction)]
                    )
                )

            try:
                token_res = cl.models.count_tokens(model=normalized_model, **count_request_kwargs)
                input_tokens = getattr(token_res, "total_tokens", 0)
            except Exception as exc:
                logger.warning(f"Error calling client.models.count_tokens ({exc}). Falling back to heuristic estimation.")

        if input_tokens == 0:
            total_chars = len(prompt_text or "") + len(context_text or "") + len(system_instruction or "")
            input_tokens = max(1, total_chars // 4)

        # Estimate individual token breakdown
        sys_tokens = (len(system_instruction or "") // 4) if system_instruction else 0
        ctx_tokens = (len(context_text or "") // 4) if context_text else 0
        user_tokens = max(0, input_tokens - sys_tokens - ctx_tokens)

        token_breakdown = {
            "system_instruction_tokens": sys_tokens,
            "context_tokens": ctx_tokens,
            "user_prompt_tokens": user_tokens,
            "total_input_tokens": input_tokens,
            "estimated_max_output_tokens": max_output_tokens,
        }

        cost_proof = calculate_cost(
            model_name=normalized_model,
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
        )

        return {
            "model_name": normalized_model,
            "input_tokens": input_tokens,
            "estimated_output_tokens": max_output_tokens,
            "token_breakdown": token_breakdown,
            "cost_proof": cost_proof,
            "estimated_cost_usd": cost_proof["total_cost_usd"],
            "estimated_cost_inr": cost_proof["total_cost_inr"],
        }
