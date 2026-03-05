from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .util import reviewer_instruction_provider, simple_before_model_modifier

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCREENER_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "screener.json"

import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)


def _persist_screener_output(payload: str) -> None:
    SCREENER_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCREENER_OUTPUT_PATH.write_text(payload, encoding="utf-8")


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

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        raw_list = ctx.session.state.get("file_list")

        if not raw_list or (isinstance(raw_list, str) and not raw_list.strip()):
            ctx.session.state["screened_file_list"] = "[]"
            _persist_screener_output("[]")
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text="[]")]),
            )
            return

        latest_payload: str | None = None
        async for event in self._reviewer.run_async(ctx):
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if isinstance(parts, list):
                text = "".join((getattr(part, "text", "") or "") for part in parts).strip()
                if text:
                    latest_payload = text
            yield event

        if latest_payload:
            ctx.session.state["screened_file_list"] = latest_payload
            _persist_screener_output(latest_payload)


file_metadata_screening_agent = FileMetadataScreeningAgent()
