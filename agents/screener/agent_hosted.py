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

from hoarder.storage import load_hoarder_rows_for_screening, mark_hoarder_rows_screened
from .storage import parse_screened_payload, persist_screened_payload
from .util import reviewer_instruction_provider, simple_before_model_modifier

LLM_REQUEST_DELAY_SECONDS = max(float(os.getenv("SCREENER_LLM_DELAY_SECONDS", "2.0")), 0.0)
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
        "Split hoarder rows for hosted screener: total=%d llm=%d passthrough=%d",
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
            "Prepared %d hosted screener passthrough rows without LLM review. sample=%s",
            len(passthrough_rows),
            sample_names,
        )
    return passthrough_rows


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
        all_hoarder_items = load_hoarder_rows_for_screening()
        logger.debug("Loaded %d hoarder rows eligible for hosted screening", len(all_hoarder_items))
        hoarder_items, passthrough_items = _split_hoarder_items(all_hoarder_items)
        hoarder_output_ids = [
            item.get("_hoarder_output_id")
            for item in all_hoarder_items
            if isinstance(item.get("_hoarder_output_id"), int)
        ]
        if hoarder_output_ids:
            logger.debug(
                "About to stamp %d hoarder rows as screened for hosted screener. first_ids=%s",
                len(hoarder_output_ids),
                hoarder_output_ids[:10],
            )
        screened_at = mark_hoarder_rows_screened(hoarder_output_ids)
        ctx.session.state["file_list"] = json.dumps(hoarder_items)
        logger.debug(
            "Starting hosted screener run. llm_items=%d passthrough_items=%d last_screened_at=%s",
            len(hoarder_items),
            len(passthrough_items),
            screened_at,
        )
        if hoarder_items:
            llm_sample_names = [
                str(item.get("name") or item.get("pageTitle") or item.get("path") or item.get("pageUrl") or "").strip()
                for item in hoarder_items[:5]
            ]
            logger.debug("Hosted screener LLM-bound sample rows: %s", llm_sample_names)

        if not hoarder_items and not passthrough_items:
            logger.debug("No hoarder rows found for screening; emitting empty hosted screener result")
            empty_payload = {"files": []}
            ctx.session.state["screened_file_list"] = json.dumps(empty_payload)
            persisted_count = persist_screened_payload(empty_payload)
            logger.debug("Persisted %d hosted screener rows to %s", persisted_count, "pipeline API")
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
                "No LLM-screened items required; auto-persisted %d passthrough hosted screener rows to %s",
                persisted_count,
                "pipeline API",
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=payload_text)]),
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
            logger.debug("Hosted screener final LLM payload length=%d", len(latest_payload))
            llm_screened_items = parse_screened_payload(latest_payload)
            logger.debug(
                "Parsed %d screened rows from hosted screener output; combining with %d passthrough rows",
                len(llm_screened_items),
                len(passthrough_screened_items),
            )
            combined_payload = {"files": [*llm_screened_items, *passthrough_screened_items]}
            payload_text = json.dumps(combined_payload, indent=2)
            ctx.session.state["screened_file_list"] = payload_text
            persisted_count = persist_screened_payload(combined_payload)
            logger.debug(
                "Stored hosted screener response with %d LLM rows and %d passthrough rows; persisted %d rows to %s",
                len(llm_screened_items),
                len(passthrough_screened_items),
                persisted_count,
                "pipeline API",
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=payload_text)]),
            )
        else:
            logger.debug("Hosted screener produced no text payload to persist")


file_metadata_screening_agent = FileMetadataScreeningAgent()
