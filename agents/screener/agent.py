from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncGenerator

import ollama
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .util import reviewer_instruction_provider

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCREENER_OUTPUT_PATH = PROJECT_ROOT / ".output" / "screener.json"


def _persist_screener_output(payload: str) -> None:
    SCREENER_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCREENER_OUTPUT_PATH.write_text(payload, encoding="utf-8")


class FileMetadataScreeningAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="file_metadata_screening_agent",
            description="Custom agent that screens file metadata using an internal LLM reviewer.",
        )
        self._model = os.getenv("SCREENER_OLLAMA_MODEL", "deepseek-r1:8b")

    def _call_ollama(self, prompt: str) -> str:
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
                return content
            return json.dumps(response_dict, default=str)

        return str(response)

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

        prompt = reviewer_instruction_provider(ctx)
        ollama_response = self._call_ollama(prompt)
        ctx.session.state["screened_file_list"] = ollama_response
        _persist_screener_output(ollama_response)

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=ollama_response)]),
        )


file_metadata_screening_agent = FileMetadataScreeningAgent()
