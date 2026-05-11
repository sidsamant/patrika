from __future__ import annotations

import json
from typing import Any

import pipeline_client


def ensure_screened_files_schema(connection: object | None = None) -> None:
    pass


def _strip_json_fence(raw_text: str) -> str:
    stripped = raw_text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_screened_payload(raw_value: Any) -> list[dict[str, Any]]:
    if raw_value is None:
        return []

    payload = raw_value
    if isinstance(raw_value, str):
        stripped = _strip_json_fence(raw_value)
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict):
        files = payload.get("files")
        if isinstance(files, list):
            return [item for item in files if isinstance(item, dict)]
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


def persist_screened_payload(raw_value: Any, agent_run_id: int | None = None) -> int:
    items = parse_screened_payload(raw_value)
    if not items:
        return 0
    return pipeline_client.persist_screened_batch(items, agent_run_id)
