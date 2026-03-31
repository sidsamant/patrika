from __future__ import annotations

import json
import io
import sqlite3
import subprocess
from datetime import date, datetime
from html import escape
from pathlib import Path
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
import streamlit as st
from agents.hoarder.config import load_hoarder_config

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"
RUN_PIPELINE_PATH = PROJECT_ROOT / "run_pipeline.py"
RUN_HOARDER_SOURCE_PATH = PROJECT_ROOT / "run_hoarder_source.py"


def apply_admin_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
          --app-bg: #f4f7fb;
          --panel-bg: #ffffff;
          --panel-muted: #f8fbff;
          --border: #dbe5f0;
          --text: #172033;
          --muted: #61708a;
          --brand: #1976ff;
          --brand-strong: #0e5ddd;
          --brand-soft: rgba(25, 118, 255, 0.14);
          --success: #18a957;
          --shadow: 0 18px 45px rgba(18, 38, 63, 0.08);
        }

        .stApp {
          background:
            radial-gradient(circle at top right, rgba(25, 118, 255, 0.08), transparent 28%),
            linear-gradient(180deg, #f8fbff 0%, #f4f7fb 48%, #edf3f9 100%);
          color: var(--text);
        }

        .block-container {
          padding-top: 1.4rem;
          padding-bottom: 2rem;
          max-width: 1440px;
        }

        [data-testid="stSidebar"] {
          background: linear-gradient(180deg, #13213d 0%, #192c4f 100%);
          border-right: 1px solid rgba(255, 255, 255, 0.08);
        }

        [data-testid="stSidebar"] * {
          color: #edf4ff;
        }

        [data-testid="stSidebar"] .stSelectbox label,
        [data-testid="stSidebar"] .stButton button,
        [data-testid="stSidebar"] .stCodeBlock,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] .stCaption {
          color: inherit;
        }

        [data-testid="stMetric"] {
          background: linear-gradient(180deg, var(--panel-bg) 0%, var(--panel-muted) 100%);
          border: 1px solid var(--border);
          border-radius: 18px;
          box-shadow: var(--shadow);
          padding: 0.95rem 1rem;
        }

        [data-testid="stMetricLabel"] {
          color: var(--muted);
          font-weight: 600;
        }

        [data-testid="stMetricValue"] {
          color: var(--text);
          font-weight: 800;
        }

        div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlockBorderWrapper"] {
          border-radius: 22px;
          border: 1px solid var(--border);
          background: linear-gradient(180deg, rgba(255, 255, 255, 0.94) 0%, rgba(248, 251, 255, 0.98) 100%);
          box-shadow: var(--shadow);
        }

        .stDataFrame, .stCodeBlock, .stTextArea textarea {
          border-radius: 18px;
        }

        .stButton > button,
        .stDownloadButton > button {
          background: linear-gradient(135deg, var(--brand) 0%, var(--brand-strong) 100%);
          color: #ffffff;
          border: 1px solid rgba(14, 93, 221, 0.25);
          border-radius: 14px;
          font-weight: 700;
          min-height: 2.8rem;
          box-shadow: 0 12px 28px rgba(25, 118, 255, 0.28);
          transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover {
          transform: translateY(-1px);
          box-shadow:
            0 0 0 1px rgba(255, 255, 255, 0.18) inset,
            0 0 18px rgba(74, 156, 255, 0.42),
            0 0 34px rgba(25, 118, 255, 0.26),
            0 14px 32px rgba(25, 118, 255, 0.34);
          filter: brightness(1.04);
          animation: cta-flicker 0.7s ease-in-out infinite alternate;
        }

        .stButton > button:focus,
        .stDownloadButton > button:focus {
          outline: none;
          box-shadow: 0 0 0 4px var(--brand-soft), 0 14px 30px rgba(25, 118, 255, 0.32);
        }

        @keyframes cta-flicker {
          0% {
            box-shadow:
              0 0 0 1px rgba(255, 255, 255, 0.18) inset,
              0 0 12px rgba(74, 156, 255, 0.28),
              0 0 24px rgba(25, 118, 255, 0.18),
              0 12px 28px rgba(25, 118, 255, 0.26);
          }
          100% {
            box-shadow:
              0 0 0 1px rgba(255, 255, 255, 0.2) inset,
              0 0 22px rgba(107, 176, 255, 0.5),
              0 0 40px rgba(25, 118, 255, 0.32),
              0 16px 36px rgba(25, 118, 255, 0.36);
          }
        }

        .dashboard-hero {
          padding: 1.25rem 1.35rem;
          border-radius: 24px;
          background:
            radial-gradient(circle at top right, rgba(138, 194, 255, 0.24), transparent 26%),
            linear-gradient(135deg, #13213d 0%, #183562 52%, #1e4b8f 100%);
          color: #f4f8ff;
          box-shadow: 0 24px 55px rgba(19, 33, 61, 0.28);
          margin-bottom: 1rem;
        }

        .dashboard-hero h1 {
          margin: 0;
          font-size: 2rem;
          line-height: 1.1;
          font-weight: 800;
          color: #ffffff;
        }

        .dashboard-hero p {
          margin: 0.45rem 0 0;
          color: rgba(244, 248, 255, 0.84);
          max-width: 52rem;
        }

        .dashboard-chip-row {
          display: flex;
          gap: 0.5rem;
          flex-wrap: wrap;
          margin-top: 0.85rem;
        }

        .dashboard-chip {
          background: rgba(255, 255, 255, 0.12);
          border: 1px solid rgba(255, 255, 255, 0.14);
          color: #f4f8ff;
          border-radius: 999px;
          font-size: 0.84rem;
          font-weight: 700;
          padding: 0.38rem 0.7rem;
        }

        @media (max-width: 900px) {
          .block-container {
            padding-top: 1rem;
          }

          .dashboard-hero h1 {
            font-size: 1.5rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    existing_columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if len(row) > 1
    }
    if column_name not in existing_columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def _ensure_dashboard_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sectionizer_output_documents (
          sectionizer_output_document_id INTEGER PRIMARY KEY AUTOINCREMENT,
          sectionizer_output_id INTEGER NOT NULL,
          doc_id INTEGER NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_screened_files (
          document_screened_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_id INTEGER NOT NULL,
          screened_file_id INTEGER NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hoarder_output_media_assets (
          hoarder_output_media_asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
          hoarder_output_id INTEGER NOT NULL,
          media_id INTEGER NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS screened_file_hoarder_outputs (
          screened_file_hoarder_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
          screened_file_id INTEGER NOT NULL,
          hoarder_output_id INTEGER NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hoarder_source_runs (
          hoarder_source_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id TEXT NOT NULL,
          source_path TEXT,
          status TEXT NOT NULL,
          item_count INTEGER,
          error_text TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS newsletter_runs (
          newsletter_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_timestamp TEXT NOT NULL,
          llm_instruction TEXT,
          llm_content TEXT,
          output_markdown TEXT NOT NULL,
          output_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS newsletter_run_sectionizer_outputs (
          newsletter_run_sectionizer_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
          newsletter_run_id INTEGER NOT NULL,
          sectionizer_output_id INTEGER NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS newsletter_run_configs (
          newsletter_run_id INTEGER PRIMARY KEY,
          newsletter_date TEXT,
          config_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(connection, "newsletter_runs", "llm_instruction", "TEXT")
    _ensure_column(connection, "newsletter_runs", "llm_content", "TEXT")
    _ensure_column(connection, "newsletter_runs", "output_html", "TEXT")
    connection.commit()


def _open_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    _ensure_dashboard_schema(connection)
    return connection


@st.cache_data(show_spinner=False)
def load_newsletter_runs() -> list[dict[str, object]]:
    if not DB_PATH.exists():
        return []

    runs: list[dict[str, object]] = []
    with _open_connection() as connection:
        cursor = connection.execute(
            """
            SELECT newsletter_run_id, run_timestamp, output_json, created_at
            FROM newsletter_runs
            ORDER BY newsletter_run_id DESC
            """
        )
        for row in cursor.fetchall():
            output_payload: dict[str, object] = {}
            raw_output = row["output_json"]
            if isinstance(raw_output, str) and raw_output.strip():
                try:
                    loaded = json.loads(raw_output)
                    if isinstance(loaded, dict):
                        output_payload = loaded
                except json.JSONDecodeError:
                    output_payload = {}
            runs.append(
                {
                    "newsletter_run_id": int(row["newsletter_run_id"]),
                    "run_timestamp": str(row["run_timestamp"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "section_count": int(output_payload.get("sectionCount") or 0),
                    "story_count": int(output_payload.get("storyCount") or 0),
                }
            )
    return runs


@st.cache_data(show_spinner=False)
def load_pending_screener_items() -> list[dict[str, object]]:
    if not DB_PATH.exists():
        return []

    with _open_connection() as connection:
        rows = connection.execute(
            """
            SELECT
              hoarder_output_id,
              source_id,
              name,
              source_path,
              created_at,
              screened_at
            FROM hoarder_outputs
            WHERE screened_at IS NULL
            ORDER BY hoarder_output_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@st.cache_data(show_spinner=False)
def load_hoarder_source_statuses() -> list[dict[str, object]]:
    config = load_hoarder_config()
    sources = config.sources
    latest_runs_by_source_id: dict[str, dict[str, object]] = {}

    if DB_PATH.exists():
        with _open_connection() as connection:
            rows = connection.execute(
                """
                WITH ranked_runs AS (
                  SELECT
                    hoarder_source_run_id,
                    source_id,
                    source_path,
                    status,
                    item_count,
                    error_text,
                    created_at,
                    ROW_NUMBER() OVER (
                      PARTITION BY source_id
                      ORDER BY hoarder_source_run_id DESC
                    ) AS source_rank
                  FROM hoarder_source_runs
                )
                SELECT
                  hoarder_source_run_id,
                  source_id,
                  source_path,
                  status,
                  item_count,
                  error_text,
                  created_at
                FROM ranked_runs
                WHERE source_rank = 1
                """
            ).fetchall()
        latest_runs_by_source_id = {str(row["source_id"]): dict(row) for row in rows}

    statuses: list[dict[str, object]] = []
    for source in sources:
        last_run = latest_runs_by_source_id.get(source.id, {})
        statuses.append(
            {
                "source_id": source.id,
                "enabled": source.enabled,
                "source_path": source.path,
                "last_run_at": str(last_run.get("created_at") or ""),
                "last_status": str(last_run.get("status") or ""),
                "last_item_count": last_run.get("item_count"),
                "last_error": str(last_run.get("error_text") or ""),
            }
        )
    return statuses


@st.cache_data(show_spinner=False)
def load_pending_standardizer_items() -> list[dict[str, object]]:
    if not DB_PATH.exists():
        return []

    with _open_connection() as connection:
        rows = connection.execute(
            """
            WITH ranked_screened_files AS (
              SELECT
                screened_file_id,
                hoarder_output_id,
                name,
                source_path,
                source_created_at,
                source_modified_at,
                created_at,
                processed_at,
                rejection_reason,
                ROW_NUMBER() OVER (
                  PARTITION BY COALESCE(NULLIF(LOWER(TRIM(source_path)), ''), 'screened_file:' || CAST(screened_file_id AS TEXT))
                  ORDER BY screened_file_id DESC
                ) AS source_rank
              FROM screened_files
              WHERE is_selected = 1
                AND processed_at IS NULL
            )
            SELECT
              screened_file_id,
              hoarder_output_id,
              name,
              source_path,
              source_created_at,
              source_modified_at,
              created_at,
              rejection_reason
            FROM ranked_screened_files
            WHERE source_rank = 1
            ORDER BY screened_file_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@st.cache_data(show_spinner=False)
def load_pending_sectionizer_items() -> list[dict[str, object]]:
    if not DB_PATH.exists():
        return []

    with _open_connection() as connection:
        rows = connection.execute(
            """
            WITH ranked_documents AS (
              SELECT
                d.doc_id,
                d.source_path,
                d.author,
                d.extraction_status,
                d.modified_at,
                d.created_at,
                d.persisted_at,
                ROW_NUMBER() OVER (
                  PARTITION BY COALESCE(NULLIF(LOWER(TRIM(d.source_path)), ''), 'doc:' || CAST(d.doc_id AS TEXT))
                  ORDER BY d.doc_id DESC
                ) AS source_rank
              FROM documents d
              WHERE d.text_content IS NOT NULL
                AND TRIM(d.text_content) <> ''
                AND NOT EXISTS (
                  SELECT 1
                  FROM sectionizer_outputs so
                  WHERE so.doc_id = d.doc_id
                )
            )
            SELECT
              doc_id,
              source_path,
              author,
              extraction_status,
              modified_at,
              created_at,
              persisted_at
            FROM ranked_documents
            WHERE source_rank = 1
            ORDER BY doc_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _parse_default_newsletter_date(raw_value: str) -> date:
    value = str(raw_value or "").strip()
    if not value:
        return date.today()

    for parser in (
        lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")).date(),
        lambda text: datetime.strptime(text, "%Y%m%d-%H%M%S").date(),
        lambda text: datetime.strptime(text, "%Y-%m-%d").date(),
    ):
        try:
            return parser(value)
        except ValueError:
            continue
    return date.today()


def _default_run_config(run_timestamp: str) -> dict[str, object]:
    return {
        "newsletter_date": _parse_default_newsletter_date(run_timestamp).isoformat(),
    }


def _load_run_config(connection: sqlite3.Connection, newsletter_run_id: int, run_timestamp: str) -> dict[str, object]:
    default_config = _default_run_config(run_timestamp)
    row = connection.execute(
        """
        SELECT newsletter_date, config_json
        FROM newsletter_run_configs
        WHERE newsletter_run_id = ?
        """,
        (newsletter_run_id,),
    ).fetchone()
    if row is None:
        return default_config

    config = dict(default_config)
    raw_config = row["config_json"]
    if isinstance(raw_config, str) and raw_config.strip():
        try:
            loaded = json.loads(raw_config)
            if isinstance(loaded, dict):
                config.update(loaded)
        except json.JSONDecodeError:
            pass

    newsletter_date = str(row["newsletter_date"] or "").strip()
    if newsletter_date:
        config["newsletter_date"] = newsletter_date
    return config


def save_run_config(newsletter_run_id: int, config: dict[str, object]) -> None:
    with _open_connection() as connection:
        current_timestamp = datetime.utcnow().isoformat() + "Z"
        newsletter_date = str(config.get("newsletter_date") or "").strip() or None
        connection.execute(
            """
            INSERT INTO newsletter_run_configs (
              newsletter_run_id,
              newsletter_date,
              config_json,
              created_at,
              updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(newsletter_run_id) DO UPDATE SET
              newsletter_date = excluded.newsletter_date,
              config_json = excluded.config_json,
              updated_at = excluded.updated_at
            """,
            (
                newsletter_run_id,
                newsletter_date,
                json.dumps(config, ensure_ascii=True, default=str),
                current_timestamp,
                current_timestamp,
            ),
        )
        connection.commit()


@st.cache_data(show_spinner=False)
def load_run_detail(newsletter_run_id: int) -> dict[str, object] | None:
    if not DB_PATH.exists():
        return None

    with _open_connection() as connection:
        run_row = connection.execute(
            """
            SELECT newsletter_run_id, run_timestamp, output_markdown, output_html, output_json, created_at
            FROM newsletter_runs
            WHERE newsletter_run_id = ?
            """,
            (newsletter_run_id,),
        ).fetchone()
        if run_row is None:
            return None

        output_payload: dict[str, object] = {}
        raw_output = run_row["output_json"]
        if isinstance(raw_output, str) and raw_output.strip():
            try:
                loaded = json.loads(raw_output)
                if isinstance(loaded, dict):
                    output_payload = loaded
            except json.JSONDecodeError:
                output_payload = {}

        sectionizer_rows = connection.execute(
            """
            SELECT
              so.sectionizer_output_id,
              so.doc_id,
              so.source_path,
              so.llm_instruction,
              so.llm_content,
              so.output_json,
              d.author
            FROM newsletter_run_sectionizer_outputs nrso
            JOIN sectionizer_outputs so
              ON so.sectionizer_output_id = nrso.sectionizer_output_id
            LEFT JOIN sectionizer_output_documents sod
              ON sod.sectionizer_output_id = so.sectionizer_output_id
            LEFT JOIN documents d
              ON d.doc_id = sod.doc_id
            WHERE nrso.newsletter_run_id = ?
            ORDER BY nrso.newsletter_run_sectionizer_output_id ASC
            """,
            (newsletter_run_id,),
        ).fetchall()

        details_by_source_path: dict[str, dict[str, object]] = {}
        for row in sectionizer_rows:
            payload: dict[str, object] = {}
            raw_payload = row["output_json"]
            if isinstance(raw_payload, str) and raw_payload.strip():
                try:
                    loaded = json.loads(raw_payload)
                    if isinstance(loaded, dict):
                        payload = loaded
                except json.JSONDecodeError:
                    payload = {}

            sectionizer_output_id = int(row["sectionizer_output_id"])
            related_rows = connection.execute(
                """
                SELECT
                  h.hoarder_output_id,
                  h.source_id,
                  h.source_path,
                  h.created_at AS hoarder_created_at,
                  sf.screened_file_id,
                  sf.is_selected,
                  sf.rejection_reason,
                  sf.created_at AS screener_created_at,
                  sf.processed_at AS screener_processed_at,
                  d.doc_id,
                  ma.media_id,
                  ma.artifact_path,
                  ma.mime_type
                FROM sectionizer_output_documents sod
                JOIN documents d
                  ON d.doc_id = sod.doc_id
                LEFT JOIN document_screened_files dsf
                  ON dsf.doc_id = d.doc_id
                LEFT JOIN screened_files sf
                  ON sf.screened_file_id = dsf.screened_file_id
                LEFT JOIN screened_file_hoarder_outputs sfho
                  ON sfho.screened_file_id = sf.screened_file_id
                LEFT JOIN hoarder_outputs h
                  ON h.hoarder_output_id = sfho.hoarder_output_id
                LEFT JOIN hoarder_output_media_assets homa
                  ON homa.hoarder_output_id = h.hoarder_output_id
                LEFT JOIN media_assets ma
                  ON ma.media_id = homa.media_id
                WHERE sod.sectionizer_output_id = ?
                ORDER BY ma.media_id ASC
                """,
                (sectionizer_output_id,),
            ).fetchall()

            lineage_sources: list[dict[str, object]] = []
            images: list[dict[str, str]] = []
            seen_source_ids: set[tuple[object, object, object]] = set()
            seen_media_ids: set[int] = set()

            for related in related_rows:
                source_key = (
                    related["hoarder_output_id"],
                    related["screened_file_id"],
                    related["doc_id"],
                )
                if source_key not in seen_source_ids:
                    lineage_sources.append(
                        {
                            "hoarder_output_id": related["hoarder_output_id"],
                            "source_id": related["source_id"],
                            "source_path": related["source_path"],
                            "hoarder_created_at": related["hoarder_created_at"],
                            "screened_file_id": related["screened_file_id"],
                            "is_selected": related["is_selected"],
                            "rejection_reason": related["rejection_reason"],
                            "screener_created_at": related["screener_created_at"],
                            "screener_processed_at": related["screener_processed_at"],
                            "doc_id": related["doc_id"],
                        }
                    )
                    seen_source_ids.add(source_key)

                media_id = related["media_id"]
                if isinstance(media_id, int) and media_id not in seen_media_ids:
                    artifact_path = str(related["artifact_path"] or "").strip()
                    mime_type = str(related["mime_type"] or "").strip()
                    if artifact_path:
                        images.append({"artifact_path": artifact_path, "mime_type": mime_type})
                        seen_media_ids.add(media_id)

            source_path = str(row["source_path"] or "")
            details_by_source_path[source_path] = {
                "sectionizer_output_id": sectionizer_output_id,
                "doc_id": row["doc_id"],
                "source_path": source_path,
                "llm_instruction": str(row["llm_instruction"] or ""),
                "llm_content": str(row["llm_content"] or ""),
                "output": payload,
                "author": str(row["author"] or ""),
                "lineage_sources": lineage_sources,
                "images": images,
            }

        return {
            "newsletter_run_id": int(run_row["newsletter_run_id"]),
            "run_timestamp": str(run_row["run_timestamp"] or ""),
            "created_at": str(run_row["created_at"] or ""),
            "output_markdown": str(run_row["output_markdown"] or ""),
            "output_html": str(run_row["output_html"] or ""),
            "output": output_payload,
            "config": _load_run_config(
                connection,
                int(run_row["newsletter_run_id"]),
                str(run_row["run_timestamp"] or ""),
            ),
            "sectionizer_details": details_by_source_path,
        }


def _configured_newsletter_date(detail: dict[str, object]) -> str:
    config = detail.get("config")
    if isinstance(config, dict):
        configured = str(config.get("newsletter_date") or "").strip()
        if configured:
            return configured
    return _default_run_config(str(detail.get("run_timestamp") or "")).get("newsletter_date", "")


def _newsletter_html_bytes(detail: dict[str, object]) -> bytes:
    output = detail.get("output")
    sections = output.get("sections") if isinstance(output, dict) else {}
    newsletter_date = escape(_configured_newsletter_date(detail))
    parts: list[str] = [
        "<!doctype html>",
        "<html>",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Weekly Newsletter</title>",
        "<style>",
        "body{margin:0;background:#eef4fb;font-family:Arial,sans-serif;color:#1a2740;}",
        ".shell{max-width:760px;margin:0 auto;padding:24px 14px 36px;}",
        ".hero{background:linear-gradient(135deg,#16325c,#1b5bbf);color:#fff;border-radius:24px;padding:28px 24px;box-shadow:0 16px 42px rgba(18,46,87,.22);}",
        ".hero h1{margin:0 0 8px;font-size:28px;line-height:1.1;}",
        ".hero p{margin:0;color:rgba(255,255,255,.86);font-size:14px;}",
        ".section{background:#fff;border-radius:22px;padding:22px 18px;margin-top:18px;box-shadow:0 10px 28px rgba(18,46,87,.08);}",
        ".section h2{margin:0 0 14px;color:#16325c;font-size:22px;}",
        ".story{padding:0 0 18px;margin:0 0 18px;border-bottom:1px solid #e3ebf4;}",
        ".story:last-child{padding-bottom:0;margin-bottom:0;border-bottom:none;}",
        ".story h3{margin:0 0 10px;font-size:20px;color:#10213f;}",
        ".story p{margin:0 0 10px;line-height:1.65;font-size:15px;color:#31415f;}",
        ".facts{margin:0;padding-left:18px;color:#31415f;}",
        ".facts li{margin:0 0 7px;line-height:1.55;}",
        ".image{margin:0 0 14px;}",
        ".image img{width:100%;height:auto;border-radius:16px;display:block;}",
        ".image figcaption{font-size:13px;color:#5d6c86;margin-top:8px;line-height:1.45;}",
        "@media print{body{background:#fff}.shell{max-width:none;padding:0}.section,.hero{box-shadow:none;border:1px solid #d9e3ef}}",
        "</style>",
        "</head>",
        "<body>",
        "<div class=\"shell\">",
        "<section class=\"hero\">",
        "<h1>Weekly Newsletter</h1>",
        f"<p>Edition date: {newsletter_date}</p>",
        "</section>",
    ]

    if isinstance(sections, dict) and sections:
        for section_name, section_payload in sections.items():
            parts.append("<section class=\"section\">")
            parts.append(f"<h2>{escape(str(section_name))}</h2>")
            stories = section_payload.get("stories") if isinstance(section_payload, dict) else []
            if isinstance(stories, list):
                for story in stories:
                    if not isinstance(story, dict):
                        continue
                    title = escape(str(story.get("newsletter_title") or "Untitled Story").strip())
                    summary = escape(str(story.get("summary") or "").strip())
                    image_path = str(story.get("image_path") or "").strip()
                    image_caption = escape(str(story.get("image_caption") or "").strip())
                    facts = story.get("summary_facts")

                    parts.append("<article class=\"story\">")
                    parts.append(f"<h3>{title}</h3>")
                    if image_path:
                        parts.append("<figure class=\"image\">")
                        parts.append(f"<img src=\"{escape(Path(image_path).as_uri())}\" alt=\"{title}\">")
                        if image_caption:
                            parts.append(f"<figcaption>{image_caption}</figcaption>")
                        parts.append("</figure>")
                    if summary:
                        parts.append(f"<p>{summary}</p>")
                    if isinstance(facts, list):
                        fact_items = [escape(str(fact).strip()) for fact in facts if str(fact).strip()]
                        if fact_items:
                            parts.append("<ul class=\"facts\">")
                            parts.extend(f"<li>{fact_text}</li>" for fact_text in fact_items)
                            parts.append("</ul>")
                    parts.append("</article>")
            parts.append("</section>")
    else:
        parts.append("<section class=\"section\"><p>No newsletter content available for this run.</p></section>")

    parts.extend(["</div>", "</body>", "</html>"])
    return "\n".join(parts).encode("utf-8")


def _newsletter_pdf_bytes(detail: dict[str, object]) -> bytes:
    output = detail.get("output")
    sections = output.get("sections") if isinstance(output, dict) else {}
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )
    styles = getSampleStyleSheet()
    newsletter_date = _configured_newsletter_date(detail)
    story: list[object] = [
        Paragraph("Weekly Newsletter", styles["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Edition date: {newsletter_date}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]

    if isinstance(sections, dict) and sections:
        for section_name, section_payload in sections.items():
            story.append(Paragraph(str(section_name), styles["Heading1"]))
            story.append(Spacer(1, 0.1 * inch))
            stories = section_payload.get("stories") if isinstance(section_payload, dict) else []
            if isinstance(stories, list) and stories:
                for item in stories:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("newsletter_title") or "Untitled Story").strip()
                    summary = str(item.get("summary") or "").strip()
                    facts = item.get("summary_facts")

                    story.append(Paragraph(title, styles["Heading2"]))
                    if summary:
                        story.append(Paragraph(summary, styles["BodyText"]))
                    if isinstance(facts, list):
                        for fact in facts:
                            fact_text = str(fact).strip()
                            if fact_text:
                                story.append(Paragraph(f"- {fact_text}", styles["BodyText"]))
                    story.append(Spacer(1, 0.12 * inch))
            else:
                story.append(Paragraph("No stories in this section.", styles["BodyText"]))
                story.append(Spacer(1, 0.12 * inch))
    else:
        markdown = str(detail.get("output_markdown") or "").strip() or "No newsletter content available for this run."
        story.append(Paragraph(markdown.replace("\n", "<br/>"), styles["BodyText"]))

    document.build(story)
    return buffer.getvalue()


def render_run_list(runs: list[dict[str, object]]) -> None:
    st.subheader("Newsletter Runs")
    if not runs:
        st.info("No newsletter runs found yet.")
        return
    st.dataframe(runs, use_container_width=True, hide_index=True)


def render_pending_queue(
    *,
    stage_name: str,
    queue_items: list[dict[str, object]],
    empty_message: str,
) -> None:
    st.subheader(stage_name)
    st.caption("Pending items are listed in execution order and reflect the rows the stage can act on next.")
    st.metric("Pending items", str(len(queue_items)))
    if not queue_items:
        st.success(empty_message)
        return
    st.dataframe(queue_items, use_container_width=True, hide_index=True)


def render_hoarder_sources_page() -> None:
    st.subheader("Hoarder Source Agents")
    st.caption("Trigger individual hoarder source agents and inspect the latest run recorded for each source.")
    statuses = load_hoarder_source_statuses()
    if not statuses:
        st.info("No hoarder sources are configured.")
        return

    summary_rows = [
        {
            "source_id": item["source_id"],
            "enabled": item["enabled"],
            "source_path": item["source_path"],
            "last_run_at": item["last_run_at"],
            "last_status": item["last_status"],
            "last_item_count": item["last_item_count"],
        }
        for item in statuses
    ]
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

    for source in statuses:
        with st.container(border=True):
            cols = st.columns([2, 1, 1, 1])
            cols[0].markdown(f"### `{source['source_id']}`")
            cols[0].caption(str(source["source_path"]))
            cols[1].metric("Enabled", "Yes" if bool(source["enabled"]) else "No")
            cols[2].metric("Last Status", str(source["last_status"] or "Never"))
            cols[3].metric("Last Run", str(source["last_run_at"] or "Never"))

            run_label = f"Run {source['source_id']}"
            if st.button(run_label, key=f"run-hoarder-source-{source['source_id']}", use_container_width=True):
                with st.spinner(f"Running hoarder source {source['source_id']}..."):
                    result = subprocess.run(
                        [sys.executable, str(RUN_HOARDER_SOURCE_PATH), "--source-id", str(source["source_id"])],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                    )
                st.cache_data.clear()
                if result.returncode == 0:
                    st.success(f"Hoarder source {source['source_id']} finished successfully.")
                else:
                    st.error(f"Hoarder source {source['source_id']} failed with exit code {result.returncode}.")
                if result.stdout.strip():
                    st.code(result.stdout[-12000:], language="text")
                if result.stderr.strip():
                    st.code(result.stderr[-12000:], language="text")
                st.rerun()

            if source["last_error"]:
                with st.expander("Last error", expanded=False):
                    st.code(str(source["last_error"]), language="text")


def render_story_card(story: dict[str, object], related_detail: dict[str, object] | None) -> None:
    title = str(story.get("newsletter_title") or "Untitled Story").strip()
    st.markdown(f"### {title}")
    st.caption(f"Source: {story.get('source_path') or ''}")
    cols = st.columns([1, 1, 2])
    cols[0].metric("Score", str(story.get("score") or ""))
    cols[1].metric("Doc ID", str(story.get("doc_id") or ""))
    cols[2].write(str(story.get("summary") or ""))

    facts = story.get("summary_facts")
    if isinstance(facts, list) and facts:
        st.markdown("**Facts**")
        for fact in facts:
            fact_text = str(fact).strip()
            if fact_text:
                st.write(f"- {fact_text}")

    if related_detail:
        images = related_detail.get("images")
        if isinstance(images, list) and images:
            st.markdown("**Images**")
            image_cols = st.columns(min(3, max(1, len(images))))
            for index, image in enumerate(images):
                if not isinstance(image, dict):
                    continue
                artifact_path = str(image.get("artifact_path") or "").strip()
                if not artifact_path:
                    continue
                column = image_cols[index % len(image_cols)]
                with column:
                    st.image(artifact_path, caption=artifact_path, use_container_width=True)
        else:
            st.caption("No images associated with this source.")

        with st.expander("Sectionizer detail and lineage"):
            output_payload = related_detail.get("output")
            if isinstance(output_payload, dict):
                evaluations = output_payload.get("section_evaluations")
                if isinstance(evaluations, list) and evaluations:
                    st.markdown("**Section Scores**")
                    st.dataframe(evaluations, use_container_width=True, hide_index=True)

            lineage_sources = related_detail.get("lineage_sources")
            if isinstance(lineage_sources, list) and lineage_sources:
                st.markdown("**Source Lineage**")
                st.dataframe(lineage_sources, use_container_width=True, hide_index=True)

            st.markdown("**Instruction Path**")
            st.code(str(related_detail.get("llm_instruction") or ""), language="text")
            st.markdown("**Content Path**")
            st.code(str(related_detail.get("llm_content") or ""), language="text")


def render_run_configuration(detail: dict[str, object]) -> None:
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    newsletter_date = _parse_default_newsletter_date(str(config.get("newsletter_date") or detail.get("run_timestamp") or ""))

    with st.form(f"newsletter-run-config-{detail['newsletter_run_id']}"):
        st.subheader("Run Configuration")
        st.caption("These settings are persisted per newsletter run and drive the review exports.")
        selected_date = st.date_input("Newsletter date", value=newsletter_date, format="YYYY-MM-DD")
        submitted = st.form_submit_button("Save configuration", use_container_width=True)

    if submitted:
        save_run_config(
            int(detail["newsletter_run_id"]),
            {
                "newsletter_date": selected_date.isoformat(),
            },
        )
        st.cache_data.clear()
        st.success("Newsletter configuration saved.")
        st.rerun()


def render_run_detail(detail: dict[str, object]) -> None:
    output = detail["output"] if isinstance(detail["output"], dict) else {}
    sections = output.get("sections") if isinstance(output, dict) else {}
    sectionizer_details = detail["sectionizer_details"] if isinstance(detail["sectionizer_details"], dict) else {}

    st.title(f"Newsletter Run #{detail['newsletter_run_id']}")
    meta_cols = st.columns(3)
    meta_cols[0].metric("Run Timestamp", str(detail["run_timestamp"]))
    meta_cols[1].metric("Created At", str(detail["created_at"]))
    meta_cols[2].metric("Newsletter Date", _configured_newsletter_date(detail))

    export_cols = st.columns(2)
    export_cols[0].download_button(
        "Download HTML",
        data=_newsletter_html_bytes(detail),
        file_name=f"newsletter_run_{detail['newsletter_run_id']}.html",
        mime="text/html",
        use_container_width=True,
    )
    export_cols[1].download_button(
        "Download PDF",
        data=_newsletter_pdf_bytes(detail),
        file_name=f"newsletter_run_{detail['newsletter_run_id']}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )

    overview_tab, config_tab, markdown_tab = st.tabs(["Overview", "Configuration", "Rendered Markdown"])
    with overview_tab:
        if isinstance(sections, dict):
            selected_sections = list(sections.keys())
            st.markdown("**Sections selected for the newsletter**")
            st.write(", ".join(selected_sections) if selected_sections else "None")

            for section_name, section_payload in sections.items():
                with st.container(border=True):
                    st.header(str(section_name))
                    st.caption(f"Stories: {section_payload.get('storyCount') if isinstance(section_payload, dict) else 0}")
                    stories = section_payload.get("stories") if isinstance(section_payload, dict) else []
                    if isinstance(stories, list):
                        for story in stories:
                            if not isinstance(story, dict):
                                continue
                            source_path = str(story.get("source_path") or "").strip()
                            related_detail = sectionizer_details.get(source_path) if isinstance(sectionizer_details, dict) else None
                            render_story_card(story, related_detail if isinstance(related_detail, dict) else None)
                            st.divider()
        else:
            st.info("No sections found for this run.")

    with config_tab:
        render_run_configuration(detail)

    with markdown_tab:
        st.code(str(detail["output_markdown"] or ""), language="markdown")


def render_pipeline_controls() -> None:
    st.sidebar.header("Pipeline")
    backend = st.sidebar.selectbox("Screener backend", ["hosted", "ollama"], index=0)
    if st.sidebar.button("Run pipeline", use_container_width=True):
        with st.sidebar:
            with st.spinner("Running pipeline..."):
                result = subprocess.run(
                    [sys.executable, str(RUN_PIPELINE_PATH), "--backend", backend],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                )
        st.cache_data.clear()
        refreshed_runs = load_newsletter_runs()
        if refreshed_runs:
            st.session_state["selected_newsletter_run_id"] = int(refreshed_runs[0]["newsletter_run_id"])
        st.sidebar.success(f"Pipeline finished with exit code {result.returncode}")
        if result.stdout.strip():
            st.sidebar.markdown("**stdout**")
            st.sidebar.code(result.stdout[-12000:], language="text")
        if result.stderr.strip():
            st.sidebar.markdown("**stderr**")
            st.sidebar.code(result.stderr[-12000:], language="text")
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Newsletter Observability",
        page_icon="📰",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_admin_theme()
    st.markdown(
        f"""
        <section class="dashboard-hero">
          <h1>Newsletter Observability</h1>
          <p>
            Review every newsletter run, trace each story back through the pipeline,
            inspect scores and facts, and export polished review artifacts without leaving the dashboard.
          </p>
          <div class="dashboard-chip-row">
            <span class="dashboard-chip">SQLite-backed lineage</span>
            <span class="dashboard-chip">HTML + PDF review exports</span>
            <span class="dashboard-chip">Source, image, and section traceability</span>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"SQLite source: {DB_PATH}")
    render_pipeline_controls()
    if st.sidebar.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    screener_pending = load_pending_screener_items()
    standardizer_pending = load_pending_standardizer_items()
    sectionizer_pending = load_pending_sectionizer_items()
    runs = load_newsletter_runs()

    hoarder_tab, screener_tab, standardizer_tab, sectionizer_tab, newsletter_tab = st.tabs(
        [
            "1. Hoarder",
            "2. Screener",
            "3. Standardizer",
            "4. Sectionizer",
            "5. Newsletter",
        ]
    )

    with hoarder_tab:
        render_hoarder_sources_page()

    with screener_tab:
        render_pending_queue(
            stage_name="Pending For Screener",
            queue_items=screener_pending,
            empty_message="No hoarder rows are waiting for the screener.",
        )

    with standardizer_tab:
        render_pending_queue(
            stage_name="Pending For Standardizer",
            queue_items=standardizer_pending,
            empty_message="No selected screener rows are waiting for standardization.",
        )

    with sectionizer_tab:
        render_pending_queue(
            stage_name="Pending For Sectionizer",
            queue_items=sectionizer_pending,
            empty_message="No standardized documents are waiting for sectionization.",
        )

    with newsletter_tab:
        render_run_list(runs)

        if not runs:
            st.info("No newsletter runs found yet.")
            return

        latest_run = runs[0]
        with st.container(border=True):
            st.subheader("Latest Run Summary")
            cols = st.columns(4)
            cols[0].metric("Run ID", str(latest_run["newsletter_run_id"]))
            cols[1].metric("Run Timestamp", str(latest_run["run_timestamp"]))
            cols[2].metric("Sections", str(latest_run["section_count"]))
            cols[3].metric("Stories", str(latest_run["story_count"]))
            st.caption(f"Created at: {latest_run['created_at']}")

        run_options = {
            f"Run #{run['newsletter_run_id']} | {run['run_timestamp']} | sections={run['section_count']} stories={run['story_count']}": int(
                run["newsletter_run_id"]
            )
            for run in runs
        }
        selected_run_id_from_state = st.session_state.get("selected_newsletter_run_id")
        option_labels = list(run_options.keys())
        default_index = 0
        if isinstance(selected_run_id_from_state, int):
            for index, label in enumerate(option_labels):
                if run_options[label] == selected_run_id_from_state:
                    default_index = index
                    break
        selected_label = st.sidebar.selectbox("Select newsletter run", option_labels, index=default_index)

        selected_run_id = run_options[selected_label]
        st.session_state["selected_newsletter_run_id"] = selected_run_id
        detail = load_run_detail(selected_run_id)
        if detail is None:
            st.error("Selected run could not be loaded from the database.")
            return

        render_run_detail(detail)


if __name__ == "__main__":
    main()
