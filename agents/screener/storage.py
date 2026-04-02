from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db.standardizer_db import (
    DB_PATH,
    ScreenedFile,
    ScreenedFileHoarderOutput,
    ensure_standardizer_schema,
    session_scope,
    utc_now_iso,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def ensure_screened_files_schema(connection: object | None = None) -> None:
    """Ensure the shared screener tables exist in the standardizer DB."""
    del connection
    ensure_standardizer_schema()


def _strip_json_fence(raw_text: str) -> str:
    """Remove optional Markdown code fences around a JSON payload."""
    stripped = raw_text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_screened_payload(raw_value: Any) -> list[dict[str, Any]]:
    """Normalize screener output into a list of candidate item dictionaries."""
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


def persist_screened_payload(raw_value: Any) -> int:
    """Append the latest screener result to the shared screener-output table."""
    items = parse_screened_payload(raw_value)
    created_at = utc_now_iso()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with session_scope() as session:
        for item in items:
            source_path = str(item.get("path") or item.get("pageUrl") or "").strip()
            name = str(item.get("name") or item.get("pageTitle") or "").strip() or None
            screened_row = ScreenedFile(
                hoarder_output_id=item.get("_hoarder_output_id") if isinstance(item.get("_hoarder_output_id"), int) else None,
                source_path=source_path,
                name=name,
                source_created_at=str(item.get("createdAt") or "").strip() or None,
                source_modified_at=str(item.get("modifiedAt") or "").strip() or None,
                is_selected=1 if bool(item.get("isSelected", True)) else 0,
                rejection_reason=str(item.get("rejectionReason") or "").strip() or None,
                source_payload_json=json.dumps(item, ensure_ascii=True, default=str),
                created_at=created_at,
                processed_at=None,
            )
            session.add(screened_row)
            session.flush()

            hoarder_output_id = item.get("_hoarder_output_id") if isinstance(item.get("_hoarder_output_id"), int) else None
            if hoarder_output_id is not None:
                session.add(
                    ScreenedFileHoarderOutput(
                        screened_file_id=int(screened_row.screened_file_id),
                        hoarder_output_id=hoarder_output_id,
                        created_at=created_at,
                    )
                )

    return len(items)
