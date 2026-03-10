from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
STANDARDIZER_DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"
SEGMENTIZER_OUTPUT_PATH = OUTPUTS_DIR / "segmentizer.json"
CONFIG_PATH = Path(__file__).with_name("config.json")
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt_template.md")
LLM_REQUEST_DELAY_SECONDS = max(float(os.getenv("SEGMENTIZER_LLM_DELAY_SECONDS", "2.0")), 0.0)


def _state_get(context: Any, key: str) -> Any:
    """Read a state value from either a readonly context or an invocation context."""
    state = getattr(context, "state", None)
    if state is not None:
        getter = getattr(state, "get", None)
        if callable(getter):
            return getter(key)

    session = getattr(context, "session", None)
    session_state = getattr(session, "state", None)
    if isinstance(session_state, dict):
        return session_state.get(key)

    return None


def _read_text_if_exists(path: Path) -> str | None:
    """Return stripped file contents when `path` exists, otherwise `None`."""
    try:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            return content or None
    except Exception:
        logger.exception("Failed reading text from %s", path)
        return None
    return None


def _parse_json_object(raw_text: str | None) -> dict[str, Any]:
    """Parse a JSON object string into a dictionary, falling back to `{}`."""
    if not raw_text:
        return {}
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.debug("Ignoring invalid JSON object payload.")
        return {}
    return value if isinstance(value, dict) else {}


def _load_config() -> dict[str, Any]:
    """Load the segmentizer configuration file from disk."""
    config = _parse_json_object(_read_text_if_exists(CONFIG_PATH))
    logger.debug("Loaded segmentizer config from %s: %s", CONFIG_PATH, json.dumps(config, ensure_ascii=True))
    return config


def _load_prompt_template() -> str:
    """Load the external prompt template used for Gemini requests."""
    template = _read_text_if_exists(PROMPT_TEMPLATE_PATH)
    if template:
        return template
    raise FileNotFoundError(f"Prompt template not found at {PROMPT_TEMPLATE_PATH}")


def _load_rows_from_db() -> list[dict[str, Any]]:
    """Load standardized document rows from the SQLite database if it exists."""
    if not STANDARDIZER_DB_PATH.exists():
        logger.debug("Standardizer database not found at %s", STANDARDIZER_DB_PATH)
        return []
    try:
        with sqlite3.connect(STANDARDIZER_DB_PATH) as connection:
            cursor = connection.execute(
                """
                SELECT
                  doc_id,
                  source_path,
                  author,
                  text_content,
                  metadata_json,
                  extraction_status,
                  extraction_error,
                  content_sha256,
                  modified_at,
                  created_at,
                  persisted_at
                FROM documents
                ORDER BY doc_id ASC
                """
            )
            rows: list[dict[str, Any]] = []
            for record in cursor.fetchall():
                metadata_raw = record[4]
                metadata: dict[str, Any] = {}
                if isinstance(metadata_raw, str) and metadata_raw.strip():
                    try:
                        loaded = json.loads(metadata_raw)
                        if isinstance(loaded, dict):
                            metadata = loaded
                    except json.JSONDecodeError:
                        metadata = {}
                rows.append(
                    {
                        "doc_id": record[0],
                        "source_path": record[1],
                        "author": record[2],
                        "text": record[3],
                        "metadata": metadata,
                        "extraction": {
                            "status": record[5],
                            "error": record[6],
                        },
                        "content_sha256": record[7],
                        "modified_at": record[8],
                        "created_at": record[9],
                        "persisted_at": record[10],
                    }
                )
            logger.debug("Loaded %d standardized rows from %s", len(rows), STANDARDIZER_DB_PATH)
            return rows
    except Exception:
        logger.exception("Failed loading standardized rows from %s", STANDARDIZER_DB_PATH)
        return []


def _load_standardized_rows(ctx: InvocationContext) -> tuple[list[dict[str, Any]], str]:
    """Resolve standardized rows from the SQLite database only."""
    from_db = _load_rows_from_db()
    if from_db:
        return from_db, str(STANDARDIZER_DB_PATH)
    return [], "none"


def _normalize_plaintext_rules(raw_rules: Any) -> list[dict[str, Any]]:
    """Normalize config rules into plain-text rule definitions for prompt/rendering."""
    normalized: list[dict[str, Any]] = []
    if isinstance(raw_rules, str):
        raw_items = [raw_rules]
    elif isinstance(raw_rules, list):
        raw_items = raw_rules
    else:
        raw_items = []

    for idx, raw_rule in enumerate(raw_items):
        if isinstance(raw_rule, str):
            text = raw_rule.strip()
        elif isinstance(raw_rule, dict):
            text = json.dumps(raw_rule, ensure_ascii=True, sort_keys=True)
        else:
            text = str(raw_rule).strip()
        if not text:
            continue
        normalized.append(
            {
                "index": idx,
                "text": text,
            }
        )
    return normalized


def _normalize_segment_definition(raw_segment: Any) -> dict[str, Any] | None:
    """Normalize one configured segment while preserving plain-text rules for the LLM."""
    if not isinstance(raw_segment, dict):
        return None

    name = str(raw_segment.get("name") or "").strip()
    if not name:
        return None

    min_score = _to_score(raw_segment.get("min_score"))
    rules = _normalize_plaintext_rules(raw_segment.get("rules"))
    return {
        "name": name,
        "min_score": min_score,
        "rules": rules,
    }


def _normalize_segment_definitions(raw_segments: Any) -> list[dict[str, Any]]:
    """Normalize configured segments for downstream prompt rendering and scoring."""
    if not isinstance(raw_segments, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_segment in raw_segments:
        segment = _normalize_segment_definition(raw_segment)
        if segment is not None:
            normalized.append(segment)
    return normalized


def _path_get(payload: dict[str, Any], dotted_path: str) -> Any:
    """Read a nested value from dictionaries/lists using dot-separated path syntax."""
    current: Any = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
        else:
            return None
    return current


def _render_prompt(
    template: str,
    *,
    user_prompt: str,
    row: dict[str, Any],
    segment_defs: list[dict[str, Any]],
) -> str:
    """Fill the prompt template with the current document and segment definitions."""
    rendered = template
    replacements = {
        "{{USER_PROMPT}}": user_prompt or "Map standardized documents to configured newsletter segments.",
        "{{DOC_ID}}": str(row.get("doc_id", "")),
        "{{DOCUMENT_JSON}}": json.dumps(row, indent=2, ensure_ascii=True, default=str),
        "{{DOCUMENT_TEXT}}": str(row.get("text") or ""),
        "{{SEGMENTS_JSON}}": json.dumps(segment_defs, indent=2, ensure_ascii=True, default=str),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def segmentizer_instruction_provider(context: ReadonlyContext | Any) -> str:
    """Render the LLM instruction from the external prompt template and current row state."""
    row = _state_get(context, "segmentizer_current_row")
    segment_defs = _state_get(context, "segmentizer_segment_defs")
    user_prompt = str(_state_get(context, "segmentizer_runner_prompt") or "")
    if not isinstance(row, dict):
        row = {}
    if not isinstance(segment_defs, list):
        segment_defs = []
    return _render_prompt(
        _load_prompt_template(),
        user_prompt=user_prompt,
        row=row,
        segment_defs=[item for item in segment_defs if isinstance(item, dict)],
    )


async def segmentizer_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Log the rendered prompt and request payload before the ADK LLM call."""
    runtime_instruction = segmentizer_instruction_provider(callback_context)
    callback_context.state["segmentizer_runtime_instruction"] = runtime_instruction
    logger.debug("Segmentizer prompt payload:\n%s", runtime_instruction)
    logger.debug("Segmentizer llm request contents: %s", getattr(llm_request, "contents", None))
    return None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Parse a JSON object from Gemini output, tolerating fenced or prefixed text."""
    stripped = raw_text.strip()
    candidates = [stripped]

    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    return {}


def _to_score(value: Any) -> float:
    """Clamp any numeric-like value into the 0..1 range used by segment scoring."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, score)), 4)


def _to_string_list(value: Any) -> list[str]:
    """Convert a JSON value into a list of non-empty strings."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _segment_key(value: Any) -> str:
    """Normalize segment names for stable matching between config and Gemini output."""
    return str(value or "").strip().lower()


def _normalize_rule_scores(raw_segment: dict[str, Any], segment_def: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """Merge Gemini rule scores with plain-text config rules and compute the final score."""
    raw_rule_scores = raw_segment.get("rule_scores")
    indexed_scores = {
        int(item.get("index")): item
        for item in raw_rule_scores
        if isinstance(raw_rule_scores, list)
        and isinstance(item, dict)
        and isinstance(item.get("index"), int)
    }

    total_rules = 0
    score_sum = 0.0
    normalized: list[dict[str, Any]] = []

    for idx, rule in enumerate(segment_def.get("rules") or []):
        if not isinstance(rule, dict):
            continue
        raw_rule = indexed_scores.get(idx, {})
        score = _to_score(raw_rule.get("score"))
        total_rules += 1
        score_sum += score
        normalized.append(
            {
                "index": idx,
                "rule": str(rule.get("text") or "").strip(),
                "score": score,
                "reason": str(raw_rule.get("reason") or "").strip(),
                "fact": str(raw_rule.get("fact") or "").strip(),
            }
        )

    final_score = round(score_sum / total_rules, 4) if total_rules > 0 else _to_score(raw_segment.get("overall_score"))
    return normalized, final_score


def _normalize_segment_result(segment_def: dict[str, Any], raw_segment: dict[str, Any] | None) -> dict[str, Any]:
    """Convert one raw Gemini segment evaluation into the persisted output schema."""
    raw_segment = raw_segment or {}
    normalized_rule_scores, computed_score = _normalize_rule_scores(raw_segment, segment_def)
    min_score = _to_score(segment_def.get("min_score"))
    summary = str(raw_segment.get("summary") or "").strip()
    newsletter_title = str(raw_segment.get("newsletter_title") or raw_segment.get("title") or "").strip()
    summary_facts = _to_string_list(raw_segment.get("summary_facts") or raw_segment.get("facts"))

    return {
        "segment": str(segment_def.get("name") or "").strip(),
        "score": computed_score,
        "min_score": min_score,
        "matched_rule_count": sum(1 for item in normalized_rule_scores if item["score"] > 0),
        "rule_scores": normalized_rule_scores,
        "newsletter_title": newsletter_title,
        "summary": summary,
        "summary_facts": summary_facts,
        "raw_llm_score": _to_score(raw_segment.get("overall_score")),
        "passes_threshold": computed_score >= min_score,
    }


def _normalize_llm_result(segment_defs: list[dict[str, Any]], raw_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Normalize the full Gemini payload into all evaluations, passing matches, and a doc summary."""
    raw_segments = raw_payload.get("segments")
    raw_segments_by_name = {
        _segment_key(item.get("segment") or item.get("name")): item
        for item in raw_segments
        if isinstance(raw_segments, list) and isinstance(item, dict)
    }

    evaluations = [
        _normalize_segment_result(segment_def, raw_segments_by_name.get(_segment_key(segment_def.get("name"))))
        for segment_def in segment_defs
        if isinstance(segment_def, dict)
    ]
    matches = [item for item in evaluations if item.get("passes_threshold")]
    matches.sort(key=lambda item: item.get("score", 0), reverse=True)
    document_summary = str(raw_payload.get("document_summary") or "").strip()
    return evaluations, matches, document_summary


def _content_to_text(content: types.Content | None) -> str:
    """Flatten ADK content parts into a single debug-friendly text string."""
    if content is None:
        return ""

    parts = getattr(content, "parts", None)
    if not isinstance(parts, list):
        return ""

    return "".join((getattr(part, "text", "") or "") for part in parts).strip()


class SegmentizerAgent(BaseAgent):
    """ADK-backed Gemini agent that evaluates documents against configured newsletter segments."""

    def __init__(self) -> None:
        """Initialize the segmentizer agent and its internal ADK LLM reviewer."""
        reviewer = LlmAgent(
            name="segmentizer_llm_reviewer",
            model=os.getenv("SEGMENTIZER_GEMINI_MODEL", "gemini-2.5-flash-lite"),
            description="Scores documents against configured newsletter segments.",
            instruction=segmentizer_instruction_provider,
            before_model_callback=segmentizer_before_model_callback,
            output_key="segmentizer_llm_output",
            generate_content_config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )

        super().__init__(
            name="document_segmentizer_agent",
            description="Uses an internal ADK LLM agent to evaluate standardized documents against configured newsletter segments.",
            sub_agents=[reviewer],
        )
        self._reviewer = reviewer

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Evaluate each standardized row with the internal ADK LLM agent and emit persisted mappings."""
        user_prompt = _content_to_text(ctx.user_content)
        if user_prompt:
            logger.debug("Segmentizer runner prompt: %s", user_prompt)

        config = _load_config()
        segment_defs = _normalize_segment_definitions(config.get("segments"))
        logger.debug("Segmentizer will evaluate %d segment definitions.", len(segment_defs))

        standardized_rows, row_source = _load_standardized_rows(ctx)
        logger.debug("Segmentizer row source: %s (%d rows)", row_source, len(standardized_rows))

        mappings: list[dict[str, Any]] = []
        for row_index, row in enumerate(standardized_rows):
            if row_index > 0 and LLM_REQUEST_DELAY_SECONDS > 0:
                logger.debug("Sleeping %.2f seconds before next segmentizer LLM call.", LLM_REQUEST_DELAY_SECONDS)
                await asyncio.sleep(LLM_REQUEST_DELAY_SECONDS)

            ctx.session.state["segmentizer_current_row"] = row
            ctx.session.state["segmentizer_segment_defs"] = segment_defs
            ctx.session.state["segmentizer_runner_prompt"] = user_prompt

            raw_response = ""
            async for event in self._reviewer.run_async(ctx):
                content = getattr(event, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if isinstance(parts, list):
                    text = "".join((getattr(part, "text", "") or "") for part in parts).strip()
                    if text:
                        raw_response = text

            if raw_response:
                logger.debug("Segmentizer raw Gemini response:\n%s", raw_response)

            raw_payload = _extract_json_object(raw_response)
            evaluations, matches, document_summary = _normalize_llm_result(segment_defs, raw_payload)
            mappings.append(
                {
                    "doc_id": row.get("doc_id"),
                    "source_path": _path_get(row, "metadata.filesystem.path")
                    or _path_get(row, "metadata.source.path"),
                    "document_summary": document_summary,
                    "segment_evaluations": evaluations,
                    "matches": matches,
                }
            )
            logger.debug(
                "Row %s produced %d passing segments out of %d evaluations.",
                row.get("doc_id"),
                len(matches),
                len(evaluations),
            )

        matched_rows = sum(1 for item in mappings if item.get("matches"))
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        output_payload = {
            "rowSource": row_source,
            "segmentConfigPath": str(CONFIG_PATH),
            "promptTemplatePath": str(PROMPT_TEMPLATE_PATH),
            "totalRows": len(standardized_rows),
            "matchedRows": matched_rows,
            "mappings": mappings,
        }
        SEGMENTIZER_OUTPUT_PATH.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")
        ctx.session.state["segment_mappings"] = json.dumps(output_payload)
        logger.debug("Segmentizer output written to %s", SEGMENTIZER_OUTPUT_PATH)
        logger.debug("Segmentizer output payload: %s", json.dumps(output_payload, ensure_ascii=True))

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(output_payload, indent=2))]),
        )


segmentizer_agent = SegmentizerAgent()
