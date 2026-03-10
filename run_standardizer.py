from __future__ import annotations

import argparse
import asyncio
from typing import Iterable

from env import load_local_env
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_local_env()

from agents.hoarder.sources.filesource.agent import filesource_agent
from agents.screener.agent_hosted import file_metadata_screening_agent
from agents.standardizer.agent import standardizer_agent

APP_NAME = "standardizer"
USER_ID = "local_user"
SESSION_ID = "standardizer_session"


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
    import json

    session.state["file_list"] = json.dumps(file_metadata)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run standardizer agent.")
    parser.add_argument(
        "--run-screener",
        action="store_true",
        help="Run screener before standardizer. Default is false to allow file-based screener input fallback.",
    )
    args = parser.parse_args()

    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    if args.run_screener:
        await _preload_file_list(session_service)
        screener_runner = Runner(agent=file_metadata_screening_agent, app_name=APP_NAME, session_service=session_service)
        await _run_agent(screener_runner, "Screen collected file metadata and return JSON")

    standardizer_runner = Runner(agent=standardizer_agent, app_name=APP_NAME, session_service=session_service)
    await _run_agent(standardizer_runner, "Standardize screened documents and persist to local DB")


if __name__ == "__main__":
    asyncio.run(main())
