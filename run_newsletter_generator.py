from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from agents.newsletter_generator.agent import newsletter_generator_agent

APP_NAME = "newsletter_generator"
USER_ID = "local_user"
SESSION_ID = "newsletter_generator_session"


def _iter_text_parts(parts: Iterable[object]) -> Iterable[str]:
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            yield text


async def main() -> None:
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    runner = Runner(agent=newsletter_generator_agent, app_name=APP_NAME, session_service=session_service)
    prompt = types.Content(role="user", parts=[types.Part(text="Generate a markdown newsletter from latest sectionizer outputs")])

    async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=prompt):
        content = getattr(event, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None)
        if not isinstance(parts, list):
            continue
        for text in _iter_text_parts(parts):
            print(text)


if __name__ == "__main__":
    asyncio.run(main())
