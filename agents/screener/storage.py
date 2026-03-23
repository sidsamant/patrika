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


def ensure_screened_files_schema(connection: sqlite3.Connection) -> None:
    """Ensure the shared screener-output table exists in the standardizer DB."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS screened_files (
          screened_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
          hoarder_output_id INTEGER,
          source_path TEXT NOT NULL,
          name TEXT,
          source_created_at TEXT,
          source_modified_at TEXT,
          is_selected INTEGER NOT NULL,
          rejection_reason TEXT,
          source_payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          processed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS screened_file_hoarder_outputs (
          screened_file_hoarder_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
          screened_file_id INTEGER NOT NULL,
          hoarder_output_id INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(screened_file_id) REFERENCES screened_files(screened_file_id),
          FOREIGN KEY(hoarder_output_id) REFERENCES hoarder_outputs(hoarder_output_id)
        )
        """
    )
    _ensure_column(connection, "screened_files", "hoarder_output_id", "INTEGER")
    _ensure_column(connection, "screened_files", "processed_at", "TEXT")
    connection.commit()


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
    created_at = _utc_now_iso()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        ensure_screened_files_schema(connection)

        for item in items:
            source_path = str(item.get("path") or item.get("pageUrl") or "").strip()
            name = str(item.get("name") or item.get("pageTitle") or "").strip() or None
            cursor = connection.execute(
                """
                INSERT INTO screened_files (
                  hoarder_output_id,
                  source_path,
                  name,
                  source_created_at,
                  source_modified_at,
                  is_selected,
                  rejection_reason,
                  source_payload_json,
                  created_at,
                  processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("_hoarder_output_id") if isinstance(item.get("_hoarder_output_id"), int) else None,
                    source_path,
                    name,
                    str(item.get("createdAt") or "").strip() or None,
                    str(item.get("modifiedAt") or "").strip() or None,
                    1 if bool(item.get("isSelected", True)) else 0,
                    str(item.get("rejectionReason") or "").strip() or None,
                    json.dumps(item, ensure_ascii=True, default=str),
                    created_at,
                    None,
                ),
            )
            screened_file_id = int(cursor.lastrowid)
            hoarder_output_id = item.get("_hoarder_output_id") if isinstance(item.get("_hoarder_output_id"), int) else None
            if hoarder_output_id is not None:
                connection.execute(
                    """
                    INSERT INTO screened_file_hoarder_outputs (
                      screened_file_id,
                      hoarder_output_id,
                      created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (screened_file_id, hoarder_output_id, created_at),
                )

        connection.commit()

    return len(items)
