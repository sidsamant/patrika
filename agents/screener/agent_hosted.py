from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ..hoarder.storage import load_hoarder_rows_for_screening, mark_hoarder_rows_screened
from .storage import DB_PATH, persist_screened_payload
from .util import reviewer_instruction_provider, simple_before_model_modifier

LLM_REQUEST_DELAY_SECONDS = max(float(os.getenv("SCREENER_LLM_DELAY_SECONDS", "2.0")), 0.0)
logger = logging.getLogger(__name__)


class FileMetadataScreeningAgent(BaseAgent):
    def __init__(self) -> None:
        reviewer = LlmAgent(
            name="file_metadata_reviewer_llm",
            model="gemini-2.5-flash-lite",
            description="Reviews filesystem metadata and decides which files are eligible for ingestion.",
            output_key="screened_file_list",
            instruction=reviewer_instruction_provider,
            before_model_callback=simple_before_model_modifier,
        )

        super().__init__(
            name="file_metadata_screening_agent",
            description="Custom agent that screens file metadata using an internal LLM reviewer.",
            sub_agents=[reviewer],
        )

        self._reviewer = reviewer
        logger.debug("Initialized hosted screener with ADK LLM reviewer")

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
            "Starting hosted screener run. hoarder_items=%d last_screened_at=%s",
            len(hoarder_items),
            screened_at,
        )

        if not hoarder_items:
            # Emit an empty result explicitly so downstream steps can rely on a stable JSON payload.
            logger.debug("No hoarder rows found for screening; emitting empty hosted screener result")
            ctx.session.state["screened_file_list"] = "[]"
            persisted_count = persist_screened_payload("[]")
            logger.debug("Persisted %d hosted screener rows to %s", persisted_count, DB_PATH)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text="[]")]),
            )
            return

        latest_payload: str | None = None
        if LLM_REQUEST_DELAY_SECONDS > 0:
            logger.debug("Sleeping %.2f seconds before hosted screener LLM call.", LLM_REQUEST_DELAY_SECONDS)
            await asyncio.sleep(LLM_REQUEST_DELAY_SECONDS)

        # Forward every event from the internal reviewer, but remember the latest text payload so
        # we can persist the final screening result for later pipeline stages.
        async for event in self._reviewer.run_async(ctx):
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if isinstance(parts, list):
                text = "".join((getattr(part, "text", "") or "") for part in parts).strip()
                if text:
                    latest_payload = text
                    logger.debug("Captured hosted screener payload chunk with %d characters", len(text))
            yield event

        if latest_payload:
            ctx.session.state["screened_file_list"] = latest_payload
            persisted_count = persist_screened_payload(latest_payload)
            logger.debug(
                "Stored hosted screener response with %d characters and persisted %d rows to %s",
                len(latest_payload),
                persisted_count,
                DB_PATH,
            )
        else:
            logger.debug("Hosted screener produced no text payload to persist")


file_metadata_screening_agent = FileMetadataScreeningAgent()
