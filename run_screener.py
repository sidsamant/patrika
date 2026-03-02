from __future__ import annotations

import argparse
import asyncio
import json
from typing import Iterable

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.hoarder.sources.filesource.agent import filesource_agent
from agents.screener.agent import file_metadata_screening_agent as ollama_screener_agent
from agents.screener.agent_hosted import file_metadata_screening_agent as hosted_screener_agent

APP_NAME = "screener"
USER_ID = "local_user"
SESSION_ID = "screener_session"


def _iter_text_parts(parts: Iterable[object]) -> Iterable[str]:
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            yield text


async def _run_agent(runner: Runner, message: str) -> None:
    prompt = types.Content(role="user", parts=[types.Part(text=message)])
    async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=prompt):
        content = getattr(event, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None)
        if not isinstance(parts, list):
            continue
        for text in _iter_text_parts(parts):
            print(text)

async def _preload_file_list(session_service: InMemorySessionService) -> None:
    source_path = filesource_agent._source_path()
    file_metadata = await filesource_agent._collect_file_metadata_with_mcp(source_path)
    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
    session.state["file_list"] = json.dumps(file_metadata)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run screener agent.")
    parser.add_argument(
        "--backend",
        choices=["hosted", "ollama"],
        default="hosted",
        help="Screener backend to run.",
    )
    parser.add_argument(
        "--skip-hoarder",
        action="store_true",
        help="Skip hoarder pre-step. Use only if file_list already exists in session state.",
    )
    args = parser.parse_args()

    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    if not args.skip_hoarder:
        await _preload_file_list(session_service)

    screener_agent = hosted_screener_agent if args.backend == "hosted" else ollama_screener_agent
    screener_runner = Runner(agent=screener_agent, app_name=APP_NAME, session_service=session_service)
    await _run_agent(screener_runner, "Screen collected file metadata and return JSON")


if __name__ == "__main__":
    asyncio.run(main())
