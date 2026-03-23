from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    """Add a missing SQLite column when upgrading an existing table."""
    existing_columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if len(row) > 1
    }
    if column_name not in existing_columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def ensure_hoarder_outputs_schema(connection: sqlite3.Connection) -> None:
    """Ensure the shared hoarder-output table exists in the standardizer DB."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hoarder_outputs (
          hoarder_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id TEXT,
          source_path TEXT NOT NULL,
          name TEXT,
          source_created_at TEXT,
          source_modified_at TEXT,
          source_payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          screened_at TEXT
        )
        """
    )
    _ensure_column(connection, "hoarder_outputs", "screened_at", "TEXT")
    connection.commit()


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
    created_at = _utc_now_iso()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        ensure_hoarder_outputs_schema(connection)

        for item in items:
            source_path = str(item.get("path") or item.get("pageUrl") or "").strip()
            if not source_path:
                continue

            connection.execute(
                """
                INSERT INTO hoarder_outputs (
                  source_id,
                  source_path,
                  name,
                  source_created_at,
                  source_modified_at,
                  source_payload_json,
                  created_at,
                  screened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item.get("sourceId") or item.get("sourceType") or "").strip() or None,
                    source_path,
                    str(item.get("name") or item.get("pageTitle") or "").strip() or None,
                    str(item.get("createdAt") or "").strip() or None,
                    str(item.get("modifiedAt") or "").strip() or None,
                    json.dumps(item, ensure_ascii=True, default=str),
                    created_at,
                    None,
                ),
            )

        connection.commit()

    return len(items)


def load_hoarder_rows_for_screening() -> list[dict[str, Any]]:
    """Load hoarder rows as screener input and include their DB ids for tracing."""
    if not DB_PATH.exists():
        return []

    with sqlite3.connect(DB_PATH) as connection:
        ensure_hoarder_outputs_schema(connection)
        cursor = connection.execute(
            """
            SELECT
              hoarder_output_id,
              source_payload_json,
              created_at,
              screened_at
            FROM hoarder_outputs
            ORDER BY hoarder_output_id ASC
            """
        )
        rows: list[dict[str, Any]] = []
        for record in cursor.fetchall():
            payload_raw = record[1]
            item: dict[str, Any] = {}
            if isinstance(payload_raw, str) and payload_raw.strip():
                try:
                    loaded = json.loads(payload_raw)
                    if isinstance(loaded, dict):
                        item = loaded
                except json.JSONDecodeError:
                    item = {}

            if item:
                item["_hoarder_output_id"] = int(record[0])
                item["_hoarder_created_at"] = record[2]
                item["_hoarder_screened_at"] = record[3]
                rows.append(item)

        return rows


def mark_hoarder_rows_screened(hoarder_output_ids: list[int]) -> str | None:
    """Stamp hoarder rows after the screener has read them."""
    ids = [value for value in hoarder_output_ids if isinstance(value, int)]
    if not ids:
        return None

    screened_at = _utc_now_iso()
    placeholders = ", ".join("?" for _ in ids)
    with sqlite3.connect(DB_PATH) as connection:
        ensure_hoarder_outputs_schema(connection)
        connection.execute(
            f"UPDATE hoarder_outputs SET screened_at = ? WHERE hoarder_output_id IN ({placeholders})",
            [screened_at, *ids],
        )
        connection.commit()

    return screened_at
