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
from .storage import DB_PATH, parse_screened_payload, persist_screened_payload
from .util import reviewer_instruction_provider

logger = logging.getLogger(__name__)
PASSTHROUGH_HOARDER_SOURCE_IDS = {"websource", "twitter"}


def _split_hoarder_items(items: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split hoarder rows into LLM-screened and passthrough buckets."""
    llm_items: list[dict[str, object]] = []
    passthrough_items: list[dict[str, object]] = []
    for item in items:
        source_id = str(item.get("sourceId") or "").strip().lower()
        if source_id in PASSTHROUGH_HOARDER_SOURCE_IDS:
            passthrough_items.append(item)
        else:
            llm_items.append(item)
    logger.debug(
        "Split hoarder rows for Ollama screener: total=%d llm=%d passthrough=%d",
        len(items),
        len(llm_items),
        len(passthrough_items),
    )
    return llm_items, passthrough_items


def _build_passthrough_screened_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Mark passthrough hoarder rows as selected without LLM review."""
    passthrough_rows: list[dict[str, object]] = []
    for item in items:
        passthrough_rows.append(
            {
                **item,
                "isSelected": True,
                "rejectionReason": None,
            }
        )
    if passthrough_rows:
        sample_names = [
            str(row.get("name") or row.get("pageTitle") or row.get("path") or row.get("pageUrl") or "").strip()
            for row in passthrough_rows[:5]
        ]
        logger.debug(
            "Prepared %d passthrough screener rows without LLM review. sample=%s",
            len(passthrough_rows),
            sample_names,
        )
    return passthrough_rows


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
        all_hoarder_items = load_hoarder_rows_for_screening()
        logger.debug("Loaded %d hoarder rows eligible for Ollama screening", len(all_hoarder_items))
        hoarder_items, passthrough_items = _split_hoarder_items(all_hoarder_items)
        hoarder_output_ids = [
            item.get("_hoarder_output_id")
            for item in all_hoarder_items
            if isinstance(item.get("_hoarder_output_id"), int)
        ]
        if hoarder_output_ids:
            logger.debug(
                "About to stamp %d hoarder rows as screened. first_ids=%s",
                len(hoarder_output_ids),
                hoarder_output_ids[:10],
            )
        screened_at = mark_hoarder_rows_screened(hoarder_output_ids)
        ctx.session.state["file_list"] = json.dumps(hoarder_items)
        logger.debug(
            "Starting Ollama screener run. llm_items=%d passthrough_items=%d last_screened_at=%s",
            len(hoarder_items),
            len(passthrough_items),
            screened_at,
        )
        if hoarder_items:
            llm_sample_names = [
                str(item.get("name") or item.get("pageTitle") or item.get("path") or item.get("pageUrl") or "").strip()
                for item in hoarder_items[:5]
            ]
            logger.debug("Ollama screener LLM-bound sample rows: %s", llm_sample_names)
        if not hoarder_items and not passthrough_items:
            logger.debug("No hoarder rows found for screening; emitting empty screener result")
            empty_payload = {"files": []}
            ctx.session.state["screened_file_list"] = json.dumps(empty_payload)
            persisted_count = persist_screened_payload(empty_payload)
            logger.debug("Persisted %d screener rows to %s", persisted_count, DB_PATH)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=json.dumps(empty_payload))]),
            )
            return

        passthrough_screened_items = _build_passthrough_screened_items(passthrough_items)
        if not hoarder_items:
            combined_payload = {"files": passthrough_screened_items}
            payload_text = json.dumps(combined_payload, indent=2)
            ctx.session.state["screened_file_list"] = payload_text
            persisted_count = persist_screened_payload(combined_payload)
            logger.debug(
                "No LLM-screened items required; auto-persisted %d passthrough screener rows to %s",
                persisted_count,
                DB_PATH,
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=payload_text)]),
            )
            return

        # The prompt builder encapsulates the file-list formatting and review instructions.
        prompt = reviewer_instruction_provider(ctx)
        logger.debug("Built Ollama screener prompt with %d characters", len(prompt))
        ollama_response = self._call_ollama(prompt)
        logger.debug("Received Ollama screener raw response with %d characters", len(ollama_response))
        llm_screened_items = parse_screened_payload(ollama_response)
        logger.debug(
            "Parsed %d screened rows from Ollama response; combining with %d passthrough rows",
            len(llm_screened_items),
            len(passthrough_screened_items),
        )
        combined_payload = {"files": [*llm_screened_items, *passthrough_screened_items]}
        payload_text = json.dumps(combined_payload, indent=2)
        ctx.session.state["screened_file_list"] = payload_text
        persisted_count = persist_screened_payload(combined_payload)
        logger.debug(
            "Stored Ollama screener response with %d LLM rows and %d passthrough rows; persisted %d rows to %s",
            len(llm_screened_items),
            len(passthrough_screened_items),
            persisted_count,
            DB_PATH,
        )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=payload_text)]),
        )


file_metadata_screening_agent = FileMetadataScreeningAgent()
