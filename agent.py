from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv
from google.adk.agents import SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from agents.hoarder.agent import root_agent as hoarder_agent
from agents.newsletter_generator.agent import newsletter_generator_agent
from agents.screener.agent import file_metadata_screening_agent as ollama_screener_agent
from agents.screener.agent_hosted import file_metadata_screening_agent as hosted_screener_agent
from agents.standardizer.agent import standardizer_agent
from agents.sectionizer.agent import sectionizer_agent

OUTPUT_FILE_PATH = PROJECT_ROOT / "agent_output.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)

# Suppress verbose third-party loggers
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_adk").setLevel(logging.DEBUG)


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
    description="Root sequential pipeline: hoarder retrieval, metadata screening, standardization, sectionization, and newsletter generation.",
    sub_agents=[
        # hoarder_agent,
        # ollama_screener_agent
        # if os.getenv("SCREENER_BACKEND", "hosted").strip().lower() == "ollama"
        # else hosted_screener_agent,
        # standardizer_agent,
        # sectionizer_agent,
        newsletter_generator_agent,
    ],
)
