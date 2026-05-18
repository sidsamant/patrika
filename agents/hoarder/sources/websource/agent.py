from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import psycopg2
import psycopg2.extras
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ...config import SourceConfig
from ...storage import persist_hoarder_payload, record_hoarder_source_run

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_CHECKPOINT_PATH = _PROJECT_ROOT / ".output" / "checkpoints" / "websource.json"


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> int:
    """Return the last processed scraped_items.id (0 if no checkpoint exists)."""
    if _CHECKPOINT_PATH.exists():
        try:
            data = json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
            return int(data.get("last_id", 0))
        except Exception:
            pass
    return 0


def _save_checkpoint(last_id: int) -> None:
    """Persist the checkpoint atomically."""
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_PATH.write_text(
        json.dumps({"last_id": last_id, "saved_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def _is_twitter_target(raw_url: Any) -> bool:
    url = str(raw_url or "").strip().lower()
    return "x.com/" in url or "twitter.com/" in url


def _looks_like_draft(*values: Any) -> bool:
    haystack = " ".join(str(v or "").strip().lower() for v in values if str(v or "").strip())
    return any(token in haystack for token in ("draft", "wip", "work in progress", "internal only"))


def _load_raw_item(raw_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

def _normalize_scraped_row(source: SourceConfig, row: dict[str, Any]) -> dict[str, object] | None:
    item_id = int(row["id"])
    raw_item = _load_raw_item(str(row["raw_json"] or ""))
    url = str(row["url"] or "").strip()
    source_page = str(row["source_page"] or "").strip()
    title = str(row["title"] or "").strip()
    description = str(row["description"] or "").strip()
    image = str(row["image"] or "").strip()
    item_type = str(row["item_type"] or "").strip()
    section = str(row["section"] or "").strip()
    published_date = str(row["published_date"] or "").strip()

    if not url or _is_twitter_target(url) or _is_twitter_target(source_page):
        return None
    if _looks_like_draft(title, description, url, source_page):
        return None

    source_name = str(row["source_name"] or "").strip()
    source_link = str(row["source_link"] or "").strip()
    content_text = "\n\n".join(part for part in [title, description] if part).strip()
    return {
        "sourceId": source.id,
        "sourceType": "webpage",
        "name": title or source_name or url,
        "path": url,
        "pageUrl": url,
        "pageTitle": title or source_name or url,
        "pageSummary": description,
        "contentText": content_text,
        "createdAt": published_date or str(row["first_seen_at_utc"] or "").strip() or None,
        "modifiedAt": str(row["last_seen_at_utc"] or "").strip() or None,
        "author": source_name or None,
        "abstract": description or None,
        "tags": [tag for tag in [section, item_type] if tag],
        "hasImages": bool(image),
        "frontImage": image or None,
        "mediaPaths": [image] if image else [],
        "temporal": published_date or None,
        "location": source_page or source_link or None,
        "extension": ".html",
        "crawlScrapedItemId": item_id,
        "crawlSourceId": row["crawl_source_id"],
        "crawlRunSourceId": row["run_source_id"],
        "crawlSourceName": source_name or None,
        "crawlSourceLink": source_link or None,
        "sourceMetadata": {
            "scraped_item": raw_item or {
                "item_id": row["item_id"],
                "section": section,
                "type": item_type,
                "title": title,
                "description": description,
                "published_date": published_date,
                "url": url,
                "image": image,
                "source_page": source_page,
            },
        },
    }


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def _get_database_url() -> str:
    url = os.environ.get("CRAWL4AI_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("CRAWL4AI_DATABASE_URL environment variable is not set")
    return url


def _fetch_new_rows(last_id: int) -> list[dict[str, Any]]:
    """Return scraped_items rows with id > last_id, ordered by id ASC."""
    database_url = _get_database_url()
    with psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  si.id,
                  si.source_id  AS crawl_source_id,
                  si.run_source_id,
                  si.item_id,
                  si.section,
                  si.item_type,
                  si.title,
                  si.description,
                  si.published_date,
                  si.url,
                  si.image,
                  si.read_time,
                  si.button_text,
                  si.external,
                  si.source_page,
                  si.raw_json,
                  si.first_seen_at_utc,
                  si.last_seen_at_utc,
                  s.name AS source_name,
                  s.link AS source_link
                FROM scraped_items si
                JOIN sources s ON s.id = si.source_id
                WHERE si.id > %(last_id)s
                ORDER BY si.id ASC
                """,
                {"last_id": last_id},
            )
            return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class WebSourceHoarderAgent(BaseAgent):
    """Read new Crawl4AI scraped_items rows (since last checkpoint) and persist them directly."""

    def __init__(self, source: SourceConfig) -> None:
        super().__init__(
            name="websource_hoarder",
            description="Reads new Crawl4AI scraped items since last checkpoint and persists them to the hoarder pipeline.",
        )
        self._source = source

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        database_url = _get_database_url()
        last_id = _load_checkpoint()
        logger.debug(
            "WebSource starting: source_id=%s checkpoint_last_id=%d", self._source.id, last_id
        )

        try:
            new_rows = await asyncio.to_thread(_fetch_new_rows, last_id)
        except Exception as error:
            record_hoarder_source_run(
                source_id=self._source.id,
                source_path=database_url,
                status="error",
                error_text=str(error),
            )
            raise

        collected_items: list[dict[str, object]] = []
        max_id_seen = last_id

        for row in new_rows:
            row_id = int(row["id"])
            if row_id > max_id_seen:
                max_id_seen = row_id
            try:
                normalized = _normalize_scraped_row(self._source, row)
                if normalized is not None:
                    collected_items.append(normalized)
            except Exception:
                logger.exception("WebSource failed normalising scraped_item id=%s", row_id)

        logger.debug(
            "WebSource: %d new rows fetched, %d accepted after filtering",
            len(new_rows),
            len(collected_items),
        )

        persisted_count = 0
        if collected_items:
            persisted_count = await asyncio.to_thread(persist_hoarder_payload, collected_items)

        if max_id_seen > last_id:
            _save_checkpoint(max_id_seen)
            logger.debug("WebSource checkpoint updated: last_id=%d", max_id_seen)

        record_hoarder_source_run(
            source_id=self._source.id,
            source_path=database_url,
            status="success",
            item_count=persisted_count,
        )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=json.dumps(
                            {
                                "sourceId": self._source.id,
                                "checkpointLastId": last_id,
                                "newRowsFetched": len(new_rows),
                                "itemsAccepted": len(collected_items),
                                "itemsPersisted": persisted_count,
                                "newCheckpointLastId": max_id_seen,
                            },
                            indent=2,
                        )
                    )
                ],
            ),
        )


def create_websource_hoarder_agent(source: SourceConfig) -> BaseAgent:
    """Create a WebSource hoarder agent backed by the Crawl4AI PostgreSQL database."""
    return WebSourceHoarderAgent(source)
