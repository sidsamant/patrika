from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ...config import SourceConfig
from ...storage import load_processed_hoarder_artifact_paths, record_hoarder_source_artifact, record_hoarder_source_run
from ..common import merge_file_list

logger = logging.getLogger(__name__)


def _is_twitter_target(raw_url: Any) -> bool:
    """Return whether a URL points to X/Twitter and should be excluded."""
    url = str(raw_url or "").strip().lower()
    return "x.com/" in url or "twitter.com/" in url


def _looks_like_draft(*values: Any) -> bool:
    """Return whether any metadata field suggests a draft or internal item."""
    haystack = " ".join(str(value or "").strip().lower() for value in values if str(value or "").strip())
    return any(token in haystack for token in ("draft", "wip", "work in progress", "internal only"))


def _load_raw_item(raw_json: str) -> dict[str, Any]:
    """Parse one Crawl4AI scraped-item JSON payload."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_key(scraped_item_id: int) -> str:
    """Build the audit key used to track already-hoarded crawl rows."""
    return f"scraped_item:{scraped_item_id}"


def _normalize_scraped_row(source: SourceConfig, row: sqlite3.Row) -> dict[str, object] | None:
    """Convert one Crawl4AI scraped_items row into the shared hoarder metadata schema."""
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


def _load_unread_scraped_rows(source: SourceConfig, db_path: Path) -> list[sqlite3.Row]:
    """Load unread non-Twitter Crawl4AI rows ordered by first-seen time."""
    processed_keys = load_processed_hoarder_artifact_paths(source.id)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
              si.id,
              si.source_id AS crawl_source_id,
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
            JOIN sources s
              ON s.id = si.source_id
            ORDER BY si.first_seen_at_utc ASC, si.id ASC
            """
        ).fetchall()

    unread_rows = [row for row in rows if _artifact_key(int(row["id"])) not in processed_keys]
    logger.debug(
        "WebSource found %d unread scraped_items rows out of %d total in %s",
        len(unread_rows),
        len(rows),
        db_path,
    )
    return unread_rows


class WebSourceHoarderAgent(BaseAgent):
    """Read Crawl4AI scraped_items rows and hoard non-Twitter web entries."""

    def __init__(self, source: SourceConfig) -> None:
        """Initialize the web-source hoarder agent for one configured Crawl4AI DB."""
        super().__init__(
            name="websource_hoarder",
            description="Custom hoarder agent that reads Crawl4AI scraped items and extracts non-Twitter web entries.",
        )
        self._source = source

    def _db_path(self) -> Path:
        """Resolve the configured Crawl4AI SQLite database path."""
        return Path(self._source.path).resolve()

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Collect unread Crawl4AI items, merge them into session state, and audit processed rows."""
        db_path = self._db_path()
        logger.debug("Running websource hoarder for source_id=%s db=%s", self._source.id, db_path)

        if not db_path.exists() or not db_path.is_file():
            record_hoarder_source_run(
                source_id=self._source.id,
                source_path=str(db_path),
                status="error",
                error_text=f"Crawl4AI database not found: {db_path}",
            )
            raise FileNotFoundError(f"WebSource Crawl4AI database not found: {db_path}")

        collected_items: list[dict[str, object]] = []
        unread_rows = _load_unread_scraped_rows(self._source, db_path)

        for row in unread_rows:
            scraped_item_id = int(row["id"])
            audit_key = _artifact_key(scraped_item_id)
            try:
                normalized = _normalize_scraped_row(self._source, row)
                if normalized is not None:
                    collected_items.append(normalized)
                record_hoarder_source_artifact(
                    source_id=self._source.id,
                    artifact_path=audit_key,
                    status="success",
                    error_text=None,
                )
                logger.debug("WebSource ingested scraped_item id=%s", scraped_item_id)
            except Exception as error:
                record_hoarder_source_artifact(
                    source_id=self._source.id,
                    artifact_path=audit_key,
                    status="error",
                    error_text=str(error),
                )
                logger.exception("WebSource failed reading scraped_item id=%s", scraped_item_id)

        record_hoarder_source_run(
            source_id=self._source.id,
            source_path=str(db_path),
            status="success",
            item_count=len(collected_items),
        )

        merged_items = merge_file_list(ctx.session.state.get("file_list"), collected_items)
        ctx.session.state["file_list"] = json.dumps(merged_items)
        logger.debug("Merged websource results into session state. total_items=%d", len(merged_items))

        output_text = json.dumps(
            {
                "sourceId": self._source.id,
                "sourcePath": str(db_path),
                "processedScrapedItemCount": len(unread_rows),
                "fileCount": len(collected_items),
                "files": collected_items,
            },
            indent=2,
        )
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=output_text)]),
        )


def create_websource_hoarder_agent(source: SourceConfig) -> BaseAgent:
    """Create a WebSource hoarder agent for the configured Crawl4AI database."""
    return WebSourceHoarderAgent(source)
