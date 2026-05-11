from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
SCREENER_LOG_PATH = LOGS_DIR / f"screener-{RUN_TIMESTAMP}.debug.log"


def _configure_logging() -> logging.Logger:
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in root_logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    resolved_log_path = SCREENER_LOG_PATH.resolve()
    if not any(
        isinstance(handler, logging.FileHandler) and Path(getattr(handler, "baseFilename", "")).resolve() == resolved_log_path
        for handler in root_logger.handlers
    ):
        file_handler = logging.FileHandler(resolved_log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger(__name__)


logger = _configure_logging()
logger.debug("Initialized screener runner logging. log_path=%s", SCREENER_LOG_PATH)

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv(PROJECT_ROOT / ".env")

_SENTINELPRESS_ROOT = str(PROJECT_ROOT.parent / "sentinelpress")
if _SENTINELPRESS_ROOT not in sys.path:
    sys.path.insert(0, _SENTINELPRESS_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sentinelpress.settings")
import django
django.setup()

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
    logger.debug("Starting screener runner with backend=%s", args.backend)
    logger.debug("Screener debug log path: %s", SCREENER_LOG_PATH)

    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    screener_agent = hosted_screener_agent if args.backend == "hosted" else ollama_screener_agent
    screener_runner = Runner(agent=screener_agent, app_name=APP_NAME, session_service=session_service)
    await _run_agent(screener_runner, "Screen collected file metadata and return JSON")


if __name__ == "__main__":
    asyncio.run(main())
