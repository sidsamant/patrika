"""Google Gemini LLM execution node for ADK Graph Workflows."""
from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import client as genai_client
from google.genai import types

logger = logging.getLogger(__name__)


class LLMExecutionNode:
    """Graph node that executes approved Google Gemini LLM API generation requests."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    def _get_client(self) -> genai_client.Client:
        return genai_client.Client(api_key=self.api_key)

    def execute_call(
        self,
        *,
        model_name: str,
        prompt_text: str,
        system_instruction: str | None = None,
        context_text: str | None = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Executes content generation via Google GenAI SDK and returns output text and usage metadata."""
        cl = self._get_client()
        normalized_model = model_name or "gemini-2.5-flash"

        full_prompt = prompt_text
        if context_text:
            full_prompt = f"Context:\n{context_text}\n\nTask:\n{prompt_text}"

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        if system_instruction:
            config.system_instruction = system_instruction

        logger.info(f"Executing Google Gemini LLM call with model [{normalized_model}]...")
        response = cl.models.generate_content(
            model=normalized_model,
            contents=full_prompt,
            config=config,
        )

        response_text = response.text or ""
        usage_meta = getattr(response, "usage_metadata", None)

        prompt_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
        candidates_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0
        total_tokens = getattr(usage_meta, "total_token_count", 0) if usage_meta else (prompt_tokens + candidates_tokens)

        return {
            "output_text": response_text,
            "model_name": normalized_model,
            "actual_tokens": {
                "prompt_token_count": prompt_tokens,
                "candidates_token_count": candidates_tokens,
                "total_token_count": total_tokens,
            },
        }
