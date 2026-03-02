from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .util import reviewer_instruction_provider, simple_before_model_modifier


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
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text="[]")]),
            )
            return

        async for event in self._reviewer.run_async(ctx):
            yield event


file_metadata_screening_agent = FileMetadataScreeningAgent()
