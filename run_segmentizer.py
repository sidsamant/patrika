from __future__ import annotations

import asyncio
from typing import Iterable

from env import load_local_env
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_local_env()

from agents.segmentizer.agent import segmentizer_agent

APP_NAME = "segmentizer"
USER_ID = "local_user"
SESSION_ID = "segmentizer_session"


def _iter_text_parts(parts: Iterable[object]) -> Iterable[str]:
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            yield text


async def main() -> None:
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    runner = Runner(agent=segmentizer_agent, app_name=APP_NAME, session_service=session_service)
    prompt = types.Content(role="user", parts=[types.Part(text="Map standardized documents to configured newsletter segments")])

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
