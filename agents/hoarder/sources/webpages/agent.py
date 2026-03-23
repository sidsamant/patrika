from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

try:
    import scrapy
    from scrapy.crawler import CrawlerProcess
    from scrapy.http import Response
    from scrapy.selector import Selector, SelectorList
except Exception:  # pragma: no cover - optional dependency guard
    scrapy = None
    CrawlerProcess = None
    Response = Any
    Selector = Any
    SelectorList = Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ...config import SourceConfig
from ..common import merge_file_list

PROJECT_ROOT = Path(__file__).resolve().parents[4]
WEBPAGE_STATE_PATH = PROJECT_ROOT / ".output" / "webpages_state.json"
SCRAPY_USER_AGENT = "newsletter-adk-webpage-monitor/2.0"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _normalize_selector_type(value: Any) -> str:
    return "xpath" if str(value or "").strip().lower() == "xpath" else "css"


def _load_page_configs(config_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        pages = payload
    elif isinstance(payload, dict):
        pages = payload.get("pages", [])
    else:
        pages = []

    normalized: list[dict[str, Any]] = []
    for item in pages:
        if not isinstance(item, dict):
            continue

        url = str(item.get("url") or item.get("page_url") or "").strip()
        if not url:
            continue

        selector_type = _normalize_selector_type(item.get("selector_type") or item.get("selectorType"))
        normalized.append(
            {
                "company": str(item.get("company") or "").strip(),
                "url": url,
                "page_name": str(item.get("page_name") or item.get("name") or "").strip(),
                "selector_type": selector_type,
                "article_selector": str(item.get("article_selector") or item.get("articleSelector") or "").strip(),
                "title_selector": str(item.get("title_selector") or item.get("titleSelector") or "").strip(),
                "link_selector": str(item.get("link_selector") or item.get("linkSelector") or "").strip(),
                "text_selector": str(item.get("text_selector") or item.get("textSelector") or "").strip(),
                "published_at_selector": str(
                    item.get("published_at_selector") or item.get("publishedAtSelector") or ""
                ).strip(),
                "page_content_selector": str(
                    item.get("page_content_selector") or item.get("pageContentSelector") or ""
                ).strip(),
                "meta": item.get("meta", {}) if isinstance(item.get("meta"), dict) else {},
            }
        )
    return normalized


def _load_state_map() -> dict[str, dict[str, Any]]:
    try:
        raw = WEBPAGE_STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _save_state_map(state_map: dict[str, dict[str, Any]]) -> None:
    WEBPAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEBPAGE_STATE_PATH.write_text(json.dumps(state_map, indent=2, ensure_ascii=True), encoding="utf-8")


def _selector_list(root: Selector | Response, query: str, selector_type: str) -> SelectorList:
    if selector_type == "xpath":
        return root.xpath(query)
    return root.css(query)


def _extract_first_value(root: Selector | Response, query: str, selector_type: str) -> str:
    if not query:
        return ""
    return _clean_text(_selector_list(root, query, selector_type).get() or "")


def _extract_joined_text(root: Selector | Response, query: str, selector_type: str) -> str:
    if not query:
        return ""
    return _clean_text(" ".join(item for item in _selector_list(root, query, selector_type).getall() if item))


def _element_text(element: Selector) -> str:
    return _clean_text(element.xpath("string(.)").get() or "")


def _build_page_record(
    target: dict[str, Any],
    response: Response,
    *,
    page_title: str,
    page_content: str,
    previous_hashes: set[str],
) -> dict[str, Any] | None:
    content_text = _clean_text(page_content)
    if not content_text:
        return None

    content_sha256 = _sha256_hex(content_text)
    return {
        "path": str(target.get("url") or response.url),
        "name": " - ".join(part for part in [target.get("company", ""), target.get("page_name", "")] if part),
        "company": target.get("company", ""),
        "pageUrl": response.url,
        "pageTitle": page_title,
        "pageSummary": content_text[:500],
        "contentText": content_text,
        "publishedAt": None,
        "createdAt": None,
        "modifiedAt": None,
        "sourceType": "webpage",
        "scrapeStatus": "ok",
        "scrapeError": None,
        "content_sha256": content_sha256,
        "isDuplicate": content_sha256 in previous_hashes,
        "meta": {
            **target.get("meta", {}),
            "record_type": "page",
        },
    }


def _build_article_record(
    target: dict[str, Any],
    response: Response,
    *,
    page_title: str,
    article: Selector,
    selector_type: str,
    previous_hashes: set[str],
) -> dict[str, Any] | None:
    title = _extract_joined_text(article, str(target.get("title_selector") or ""), selector_type)
    if not title:
        title = _extract_first_value(article, ".//h1/text()", "xpath")
    if not title:
        title = _extract_first_value(article, ".//h2/text()", "xpath")
    if not title:
        title = _extract_first_value(article, ".//h3/text()", "xpath")

    text_content = _extract_joined_text(article, str(target.get("text_selector") or ""), selector_type)
    if not text_content:
        text_content = _element_text(article)

    published_at = _extract_joined_text(article, str(target.get("published_at_selector") or ""), selector_type) or None
    raw_link = _extract_first_value(article, str(target.get("link_selector") or ""), selector_type)
    if not raw_link:
        raw_link = _extract_first_value(article, ".//a[1]/@href", "xpath")
    article_url = urljoin(response.url, raw_link) if raw_link else response.url

    combined_text = "\n".join(part for part in [title, published_at or "", text_content] if part)
    content_sha256 = _sha256_hex(combined_text)

    if not title and not text_content:
        return None

    return {
        "path": article_url,
        "name": title or " - ".join(part for part in [target.get("company", ""), target.get("page_name", "")] if part),
        "company": target.get("company", ""),
        "pageUrl": response.url,
        "pageTitle": page_title,
        "pageSummary": text_content[:500],
        "contentText": text_content,
        "publishedAt": published_at,
        "createdAt": published_at,
        "modifiedAt": None,
        "sourceType": "webpage",
        "scrapeStatus": "ok",
        "scrapeError": None,
        "content_sha256": content_sha256,
        "isDuplicate": content_sha256 in previous_hashes,
        "meta": {
            **target.get("meta", {}),
            "record_type": "news_article",
            "article_title": title,
            "page_name": target.get("page_name", ""),
        },
    }


def _parse_response_records(
    target: dict[str, Any],
    response: Response,
    previous_hashes: set[str],
) -> list[dict[str, Any]]:
    selector_type = str(target.get("selector_type") or "css")
    page_title = _extract_first_value(response, "title::text", "css") or _extract_first_value(response, "//title/text()", "xpath")
    page_content = _extract_joined_text(response, str(target.get("page_content_selector") or ""), selector_type)

    records: list[dict[str, Any]] = []
    article_selector = str(target.get("article_selector") or "").strip()
    if article_selector:
        for article in _selector_list(response, article_selector, selector_type):
            if not isinstance(article, Selector):
                continue
            record = _build_article_record(
                target,
                response,
                page_title=page_title,
                article=article,
                selector_type=selector_type,
                previous_hashes=previous_hashes,
            )
            if record is not None:
                records.append(record)

    if records:
        return records

    page_record = _build_page_record(
        target,
        response,
        page_title=page_title,
        page_content=page_content or _element_text(response.selector),
        previous_hashes=previous_hashes,
    )
    return [page_record] if page_record is not None else []


class _ConfiguredNewsSpider(scrapy.Spider if scrapy is not None else object):
    name = "newsletter_webpages"
    custom_settings = {
        "LOG_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_TIMEOUT": 30,
        "RETRY_ENABLED": False,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
    }

    def __init__(
        self,
        *,
        page_targets: list[dict[str, Any]],
        state_map: dict[str, dict[str, Any]],
        output_items: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self.page_targets = page_targets
        self.state_map = state_map
        self.output_items = output_items

    def start_requests(self) -> Iterable[Any]:
        for target in self.page_targets:
            yield scrapy.Request(
                url=str(target.get("url") or ""),
                callback=self.parse_page,
                errback=self.handle_error,
                cb_kwargs={"target": target},
                dont_filter=True,
                headers={"User-Agent": SCRAPY_USER_AGENT},
            )

    def parse_page(self, response: Response, target: dict[str, Any]) -> None:
        previous_entry = self.state_map.get(str(target.get("url") or ""), {})
        previous_hashes = {
            str(item).strip()
            for item in previous_entry.get("known_hashes", [])
            if str(item).strip()
        }

        records = _parse_response_records(target, response, previous_hashes)
        page_hash_source = _clean_text(response.text)
        page_hash = _sha256_hex(page_hash_source)

        combined_hashes = previous_hashes | {
            str(item.get("content_sha256") or "").strip()
            for item in records
            if isinstance(item, dict) and str(item.get("content_sha256") or "").strip()
        }
        self.state_map[str(target.get("url") or response.url)] = {
            "page_hash": page_hash,
            "known_hashes": sorted(combined_hashes),
            "last_crawled_at": _now_iso(),
        }
        self.output_items.extend(records)

    def handle_error(self, failure: Any) -> None:
        request = getattr(failure, "request", None)
        target = {}
        if request is not None:
            target = getattr(request, "cb_kwargs", {}).get("target", {}) or {}
        url = str(target.get("url") or getattr(request, "url", "") or "").strip()
        self.output_items.append(
            {
                "path": url,
                "name": " - ".join(part for part in [target.get("company", ""), target.get("page_name", "")] if part),
                "company": target.get("company", ""),
                "pageUrl": url,
                "pageTitle": None,
                "pageSummary": "",
                "contentText": "",
                "publishedAt": None,
                "createdAt": None,
                "modifiedAt": None,
                "sourceType": "webpage",
                "scrapeStatus": "error",
                "scrapeError": str(failure.value) if getattr(failure, "value", None) else str(failure),
                "content_sha256": None,
                "isDuplicate": False,
                "meta": {
                    **(target.get("meta", {}) if isinstance(target, dict) else {}),
                    "record_type": "error",
                },
            }
        )


def _run_scrapy_crawl(
    page_targets: list[dict[str, Any]],
    state_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if scrapy is None or CrawlerProcess is None:
        raise RuntimeError("Scrapy is not installed. Install it with `pip install Scrapy` before using webpages source.")

    output_items: list[dict[str, Any]] = []
    process = CrawlerProcess(
        settings={
            "LOG_ENABLED": False,
            "USER_AGENT": SCRAPY_USER_AGENT,
            "ROBOTSTXT_OBEY": False,
            "DOWNLOAD_TIMEOUT": 30,
            "RETRY_ENABLED": False,
            "COOKIES_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
        }
    )
    process.crawl(
        _ConfiguredNewsSpider,
        page_targets=page_targets,
        state_map=state_map,
        output_items=output_items,
    )
    process.start(stop_after_crawl=True, install_signal_handlers=False)
    return output_items


class PagePulseHoarderAgent(BaseAgent):
    def __init__(self, source: SourceConfig) -> None:
        super().__init__(
            name="page_pulse_hoarder",
            description="Collects company webpage and news article content with Scrapy.",
        )
        self._source = source

    def _config_path(self) -> Path:
        raw_path = Path(self._source.path)
        if raw_path.is_absolute():
            return raw_path
        return PROJECT_ROOT / raw_path

    async def _run_async_impl(self, ctx: InvocationContext) -> Iterable[Event]:
        config_path = self._config_path()
        state_map = _load_state_map()
        try:
            page_configs = _load_page_configs(config_path)
            output_items = await asyncio.to_thread(_run_scrapy_crawl, page_configs, state_map)
            _save_state_map(state_map)
        except Exception as error:
            output_items = [
                {
                    "path": str(config_path),
                    "name": "webpage-config",
                    "company": "",
                    "pageUrl": "",
                    "pageTitle": None,
                    "pageSummary": "",
                    "contentText": "",
                    "publishedAt": None,
                    "createdAt": None,
                    "modifiedAt": None,
                    "sourceType": "webpage",
                    "scrapeStatus": "error",
                    "scrapeError": f"Failed to crawl webpage source: {error}",
                    "content_sha256": None,
                    "isDuplicate": False,
                    "meta": {"record_type": "error"},
                }
            ]

        merged_items = merge_file_list(ctx.session.state.get("file_list"), output_items)
        ctx.session.state["file_list"] = json.dumps(merged_items)

        payload = {
            "sourceId": self._source.id,
            "configPath": str(config_path),
            "reportedCount": len(output_items),
            "results": output_items,
        }

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload, indent=2))]),
        )


def create_webpages_hoarder_agent(source: SourceConfig) -> BaseAgent:
    return PagePulseHoarderAgent(source)
