from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator

from jinja2 import Environment, FileSystemLoader, select_autoescape
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

import pipeline_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / ".output"
NEWSLETTER_OUTPUT_DIR = OUTPUT_DIR / "newsletter"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
HTML_TEMPLATE_NAME = "mobile_newsletter.html.j2"
NEWSLETTER_TITLE = "Gagan Gaze"
NEWSLETTER_SLOGAN = "India for Space"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_run_date(run_timestamp: str | None) -> date:
    value = str(run_timestamp or "").strip()
    if not value:
        return _utc_now().date()
    for parser in (
        lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")).date(),
        lambda text: datetime.strptime(text, "%Y%m%d-%H%M%S").date(),
        lambda text: datetime.strptime(text, "%Y-%m-%d").date(),
    ):
        try:
            return parser(value)
        except ValueError:
            continue
    return _utc_now().date()


def _format_indian_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _format_filename_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _load_sectionizer_outputs() -> tuple[str | None, list[dict[str, Any]]]:
    """Load deduped unreferenced sectionizer rows from the API."""
    try:
        return pipeline_client.load_sectionizer_outputs_for_newsletter()
    except Exception:
        return None, []


def _load_media_assets() -> dict[int, list[dict[str, str]]]:
    """Load persisted media assets keyed by document id."""
    try:
        return pipeline_client.load_media_assets()
    except Exception:
        return {}


def _is_image_asset(asset: dict[str, str]) -> bool:
    mime_type = str(asset.get("mime_type") or "").lower()
    artifact_path = str(asset.get("artifact_path") or "").lower()
    return mime_type.startswith("image/") or artifact_path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def _pick_primary_image(doc_id: Any, media_assets: dict[int, list[dict[str, str]]]) -> str | None:
    try:
        normalized_doc_id = int(doc_id)
    except (TypeError, ValueError):
        return None

    for asset in media_assets.get(normalized_doc_id, []):
        if _is_image_asset(asset):
            path = str(asset.get("artifact_path") or "").strip()
            if path:
                return path
    return None


def _build_caption(title: str, summary: str) -> str:
    if summary:
        sentence = summary.split(". ")[0].strip()
        if sentence:
            return sentence.rstrip(".") + "."
    if title:
        return f"Visual associated with {title}."
    return "Visual associated with this story."


def _story_sort_key(story: dict[str, Any]) -> tuple[float, str]:
    return (-float(story.get("score") or 0), str(story.get("newsletter_title") or ""))


def _sorted_story_sections(stories_by_section: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "name": section_name,
            "stories": sorted(section_stories, key=_story_sort_key),
        }
        for section_name, section_stories in sorted(stories_by_section.items())
    ]


def _render_markdown(*, run_timestamp: str, stories_by_section: dict[str, list[dict[str, Any]]]) -> str:
    edition_date = _format_indian_date(_parse_run_date(run_timestamp))
    total_story_count = sum(len(items) for items in stories_by_section.values())
    lines: list[str] = [
        f"# {NEWSLETTER_TITLE}",
        "",
        f"- Edition date: `{edition_date}`",
        f"- Total stories: `{total_story_count}`",
        f"- Sections: `{len(stories_by_section)}`",
        "",
    ]

    for section_name in sorted(stories_by_section):
        section_stories = sorted(stories_by_section[section_name], key=_story_sort_key)
        lines.append("---")
        lines.append("")
        lines.append(f"## {section_name}")
        lines.append("")

        for story in section_stories:
            title = str(story.get("newsletter_title") or "Untitled Story").strip()
            lines.append(f"### {title}")
            lines.append("")

            image_path = str(story.get("image_path") or "").strip()
            if image_path:
                lines.append(f"![{title}]({image_path})")
                lines.append("")
                lines.append(f"*{story.get('image_caption') or ''}*")
                lines.append("")

            summary = str(story.get("summary") or "").strip()
            if summary:
                lines.append(summary)
                lines.append("")

            summary_facts = story.get("summary_facts") or []
            if isinstance(summary_facts, list) and summary_facts:
                lines.append("Key facts:")
                for fact in summary_facts:
                    fact_text = str(fact).strip()
                    if fact_text:
                        lines.append(f"- {fact_text}")
                lines.append("")

            source_path = str(story.get("source_path") or "").strip()
            if source_path:
                lines.append(f"Source: `{source_path}`")
                lines.append("")

    return "\n".join(lines).strip() + "\n"


def _build_newsletter_render_context(*, run_timestamp: str, stories_by_section: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sections = _sorted_story_sections(stories_by_section)
    total_story_count = sum(len(section["stories"]) for section in sections)
    edition_date = _format_indian_date(_parse_run_date(run_timestamp))
    return {
        "title": NEWSLETTER_TITLE,
        "slogan": NEWSLETTER_SLOGAN,
        "run_timestamp": run_timestamp,
        "edition_date": edition_date,
        "total_story_count": total_story_count,
        "sections": sections,
    }


@lru_cache(maxsize=1)
def _template_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_html(*, template_context: dict[str, Any]) -> str:
    template = _template_environment().get_template(HTML_TEMPLATE_NAME)
    return template.render(**template_context)


def _build_newsletter_instruction() -> str:
    return (
        "Generate a markdown newsletter grouped by section. "
        "Deduplicate stories by source path, preserve the sectionizer summaries and facts, "
        "and sort stories within each section by descending score."
    )


def _build_newsletter_content(sectionizer_outputs: list[dict[str, Any]]) -> str:
    return json.dumps(sectionizer_outputs, indent=2, ensure_ascii=True, default=str)


def _persist_newsletter_run(
    *,
    run_timestamp: str,
    llm_instruction: str,
    llm_content: str,
    newsletter_markdown: str,
    newsletter_html: str,
    output_payload: dict[str, Any],
    sectionizer_output_ids: list[int],
    newsletter_slug: str | None = None,
) -> int:
    """Insert one newsletter run row and the referenced sectionizer-output links via API."""
    return pipeline_client.create_newsletter_run(
        run_timestamp=run_timestamp,
        llm_instruction=llm_instruction,
        llm_content=llm_content,
        output_markdown=newsletter_markdown,
        output_html=newsletter_html,
        output_json=output_payload,
        sectionizer_output_ids=sectionizer_output_ids,
        newsletter_slug=newsletter_slug,
    )


class NewsletterGeneratorAgent(BaseAgent):
    """Builds a markdown newsletter from deduped sectionizer outputs and persists the run."""

    def __init__(self) -> None:
        super().__init__(
            name="newsletter_generator_agent",
            description="Aggregates sectionizer outputs, deduplicates stories, and renders a markdown newsletter.",
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Load sectionizer DB rows, render the newsletter, persist the run, and emit the summary."""
        run_timestamp, sectionizer_outputs = _load_sectionizer_outputs()
        media_assets = _load_media_assets()

        stories_by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sectionizer_output_ids: list[int] = []

        for payload in sectionizer_outputs:
            doc_id = payload.get("doc_id")
            source_path = str(payload.get("source_path") or "").strip()
            matches = payload.get("matches")
            sectionizer_output_id = payload.get("_sectionizer_output_id")
            if isinstance(sectionizer_output_id, int):
                sectionizer_output_ids.append(sectionizer_output_id)

            if not isinstance(matches, list) or not matches:
                continue

            primary_image = _pick_primary_image(doc_id, media_assets)

            for match in matches:
                if not isinstance(match, dict) or not bool(match.get("passes_threshold")):
                    continue

                newsletter_title = str(match.get("newsletter_title") or "").strip()
                summary = str(match.get("summary") or "").strip()
                story = {
                    "doc_id": doc_id,
                    "source_path": source_path,
                    "score": match.get("score") or 0,
                    "section": str(match.get("section") or "Uncategorized").strip() or "Uncategorized",
                    "newsletter_title": newsletter_title,
                    "summary": summary,
                    "summary_facts": match.get("summary_facts") or [],
                    "image_path": primary_image,
                    "image_caption": _build_caption(newsletter_title, summary) if primary_image else None,
                }
                stories_by_section[story["section"]].append(story)

        run_timestamp = run_timestamp or _utc_now().strftime("%Y%m%d-%H%M%S")
        llm_instruction = _build_newsletter_instruction()
        llm_content = _build_newsletter_content(sectionizer_outputs)
        template_context = _build_newsletter_render_context(
            run_timestamp=run_timestamp,
            stories_by_section=dict(stories_by_section),
        )
        edition_date = _parse_run_date(run_timestamp)
        file_date = _format_filename_date(edition_date)

        NEWSLETTER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        newsletter_markdown = _render_markdown(run_timestamp=run_timestamp, stories_by_section=dict(stories_by_section))
        newsletter_html = _render_html(template_context=template_context)

        # Build payload without paths first — run ID not yet known
        output_payload: dict[str, Any] = {
            "title": NEWSLETTER_TITLE,
            "runTimestamp": run_timestamp,
            "editionDate": _format_indian_date(edition_date),
            "storage": "postgresql.newsletter_runs",
            "generatedAt": _utc_now_iso(),
            "sectionCount": len(stories_by_section),
            "storyCount": sum(len(items) for items in stories_by_section.values()),
            "sections": {
                section_name: {
                    "storyCount": len(items),
                    "stories": items,
                }
                for section_name, items in sorted(stories_by_section.items())
            },
        }

        newsletter_slug = ctx.session.state.get("newsletter_slug")

        newsletter_run_id = _persist_newsletter_run(
            run_timestamp=run_timestamp,
            llm_instruction=llm_instruction,
            llm_content=llm_content,
            newsletter_markdown=newsletter_markdown,
            newsletter_html=newsletter_html,
            output_payload=output_payload,
            sectionizer_output_ids=sectionizer_output_ids,
            newsletter_slug=newsletter_slug,
        )

        markdown_path = NEWSLETTER_OUTPUT_DIR / f"gagan-gaze_{file_date}_run-{newsletter_run_id}.md"
        html_path = NEWSLETTER_OUTPUT_DIR / f"gagan-gaze_{file_date}_run-{newsletter_run_id}.html"
        markdown_path.write_text(newsletter_markdown, encoding="utf-8")
        html_path.write_text(newsletter_html, encoding="utf-8")

        output_payload["newsletterPath"] = str(markdown_path)
        output_payload["newsletterHtmlPath"] = str(html_path)
        output_payload["newsletterRunId"] = newsletter_run_id
        output_payload["sectionizerOutputIds"] = sectionizer_output_ids
        ctx.session.state["newsletter_markdown_path"] = str(markdown_path)
        ctx.session.state["newsletter_html_path"] = str(html_path)
        ctx.session.state["newsletter_output"] = json.dumps(output_payload)

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(output_payload, indent=2))]),
        )


newsletter_generator_agent = NewsletterGeneratorAgent()
