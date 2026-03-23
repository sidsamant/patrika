from __future__ import annotations

import json
import logging
import os
from typing import AsyncGenerator

import ollama
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ..hoarder.storage import load_hoarder_rows_for_screening, mark_hoarder_rows_screened
from .storage import DB_PATH, persist_screened_payload
from .util import reviewer_instruction_provider

logger = logging.getLogger(__name__)


class FileMetadataScreeningAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="file_metadata_screening_agent",
            description="Custom agent that screens file metadata using an internal LLM reviewer.",
        )
        self._model = os.getenv("SCREENER_OLLAMA_MODEL", "deepseek-r1:8b")
        logger.debug("Initialized Ollama screener with model %s", self._model)

    def _call_ollama(self, prompt: str) -> str:
        logger.debug("Sending screener prompt to Ollama model %s", self._model)
        response = ollama.chat(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            options={"keep_alive": "30m"},
        )
        response_dict: dict[str, object] | None = None
        if isinstance(response, dict):
            response_dict = response
        else:
            model_dump = getattr(response, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump()
                if isinstance(dumped, dict):
                    response_dict = dumped
            if response_dict is None:
                to_dict = getattr(response, "dict", None)
                if callable(to_dict):
                    dumped = to_dict()
                    if isinstance(dumped, dict):
                        response_dict = dumped

        if response_dict:
            message = response_dict.get("message", {})
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                logger.debug("Received structured Ollama response with %d characters", len(content))
                return content
            logger.debug("Received non-message Ollama response; serializing full payload")
            return json.dumps(response_dict, default=str)

        logger.debug("Received plain-string Ollama response")
        return str(response)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        hoarder_items = load_hoarder_rows_for_screening()
        hoarder_output_ids = [
            item.get("_hoarder_output_id")
            for item in hoarder_items
            if isinstance(item.get("_hoarder_output_id"), int)
        ]
        screened_at = mark_hoarder_rows_screened(hoarder_output_ids)
        ctx.session.state["file_list"] = json.dumps(hoarder_items)
        raw_list = ctx.session.state.get("file_list")
        logger.debug(
            "Starting Ollama screener run. hoarder_items=%d last_screened_at=%s",
            len(hoarder_items),
            screened_at,
        )
        if not hoarder_items:
            # Keep downstream stages deterministic by always materializing an empty JSON array.
            logger.debug("No hoarder rows found for screening; emitting empty screener result")
            ctx.session.state["screened_file_list"] = "[]"
            persisted_count = persist_screened_payload("[]")
            logger.debug("Persisted %d screener rows to %s", persisted_count, DB_PATH)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text="[]")]),
            )
            return

        # The prompt builder encapsulates the file-list formatting and review instructions.
        prompt = reviewer_instruction_provider(ctx)
        logger.debug("Built Ollama screener prompt with %d characters", len(prompt))
        ollama_response = self._call_ollama(prompt)
        ctx.session.state["screened_file_list"] = ollama_response
        persisted_count = persist_screened_payload(ollama_response)
        logger.debug(
            "Stored Ollama screener response with %d characters and persisted %d rows to %s",
            len(ollama_response),
            persisted_count,
            DB_PATH,
        )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=ollama_response)]),
        )


file_metadata_screening_agent = FileMetadataScreeningAgent()
