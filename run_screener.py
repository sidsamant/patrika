from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

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


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run screener agent.")
    parser.add_argument(
        "--backend",
        choices=["hosted", "ollama"],
        default="hosted",
        help="Screener backend to run.",
    )
    args = parser.parse_args()

    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    screener_agent = hosted_screener_agent if args.backend == "hosted" else ollama_screener_agent
    screener_runner = Runner(agent=screener_agent, app_name=APP_NAME, session_service=session_service)
    await _run_agent(screener_runner, "Screen collected file metadata and return JSON")


if __name__ == "__main__":
    asyncio.run(main())
