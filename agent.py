from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from agents.hoarder.agent import root_agent as hoarder_agent
from agents.screener.agent import file_metadata_screening_agent as ollama_screener_agent
from agents.screener.agent_hosted import file_metadata_screening_agent as hosted_screener_agent

OUTPUT_FILE_PATH = Path(__file__).with_name("agent_output.txt")


class PersistentOutputSequentialAgent(SequentialAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        latest_response_text = ""

        async for event in super()._run_async_impl(ctx):
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) if content else None

            if isinstance(parts, list):
                event_text = "".join((getattr(part, "text", "") or "") for part in parts).strip()
                if event_text:
                    latest_response_text = event_text

            yield event

        OUTPUT_FILE_PATH.write_text(latest_response_text, encoding="utf-8")

    async def _run_live_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        async for event in self._run_async_impl(ctx):
            yield event


root_agent = PersistentOutputSequentialAgent(
    name="root_document_pipeline",
    description="Root sequential pipeline: hoarder retrieval followed by metadata screening.",
    sub_agents=[
        hoarder_agent,
        ollama_screener_agent
        if os.getenv("SCREENER_BACKEND", "hosted").strip().lower() == "ollama"
        else hosted_screener_agent,
    ],
)
