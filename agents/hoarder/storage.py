from __future__ import annotations

import json
from typing import Any

import pipeline_client


def ensure_hoarder_outputs_schema(connection: object | None = None) -> None:
    pass


def _parse_items(raw_value: Any) -> list[dict[str, Any]]:
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


def persist_hoarder_payload(raw_value: Any, agent_run_id: int | None = None) -> int:
    items = _parse_items(raw_value)
    if not items:
        return 0
    return pipeline_client.persist_hoarder_batch(items, agent_run_id)


def load_hoarder_rows_for_screening() -> list[dict[str, Any]]:
    return pipeline_client.load_hoarder_rows_for_screening()


def mark_hoarder_rows_screened(hoarder_output_ids: list[int]) -> str | None:
    return pipeline_client.mark_hoarder_rows_screened(hoarder_output_ids)


def record_hoarder_source_run(
    *,
    source_id: str,
    source_path: str | None,
    status: str,
    item_count: int | None = None,
    error_text: str | None = None,
) -> int:
    return pipeline_client.record_hoarder_source_run(
        source_id=source_id,
        source_path=source_path,
        status=status,
        item_count=item_count,
        error_text=error_text,
    )


def load_processed_hoarder_artifact_paths(source_id: str) -> set[str]:
    return pipeline_client.load_processed_hoarder_artifact_paths(source_id)


def record_hoarder_source_artifact(
    *,
    source_id: str,
    artifact_path: str,
    status: str,
    error_text: str | None = None,
) -> int:
    return pipeline_client.record_hoarder_source_artifact(
        source_id=source_id,
        artifact_path=artifact_path,
        status=status,
        error_text=error_text,
    )
