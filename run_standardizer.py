from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import logging
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
STANDARDIZER_LOG_PATH = LOGS_DIR / f"standardizer-{RUN_TIMESTAMP}.debug.log"


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
    resolved_log_path = STANDARDIZER_LOG_PATH.resolve()
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
logger.debug("Initialized standardizer runner logging. log_path=%s", STANDARDIZER_LOG_PATH)

from agents.hoarder.config import SourceConfig, load_hoarder_config
from agents.hoarder.sources.filesource.agent import FilesourceHoarderAgent
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv(PROJECT_ROOT / ".env")

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


def _get_filesystem_source() -> SourceConfig:
    for source in load_hoarder_config().sources:
        if source.id == "filesystem" and source.enabled:
            return source
    raise ValueError("No enabled filesystem source found in agents/hoarder/config.yaml")


async def _preload_file_list(session_service: InMemorySessionService) -> None:
    filesource_agent = FilesourceHoarderAgent(_get_filesystem_source())
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
    logger.debug("Starting standardizer runner with run_screener=%s", args.run_screener)
    logger.debug("Standardizer debug log path: %s", STANDARDIZER_LOG_PATH)

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
