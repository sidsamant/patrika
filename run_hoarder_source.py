from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

APP_NAME = "newsletter_adk"
USER_ID = "local_user"
SESSION_ID = "hoarder_source_session"


def _parse_items(raw_value: object) -> list[dict[str, object]]:
    if isinstance(raw_value, list):
        return [item for item in raw_value if isinstance(item, dict)]
    if not isinstance(raw_value, str):
        return []
    stripped = raw_value.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run one hoarder source agent.")
    parser.add_argument("--source-id", required=True, help="Configured hoarder source id to execute.")
    args = parser.parse_args()

    from agents.hoarder.agent import PersistentHoarderSequentialAgent, build_hoarder_source_agent
    from agents.hoarder.config import load_hoarder_config
    from agents.hoarder.storage import record_hoarder_source_run

    config = load_hoarder_config()
    source = next((item for item in config.sources if item.id == args.source_id), None)
    if source is None:
        raise ValueError(f"Unknown hoarder source id: {args.source_id}")

    source_agent = PersistentHoarderSequentialAgent(
        name=f"{source.id}_hoarder_coordinator",
        description=f"Single-source hoarder coordinator for {source.id}.",
        sub_agents=[build_hoarder_source_agent(source)],
    )
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    runner = Runner(agent=source_agent, app_name=APP_NAME, session_service=session_service)
    prompt = types.Content(
        role="user",
        parts=[types.Part(text=f"Run hoarder source {source.id}")],
    )

    try:
        async for _ in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=prompt):
            pass

        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
        raw_file_list = session.state.get("file_list") if session else None
        items = _parse_items(raw_file_list)
        item_count = len(items)
        record_hoarder_source_run(
            source_id=source.id,
            source_path=source.path,
            status="success",
            item_count=item_count,
            error_text=None,
        )
        print(
            json.dumps(
                {
                    "sourceId": source.id,
                    "status": "success",
                    "itemCount": item_count,
                },
                indent=2,
            )
        )
    except Exception as error:
        record_hoarder_source_run(
            source_id=source.id,
            source_path=source.path,
            status="error",
            item_count=0,
            error_text=str(error),
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())
