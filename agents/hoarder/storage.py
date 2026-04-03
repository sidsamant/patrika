from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from db.standardizer_db import (
    DB_PATH,
    HoarderOutput,
    HoarderSourceArtifact,
    HoarderSourceRun,
    ensure_standardizer_schema,
    session_scope,
    utc_now_iso,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def ensure_hoarder_outputs_schema(connection: object | None = None) -> None:
    """Ensure the shared hoarder tables exist in the standardizer DB."""
    del connection
    ensure_standardizer_schema()


def _parse_items(raw_value: Any) -> list[dict[str, Any]]:
    """Normalize hoarder output into a list of item dictionaries."""
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


def persist_hoarder_payload(raw_value: Any) -> int:
    """Append hoarder output rows to the shared DB without replacing prior rows."""
    items = _parse_items(raw_value)
    created_at = utc_now_iso()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with session_scope() as session:
        for item in items:
            source_path = str(item.get("path") or item.get("pageUrl") or "").strip()
            if not source_path:
                continue

            session.add(
                HoarderOutput(
                    source_id=str(item.get("sourceId") or item.get("sourceType") or "").strip() or None,
                    source_path=source_path,
                    name=str(item.get("name") or item.get("pageTitle") or "").strip() or None,
                    source_created_at=str(item.get("createdAt") or "").strip() or None,
                    source_modified_at=str(item.get("modifiedAt") or "").strip() or None,
                    source_payload_json=json.dumps(item, ensure_ascii=True, default=str),
                    created_at=created_at,
                    screened_at=None,
                )
            )
    return len(items)


def load_hoarder_rows_for_screening() -> list[dict[str, Any]]:
    """Load hoarder rows as screener input and include their DB ids for tracing."""
    if not DB_PATH.exists():
        return []

    with session_scope() as session:
        rows = session.execute(
            select(HoarderOutput)
            .where(HoarderOutput.screened_at.is_(None))
            .order_by(HoarderOutput.hoarder_output_id.asc())
        ).scalars().all()

    results: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        payload_raw = row.source_payload_json
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                loaded = json.loads(payload_raw)
                if isinstance(loaded, dict):
                    item = loaded
            except json.JSONDecodeError:
                item = {}

        if item:
            item["_hoarder_output_id"] = int(row.hoarder_output_id)
            item["_hoarder_created_at"] = row.created_at
            item["_hoarder_screened_at"] = row.screened_at
            results.append(item)

    return results


def mark_hoarder_rows_screened(hoarder_output_ids: list[int]) -> str | None:
    """Stamp hoarder rows after the screener has read them."""
    ids = [value for value in hoarder_output_ids if isinstance(value, int)]
    if not ids:
        return None

    screened_at = utc_now_iso()
    with session_scope() as session:
        rows = session.execute(
            select(HoarderOutput).where(HoarderOutput.hoarder_output_id.in_(ids))
        ).scalars().all()
        for row in rows:
            row.screened_at = screened_at
    return screened_at


def record_hoarder_source_run(
    *,
    source_id: str,
    source_path: str | None,
    status: str,
    item_count: int | None = None,
    error_text: str | None = None,
) -> int:
    """Append one hoarder source-agent run record for dashboard observability."""
    created_at = utc_now_iso()
    with session_scope() as session:
        row = HoarderSourceRun(
            source_id=source_id.strip(),
            source_path=source_path.strip() if isinstance(source_path, str) and source_path.strip() else None,
            status=status.strip(),
            item_count=item_count,
            error_text=error_text.strip() if isinstance(error_text, str) and error_text.strip() else None,
            created_at=created_at,
        )
        session.add(row)
        session.flush()
        return int(row.hoarder_source_run_id)


def load_processed_hoarder_artifact_paths(source_id: str) -> set[str]:
    """Return the set of artifact paths already processed for a hoarder source."""
    if not DB_PATH.exists():
        return set()

    with session_scope() as session:
        rows = session.execute(
            select(HoarderSourceArtifact.artifact_path).where(
                HoarderSourceArtifact.source_id == source_id.strip(),
                HoarderSourceArtifact.status == "success",
            )
        ).all()
    return {str(row[0]).strip() for row in rows if row and str(row[0]).strip()}


def record_hoarder_source_artifact(
    *,
    source_id: str,
    artifact_path: str,
    status: str,
    error_text: str | None = None,
) -> int:
    """Record one source artifact processing outcome for hoarder auditability."""
    processed_at = utc_now_iso()
    with session_scope() as session:
        row = HoarderSourceArtifact(
            source_id=source_id.strip(),
            artifact_path=artifact_path.strip(),
            status=status.strip(),
            error_text=error_text.strip() if isinstance(error_text, str) and error_text.strip() else None,
            processed_at=processed_at,
        )
        session.add(row)
        session.flush()
        return int(row.hoarder_source_artifact_id)
