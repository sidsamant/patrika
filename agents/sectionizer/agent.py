from __future__ import annotations

import asyncio
from datetime import datetime
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator

from google import genai
from dotenv import load_dotenv
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

import pipeline_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
SECTIONIZER_LOG_PATH = LOGS_DIR / f"sectionizer-{RUN_TIMESTAMP}.debug.log"
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt_template.md")

load_dotenv(ENV_PATH)

LLM_REQUEST_DELAY_SECONDS = max(float(os.getenv("SECTIONIZER_LLM_DELAY_SECONDS", "2.0")), 0.0)


def _configure_logging() -> logging.Logger:
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in root_logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    resolved_log_path = SECTIONIZER_LOG_PATH.resolve()
    if not any(
        isinstance(handler, logging.FileHandler) and Path(getattr(handler, "baseFilename", "")).resolve() == resolved_log_path
        for handler in root_logger.handlers
    ):
        file_handler = logging.FileHandler(resolved_log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger(__name__)


logger = _configure_logging()


def _mask_secret(value: str, *, visible: int = 4) -> str:
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def _get_llm_token_display() -> str:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        token = os.getenv(env_name)
        if token:
            return f"{env_name}={_mask_secret(token)}"
    return "no LLM token env var found"


@lru_cache(maxsize=1)
def _get_genai_client() -> genai.Client:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


def _count_token_parts(runtime_instruction: str, llm_request: LlmRequest) -> tuple[int | None, int | None, int | None]:
    model_name = llm_request.model or os.getenv("SECTIONIZER_GEMINI_MODEL", "gemini-2.5-flash-lite")
    try:
        instruction_response = _get_genai_client().models.count_tokens(
            model=model_name,
            contents=runtime_instruction,
        )
    except Exception:
        logger.exception("Failed counting system-instruction tokens for model %s", model_name)
        return None, None, None

    try:
        contents_response = _get_genai_client().models.count_tokens(
            model=model_name,
            contents=llm_request.contents,
        )
    except Exception:
        logger.exception("Failed counting request-content tokens for model %s", model_name)
        return (
            int(getattr(instruction_response, "total_tokens", 0)) if isinstance(getattr(instruction_response, "total_tokens", None), int) else None,
            None,
            None,
        )

    instruction_tokens = getattr(instruction_response, "total_tokens", None)
    content_tokens = getattr(contents_response, "total_tokens", None)
    if not isinstance(instruction_tokens, int) or not isinstance(content_tokens, int):
        return None, None, None
    return instruction_tokens, content_tokens, instruction_tokens + content_tokens


def _state_get(context: Any, key: str) -> Any:
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
    try:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            return content or None
    except Exception:
        logger.exception("Failed reading text from %s", path)
        return None
    return None


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _load_categories_from_db(newsletter_slug: str | None = None) -> list[dict[str, Any]]:
    """Load sectionizer categories from the pipeline API (pipeline.sectionizer_categories table)."""
    try:
        if newsletter_slug:
            from agents import pipeline_client
            settings_payload = pipeline_client.load_newsletter_settings(newsletter_slug)
            return settings_payload.get("categories") or []
        
        categories = pipeline_client.load_sectionizer_categories()
        logger.debug("Loaded %d sectionizer categories from pipeline API", len(categories))
        return categories
    except Exception:
        logger.exception("Failed loading sectionizer categories from pipeline API")
        return []


def _load_prompt_template() -> str:
    template = _read_text_if_exists(PROMPT_TEMPLATE_PATH)
    if template:
        return template
    raise FileNotFoundError(f"Prompt template not found at {PROMPT_TEMPLATE_PATH}")


def _load_rows_from_db() -> list[dict[str, Any]]:
    """Load the latest unsectionized standardized rows, deduped by source path."""
    try:
        rows = pipeline_client.load_documents_for_sectionizing()
        logger.debug("Loaded %d deduped unprocessed standardized rows from API", len(rows))
        return rows
    except Exception:
        logger.exception("Failed loading standardized rows from API")
        return []


def _load_standardized_rows(ctx: InvocationContext) -> tuple[list[dict[str, Any]], str]:
    from_db = _load_rows_from_db()
    if from_db:
        return from_db, "postgresql.documents"
    return [], "none"


def _normalize_plaintext_rules(raw_rules: Any) -> list[dict[str, Any]]:
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


def _normalize_section_definition(raw_section: Any) -> dict[str, Any] | None:
    if not isinstance(raw_section, dict):
        return None

    name = str(raw_section.get("name") or "").strip()
    if not name:
        return None

    min_score = _to_score(raw_section.get("min_score"))
    objective = str(raw_section.get("objective") or "").strip() or None
    ai_instructions = str(raw_section.get("ai_instructions") or "").strip() or None
    rules = _normalize_plaintext_rules(raw_section.get("rules"))
    return {
        "sectionizer_category_id": raw_section.get("sectionizer_category_id"),
        "name": name,
        "objective": objective,
        "ai_instructions": ai_instructions,
        "min_score": min_score,
        "rules": rules,
    }


def _normalize_section_definitions(raw_sections: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_sections, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_section in raw_sections:
        section = _normalize_section_definition(raw_section)
        if section is not None:
            normalized.append(section)
    return normalized


def _path_get(payload: dict[str, Any], dotted_path: str) -> Any:
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
    section_defs: list[dict[str, Any]],
) -> str:
    rendered = template
    replacements = {
        "{{SECTIONS_JSON}}": json.dumps(section_defs, indent=2, ensure_ascii=True, default=str),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _render_document_payload(row: dict[str, Any]) -> str:
    return (
        "Evaluate the current standardized document and return JSON only.\n\n"
        "Document metadata:\n"
        f"{json.dumps(row.get('metadata') or {}, indent=2, ensure_ascii=True, default=str)}\n\n"
        "Document text:\n"
        f"{str(row.get('text') or '')}"
    )


def _build_static_instruction(section_defs: list[dict[str, Any]]) -> str:
    return _render_prompt(
        _load_prompt_template(),
        section_defs=[item for item in section_defs if isinstance(item, dict)],
    )


async def sectionizer_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    row = _state_get(callback_context, "sectionizer_current_row")
    if not isinstance(row, dict):
        row = {}
    runtime_instruction = getattr(llm_request.config, "system_instruction", "") or ""
    runtime_document_payload = _render_document_payload(row)
    callback_context.state["sectionizer_runtime_instruction"] = runtime_instruction
    callback_context.state["sectionizer_runtime_document_payload"] = runtime_document_payload
    llm_request.contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=runtime_document_payload)],
        )
    ]
    instruction_tokens, content_tokens, prompt_tokens = _count_token_parts(runtime_instruction, llm_request)
    logger.debug("Sectionizer LLM token: %s", _get_llm_token_display())
    logger.debug(
        "Sectionizer prompt token count: instruction=%s content=%s estimated_total=%s",
        instruction_tokens if instruction_tokens is not None else "unavailable",
        content_tokens if content_tokens is not None else "unavailable",
        prompt_tokens if prompt_tokens is not None else "unavailable",
    )
    logger.debug("Sectionizer static instruction:\n%s", runtime_instruction)
    logger.debug("Sectionizer llm request contents: %s", getattr(llm_request, "contents", None))
    return None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
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
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, score)), 4)


def _to_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _section_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_rule_scores(raw_section: dict[str, Any], section_def: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    raw_rule_scores = raw_section.get("rule_scores")
    if not isinstance(raw_rule_scores, list):
        raw_rule_scores = []
    indexed_scores = {
        int(item.get("index")): item
        for item in raw_rule_scores
        if isinstance(item, dict)
        and isinstance(item.get("index"), int)
    }

    total_rules = 0
    score_sum = 0.0
    normalized: list[dict[str, Any]] = []

    for idx, rule in enumerate(section_def.get("rules") or []):
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

    final_score = round(score_sum / total_rules, 4) if total_rules > 0 else _to_score(raw_section.get("overall_score"))
    return normalized, final_score


def _normalize_section_result(section_def: dict[str, Any], raw_section: dict[str, Any] | None) -> dict[str, Any]:
    raw_section = raw_section or {}
    normalized_rule_scores, computed_score = _normalize_rule_scores(raw_section, section_def)
    min_score = _to_score(section_def.get("min_score"))
    summary = str(raw_section.get("summary") or "").strip()
    newsletter_title = str(raw_section.get("newsletter_title") or raw_section.get("title") or "").strip()
    summary_facts = _to_string_list(raw_section.get("summary_facts") or raw_section.get("facts"))

    return {
        "category_id": section_def.get("sectionizer_category_id"),
        "section": str(section_def.get("name") or "").strip(),
        "objective": str(section_def.get("objective") or "").strip(),
        "score": computed_score,
        "min_score": min_score,
        "matched_rule_count": sum(1 for item in normalized_rule_scores if item["score"] > 0),
        "rule_scores": normalized_rule_scores,
        "newsletter_title": newsletter_title,
        "summary": summary,
        "summary_facts": summary_facts,
        "raw_llm_score": _to_score(raw_section.get("overall_score")),
        "passes_threshold": computed_score >= min_score,
    }


def _normalize_llm_result(section_defs: list[dict[str, Any]], raw_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    raw_sections = raw_payload.get("sections") or raw_payload.get("segments")
    raw_sections_by_name = {
        _section_key(item.get("section") or item.get("section_name") or item.get("segment") or item.get("name")): item
        for item in raw_sections
        if isinstance(raw_sections, list) and isinstance(item, dict)
    }

    evaluations = [
        _normalize_section_result(section_def, raw_sections_by_name.get(_section_key(section_def.get("name"))))
        for section_def in section_defs
        if isinstance(section_def, dict)
    ]
    matches = [item for item in evaluations if item.get("passes_threshold")]
    matches.sort(key=lambda item: item.get("score", 0), reverse=True)
    document_summary = str(raw_payload.get("document_summary") or "").strip()
    return evaluations, matches, document_summary


def _pick_primary_match(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not matches:
        return None
    first_match = matches[0]
    return first_match if isinstance(first_match, dict) else None


def _content_to_text(content: types.Content | None) -> str:
    if content is None:
        return ""

    parts = getattr(content, "parts", None)
    if not isinstance(parts, list):
        return ""

    return "".join((getattr(part, "text", "") or "") for part in parts).strip()


def _persist_row_output(
    *,
    row: dict[str, Any],
    row_output_payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one sectionizer output row via API."""
    source_path = str(row_output_payload.get("source_path") or "").strip() or None
    match_count = len(row_output_payload.get("matches") or []) if isinstance(row_output_payload.get("matches"), list) else 0
    category_id = row_output_payload.get("category_id")
    llm_instruction = str(row_output_payload.get("llm_instruction") or "").strip() or None
    llm_content = str(row_output_payload.get("llm_content") or "").strip() or None

    result = pipeline_client.create_sectionizer_output(
        doc_id=row.get("doc_id"),
        category_id=category_id if isinstance(category_id, int) else None,
        source_path=source_path,
        llm_instruction=llm_instruction,
        llm_content=llm_content,
        output_json=row_output_payload,
        match_count=match_count,
        run_timestamp=RUN_TIMESTAMP,
    )
    sectionizer_output_id = int(result.get("sectionizer_output_id") or 0)

    return {
        "sectionizer_output_id": sectionizer_output_id,
        "doc_id": row.get("doc_id"),
        "category_id": category_id if isinstance(category_id, int) else None,
        "source_path": source_path,
        "llm_instruction": llm_instruction,
        "llm_content": llm_content,
        "output_json": row_output_payload,
        "match_count": match_count,
        "run_timestamp": RUN_TIMESTAMP,
        "created_at": result.get("created_at"),
    }


class SectionizerAgent(BaseAgent):
    """ADK-backed Gemini agent that evaluates documents against configured newsletter sections."""

    def __init__(self) -> None:
        section_defs = _normalize_section_definitions(_load_categories_from_db())
        static_instruction = _build_static_instruction(section_defs)

        reviewer = LlmAgent(
            name="sectionizer_llm_reviewer",
            model=os.getenv("SECTIONIZER_GEMINI_MODEL", "gemini-2.5-flash-lite"),
            description="Scores documents against configured newsletter sections.",
            static_instruction=static_instruction,
            before_model_callback=sectionizer_before_model_callback,
            output_key="sectionizer_llm_output",
            generate_content_config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )

        super().__init__(
            name="document_sectionizer_agent",
            description="Uses an internal ADK LLM agent to evaluate standardized documents against configured newsletter sections.",
            sub_agents=[reviewer],
        )
        self._reviewer = reviewer

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Evaluate each standardized row with the internal ADK LLM agent and emit persisted mappings."""
        user_prompt = _content_to_text(ctx.user_content)
        if user_prompt:
            logger.debug("Sectionizer upstream runner prompt (not forwarded to Gemini): %s", user_prompt)

        newsletter_slug = ctx.session.state.get("newsletter_slug")
        custom_model = None
        custom_prompt = None
        if newsletter_slug:
            try:
                from agents import pipeline_client
                settings_payload = pipeline_client.load_newsletter_settings(newsletter_slug)
                settings = settings_payload.get("settings", {})
                custom_model = settings.get("sectionizer_model")
                custom_prompt = settings.get("sectionizer_prompt")
                if custom_model:
                    self._reviewer.model = custom_model
                    logger.debug("Sectionizer dynamic model override: %s", custom_model)
            except Exception as e:
                logger.error("Failed to load sectionizer newsletter settings: %s", e)

        section_defs = _normalize_section_definitions(_load_categories_from_db(newsletter_slug))
        logger.debug("Sectionizer will evaluate %d section definitions.", len(section_defs))

        if custom_prompt:
            static_instruction = _render_prompt(custom_prompt, section_defs=section_defs)
        else:
            static_instruction = _build_static_instruction(section_defs)
        
        self._reviewer.static_instruction = static_instruction

        standardized_rows, row_source = _load_standardized_rows(ctx)
        logger.debug("Sectionizer row source: %s (%d rows)", row_source, len(standardized_rows))

        persisted_outputs: list[dict[str, Any]] = []

        for row_index, row in enumerate(standardized_rows):
            if row_index > 0 and LLM_REQUEST_DELAY_SECONDS > 0:
                logger.debug("Sleeping %.2f seconds before next sectionizer LLM call.", LLM_REQUEST_DELAY_SECONDS)
                await asyncio.sleep(LLM_REQUEST_DELAY_SECONDS)

            ctx.session.state["sectionizer_current_row"] = row
            ctx.session.state["sectionizer_section_defs"] = section_defs

            raw_response = ""
            async for event in self._reviewer.run_async(ctx):
                content = getattr(event, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if isinstance(parts, list):
                    text = "".join((getattr(part, "text", "") or "") for part in parts).strip()
                    if text:
                        raw_response = text

            if raw_response:
                logger.debug("Sectionizer raw Gemini response:\n%s", raw_response)

            llm_instruction = str(ctx.session.state.get("sectionizer_runtime_instruction") or "").strip()
            llm_content = str(ctx.session.state.get("sectionizer_runtime_document_payload") or "").strip()
            raw_payload = _extract_json_object(raw_response)
            evaluations, matches, document_summary = _normalize_llm_result(section_defs, raw_payload)
            primary_match = _pick_primary_match(matches)
            primary_category_id = primary_match.get("category_id") if isinstance(primary_match, dict) else None
            row_output_payload = {
                "rowSource": row_source,
                "sectionConfigSource": "postgresql.sectionizer_categories",
                "promptTemplatePath": str(PROMPT_TEMPLATE_PATH),
                "runTimestamp": RUN_TIMESTAMP,
                "doc_id": row.get("doc_id"),
                "category_id": primary_category_id if isinstance(primary_category_id, int) else None,
                "category_name": primary_match.get("section") if isinstance(primary_match, dict) else None,
                "source_path": _path_get(row, "metadata.filesystem.path")
                or _path_get(row, "metadata.source.path"),
                "llm_instruction": llm_instruction,
                "llm_content": llm_content,
                "document_summary": document_summary,
                "section_evaluations": evaluations,
                "matches": matches,
            }
            persisted_record = _persist_row_output(
                row=row,
                row_output_payload=row_output_payload,
            )
            persisted_outputs.append(
                {
                    **persisted_record,
                    "match_count": len(matches),
                    "output": row_output_payload,
                }
            )
            logger.debug(
                "Row %s produced %d passing sections out of %d evaluations and was inserted into sectionizer_outputs with id=%s.",
                row.get("doc_id"),
                len(matches),
                len(evaluations),
                persisted_record["sectionizer_output_id"],
            )

        matched_rows = sum(1 for item in persisted_outputs if item.get("match_count"))
        output_payload = {
            "rowSource": row_source,
            "sectionConfigSource": "postgresql.sectionizer_categories",
            "promptTemplatePath": str(PROMPT_TEMPLATE_PATH),
            "runTimestamp": RUN_TIMESTAMP,
            "totalRows": len(standardized_rows),
            "matchedRows": matched_rows,
            "storage": "postgresql.sectionizer_outputs",
            "outputs": persisted_outputs,
        }
        ctx.session.state["section_mappings"] = json.dumps(output_payload)
        logger.debug("Sectionizer output payload: %s", json.dumps(output_payload, ensure_ascii=True))

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(output_payload, indent=2))]),
        )


sectionizer_agent = SectionizerAgent()
