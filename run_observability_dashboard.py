from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
import sys

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"
RUN_PIPELINE_PATH = PROJECT_ROOT / "run_pipeline.py"


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
    _ensure_column(connection, "newsletter_runs", "llm_instruction", "TEXT")
    _ensure_column(connection, "newsletter_runs", "llm_content", "TEXT")
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
def load_run_detail(newsletter_run_id: int) -> dict[str, object] | None:
    if not DB_PATH.exists():
        return None

    with _open_connection() as connection:
        run_row = connection.execute(
            """
            SELECT newsletter_run_id, run_timestamp, output_markdown, output_json, created_at
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
            "output": output_payload,
            "sectionizer_details": details_by_source_path,
        }


def render_run_list(runs: list[dict[str, object]]) -> None:
    st.subheader("Newsletter Runs")
    if not runs:
        st.info("No newsletter runs found yet.")
        return
    st.dataframe(runs, use_container_width=True, hide_index=True)


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


def render_run_detail(detail: dict[str, object]) -> None:
    output = detail["output"] if isinstance(detail["output"], dict) else {}
    sections = output.get("sections") if isinstance(output, dict) else {}
    sectionizer_details = detail["sectionizer_details"] if isinstance(detail["sectionizer_details"], dict) else {}

    st.title(f"Newsletter Run #{detail['newsletter_run_id']}")
    meta_cols = st.columns(3)
    meta_cols[0].metric("Run Timestamp", str(detail["run_timestamp"]))
    meta_cols[1].metric("Created At", str(detail["created_at"]))
    meta_cols[2].metric("Sections", str(output.get("sectionCount") or 0))

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

    with st.expander("Rendered Markdown", expanded=False):
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
    st.title("Newsletter Observability")
    st.caption(f"SQLite source: {DB_PATH}")
    render_pipeline_controls()

    runs = load_newsletter_runs()
    render_run_list(runs)

    if not runs:
        return

    run_options = {
        f"Run #{run['newsletter_run_id']} | {run['run_timestamp']} | sections={run['section_count']} stories={run['story_count']}": int(
            run["newsletter_run_id"]
        )
        for run in runs
    }
    selected_label = st.sidebar.selectbox("Select newsletter run", list(run_options.keys()))
    if st.sidebar.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    selected_run_id = run_options[selected_label]
    detail = load_run_detail(selected_run_id)
    if detail is None:
        st.error("Selected run could not be loaded from the database.")
        return

    render_run_detail(detail)


if __name__ == "__main__":
    main()
