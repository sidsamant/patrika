"""Sync HTTP client for the Sentinel Press pipeline REST API.

ADK agents import this module instead of touching Django ORM directly.
Configure via environment variables:
  PIPELINE_API_URL  — default http://localhost:8000
  PIPELINE_API_KEY  — must match Django's PIPELINE_API_KEY setting
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

_BASE_URL = os.environ.get("PIPELINE_API_URL", "http://localhost:8000").rstrip("/")
_API_KEY = os.environ.get("PIPELINE_API_KEY", "dev-pipeline-key")
_HEADERS = {"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"}
_TIMEOUT = 60.0


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    url = f"{_BASE_URL}/api/pipeline/{path}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(url, headers=_HEADERS, params=params or {})
        resp.raise_for_status()
        return resp.json()


def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    url = f"{_BASE_URL}/api/pipeline/{path}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, headers=_HEADERS, content=json.dumps(body or {}))
        resp.raise_for_status()
        return resp.json()


def _patch(path: str, body: dict | None = None) -> dict[str, Any]:
    url = f"{_BASE_URL}/api/pipeline/{path}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.patch(url, headers=_HEADERS, content=json.dumps(body or {}))
        resp.raise_for_status()
        return resp.json()


# ── Pipeline / Agent run lifecycle ────────────────────────────────────────────

def create_pipeline_run(triggered_by: str) -> dict[str, Any]:
    return _post("runs/", {"triggered_by": triggered_by})


def finish_pipeline_run(pipeline_run_id: int, status: str) -> None:
    _patch(f"runs/{pipeline_run_id}/", {"status": status})


def create_agent_run(agent_name: str, pipeline_run_id: int | None = None) -> dict[str, Any]:
    return _post("agent-runs/", {"agent_name": agent_name, "pipeline_run_id": pipeline_run_id})


def finish_agent_run(
    agent_run_id: int,
    *,
    status: str,
    items_in: int | None = None,
    items_out: int | None = None,
    error_text: str | None = None,
) -> None:
    body: dict[str, Any] = {"status": status}
    if items_in is not None:
        body["items_in"] = items_in
    if items_out is not None:
        body["items_out"] = items_out
    if error_text is not None:
        body["error_text"] = error_text
    _patch(f"agent-runs/{agent_run_id}/", body)


# ── Hoarder ───────────────────────────────────────────────────────────────────

def persist_hoarder_batch(items: list[dict[str, Any]], agent_run_id: int | None = None) -> int:
    result = _post("hoarder-outputs/", {"items": items, "agent_run_id": agent_run_id})
    return int(result.get("created") or 0)


def load_hoarder_rows_for_screening() -> list[dict[str, Any]]:
    return _get("hoarder-outputs/for-screening/").get("items") or []


def mark_hoarder_rows_screened(hoarder_output_ids: list[int]) -> str | None:
    ids = [v for v in hoarder_output_ids if isinstance(v, int)]
    if not ids:
        return None
    result = _post("hoarder-outputs/mark-screened/", {"ids": ids})
    return result.get("screened_at")


def record_hoarder_source_run(
    *,
    source_id: str,
    source_path: str | None,
    status: str,
    item_count: int | None = None,
    error_text: str | None = None,
) -> int:
    result = _post("hoarder-source-runs/", {
        "source_id": source_id,
        "source_path": source_path,
        "status": status,
        "item_count": item_count,
        "error_text": error_text,
    })
    return int(result.get("hoarder_source_run_id") or 0)


def load_processed_hoarder_artifact_paths(source_id: str) -> set[str]:
    result = _get("hoarder-source-artifacts/", {"source_id": source_id})
    return set(result.get("paths") or [])


def record_hoarder_source_artifact(
    *,
    source_id: str,
    artifact_path: str,
    status: str,
    error_text: str | None = None,
) -> int:
    result = _post("hoarder-source-artifacts/", {
        "source_id": source_id,
        "artifact_path": artifact_path,
        "status": status,
        "error_text": error_text,
    })
    return int(result.get("hoarder_source_artifact_id") or 0)


# ── Screener ──────────────────────────────────────────────────────────────────

def persist_screened_batch(items: list[dict[str, Any]], agent_run_id: int | None = None) -> int:
    result = _post("screened-files/", {"items": items, "agent_run_id": agent_run_id})
    return int(result.get("created") or 0)


def mark_screened_file_processed(screened_file_id: int) -> str | None:
    result = _patch(f"screened-files/{screened_file_id}/mark-processed/")
    return result.get("processed_at")


# ── Standardizer ──────────────────────────────────────────────────────────────

def load_screened_files_for_standardization() -> list[dict[str, Any]]:
    return _get("screened-files/for-standardization/").get("items") or []


def standardize_document(
    *,
    source_path: str,
    author: str | None,
    text_content: str | None,
    metadata_json: dict,
    extraction_status: str,
    extraction_error: str | None,
    content_sha256: str | None,
    modified_at: str | None,
    created_at: str | None,
    screened_file_id: int | None = None,
    hoarder_output_id: int | None = None,
    media: list[dict[str, str]] | None = None,
    agent_run_id: int | None = None,
) -> dict[str, Any]:
    return _post("documents/", {
        "source_path": source_path,
        "author": author,
        "text_content": text_content,
        "metadata_json": metadata_json,
        "extraction_status": extraction_status,
        "extraction_error": extraction_error,
        "content_sha256": content_sha256,
        "modified_at": modified_at,
        "created_at": created_at,
        "screened_file_id": screened_file_id,
        "hoarder_output_id": hoarder_output_id,
        "media": media or [],
        "agent_run_id": agent_run_id,
    })


# ── Sectionizer ───────────────────────────────────────────────────────────────

def load_documents_for_sectionizing() -> list[dict[str, Any]]:
    return _get("documents/for-sectionizing/").get("items") or []


def load_sectionizer_categories() -> list[dict[str, Any]]:
    return _get("sectionizer-categories/").get("items") or []


def create_sectionizer_output(
    *,
    doc_id: int,
    category_id: int | None,
    source_path: str | None,
    llm_instruction: str | None,
    llm_content: str | None,
    output_json: dict,
    match_count: int,
    run_timestamp: str,
    agent_run_id: int | None = None,
) -> dict[str, Any]:
    return _post("sectionizer-outputs/", {
        "doc_id": doc_id,
        "category_id": category_id,
        "output_path": "",
        "source_path": source_path,
        "llm_instruction": llm_instruction,
        "llm_content": llm_content,
        "output_json": output_json,
        "match_count": match_count,
        "run_timestamp": run_timestamp,
        "agent_run_id": agent_run_id,
    })


# ── Newsletter generator ──────────────────────────────────────────────────────

def load_sectionizer_outputs_for_newsletter() -> tuple[str | None, list[dict[str, Any]]]:
    rows = _get("sectionizer-outputs/for-newsletter/").get("items") or []
    if not rows:
        return None, []
    latest = max(str(item.get("runTimestamp") or item.get("run_timestamp") or "") for item in rows)
    return latest or None, rows


def load_media_assets() -> dict[int, list[dict[str, str]]]:
    raw = _get("media-assets/").get("by_doc_id") or {}
    return {int(k): v for k, v in raw.items()}


def create_newsletter_run(
    *,
    run_timestamp: str,
    llm_instruction: str,
    llm_content: str,
    output_markdown: str,
    output_html: str,
    output_json: dict,
    sectionizer_output_ids: list[int],
    pipeline_run_id: int | None = None,
    agent_run_id: int | None = None,
) -> int:
    result = _post("newsletter-runs/", {
        "run_timestamp": run_timestamp,
        "llm_instruction": llm_instruction,
        "llm_content": llm_content,
        "output_markdown": output_markdown,
        "output_html": output_html,
        "output_json": output_json,
        "sectionizer_output_ids": sectionizer_output_ids,
        "pipeline_run_id": pipeline_run_id,
        "agent_run_id": agent_run_id,
    })
    return int(result.get("newsletter_run_id") or 0)
