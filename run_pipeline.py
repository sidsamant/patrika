from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

_SENTINELPRESS_ROOT = str(PROJECT_ROOT.parent / "sentinelpress")
if _SENTINELPRESS_ROOT not in sys.path:
    sys.path.insert(0, _SENTINELPRESS_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sentinelpress.settings")
import django
django.setup()

APP_NAME = "newsletter_adk"
USER_ID = "local_user"
SESSION_ID = "pipeline_session"


def _iter_text_parts(parts: Iterable[object]) -> Iterable[str]:
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            yield text


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full newsletter pipeline.")
    parser.add_argument(
        "--backend",
        choices=["hosted", "ollama"],
        help="Override the screener backend for this pipeline run.",
    )
    args = parser.parse_args()

    if args.backend:
        os.environ["SCREENER_BACKEND"] = args.backend

    from agent import root_agent

    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
    prompt = types.Content(
        role="user",
        parts=[types.Part(text="Run hoarder, screener, standardizer, and sectionizer in sequence")],
    )

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
