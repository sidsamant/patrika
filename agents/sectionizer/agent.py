from __future__ import annotations

import asyncio
from datetime import datetime
from functools import lru_cache
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, AsyncGenerator

from google import genai
from dotenv import load_dotenv
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
OUTPUTS_DIR = PROJECT_ROOT / ".output"
SECTIONIZER_OUTPUTS_DIR = OUTPUTS_DIR / "sectionizer"
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
SECTIONIZER_LOG_PATH = LOGS_DIR / f"sectionizer-{RUN_TIMESTAMP}.debug.log"
STANDARDIZER_DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"
CONFIG_PATH = Path(__file__).with_name("config.json")
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt_template.md")

load_dotenv(ENV_PATH)

LLM_REQUEST_DELAY_SECONDS = max(float(os.getenv("SECTIONIZER_LLM_DELAY_SECONDS", "2.0")), 0.0)


def _configure_logging() -> logging.Logger:
    """Attach console and file handlers for sectionizer debug logs."""
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
    """Return a masked representation of a secret for debug logging."""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def _get_llm_token_display() -> str:
    """Return a safe summary of the configured LLM credential."""
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        token = os.getenv(env_name)
        if token:
            return f"{env_name}={_mask_secret(token)}"
    return "no LLM token env var found"


@lru_cache(maxsize=1)
def _get_genai_client() -> genai.Client:
    """Create a Gemini client using the configured API credentials."""
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


def _count_token_parts(runtime_instruction: str, llm_request: LlmRequest) -> tuple[int | None, int | None, int | None]:
    """Count prompt tokens for the upcoming Gemini request using Gemini API-supported inputs."""
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


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _ensure_sectionizer_storage() -> None:
    """Create the sectionizer output directory when needed."""
    SECTIONIZER_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_sectionizer_schema(connection: sqlite3.Connection) -> None:
    """Ensure the sectionizer run history table exists in the standardizer DB."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sectionizer_outputs (
          sectionizer_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_id INTEGER NOT NULL,
          output_path TEXT NOT NULL,
          run_timestamp TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(doc_id) REFERENCES documents(doc_id)
        )
        """
    )
    connection.commit()


def _build_row_output_path(doc_id: Any) -> Path:
    """Build a unique per-run JSON output path for one standardized document row."""
    safe_doc_id = str(doc_id if doc_id is not None else "unknown").strip() or "unknown"
    return SECTIONIZER_OUTPUTS_DIR / f"doc_{safe_doc_id}_{RUN_TIMESTAMP}.json"


def _persist_row_output(
    *,
    connection: sqlite3.Connection,
    row: dict[str, Any],
    row_output_payload: dict[str, Any],
) -> dict[str, Any]:
    """Write one sectionizer JSON per standardized row and record the mapping table entry."""
    _ensure_sectionizer_storage()
    _ensure_sectionizer_schema(connection)

    output_path = _build_row_output_path(row.get("doc_id"))
    output_path.write_text(json.dumps(row_output_payload, indent=2), encoding="utf-8")

    created_at = _utc_now_iso()
    run_timestamp = RUN_TIMESTAMP
    cursor = connection.execute(
        """
        INSERT INTO sectionizer_outputs (doc_id, output_path, run_timestamp, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            row.get("doc_id"),
            str(output_path),
            run_timestamp,
            created_at,
        ),
    )
    connection.commit()

    return {
        "sectionizer_output_id": int(cursor.lastrowid),
        "doc_id": row.get("doc_id"),
        "output_path": str(output_path),
        "run_timestamp": run_timestamp,
        "created_at": created_at,
    }


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
    """Load the sectionizer configuration file from disk."""
    config = _parse_json_object(_read_text_if_exists(CONFIG_PATH))
    logger.debug("Loaded sectionizer config from %s: %s", CONFIG_PATH, json.dumps(config, ensure_ascii=True))
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
                WHERE text_content IS NOT NULL
                  AND TRIM(text_content) <> ''
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


def _normalize_section_definition(raw_section: Any) -> dict[str, Any] | None:
    """Normalize one configured section while preserving plain-text rules for the LLM."""
    if not isinstance(raw_section, dict):
        return None

    name = str(raw_section.get("name") or "").strip()
    if not name:
        return None

    min_score = _to_score(raw_section.get("min_score"))
    rules = _normalize_plaintext_rules(raw_section.get("rules"))
    return {
        "name": name,
        "min_score": min_score,
        "rules": rules,
    }


def _normalize_section_definitions(raw_sections: Any) -> list[dict[str, Any]]:
    """Normalize configured sections for downstream prompt rendering and scoring."""
    if not isinstance(raw_sections, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_section in raw_sections:
        section = _normalize_section_definition(raw_section)
        if section is not None:
            normalized.append(section)
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
    section_defs: list[dict[str, Any]],
) -> str:
    """Fill the static prompt template with the configured section definitions."""
    rendered = template
    replacements = {
        "{{SECTIONS_JSON}}": json.dumps(section_defs, indent=2, ensure_ascii=True, default=str),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _render_document_payload(row: dict[str, Any]) -> str:
    """Render the document-specific user message sent alongside the static instruction."""
    return (
        "Evaluate the current standardized document and return JSON only.\n\n"
        "Document metadata:\n"
        f"{json.dumps(row.get('metadata') or {}, indent=2, ensure_ascii=True, default=str)}\n\n"
        "Document text:\n"
        f"{str(row.get('text') or '')}"
    )


def _build_static_instruction(section_defs: list[dict[str, Any]]) -> str:
    """Build the stable LLM instruction from the external prompt template and section config."""
    return _render_prompt(
        _load_prompt_template(),
        section_defs=[item for item in section_defs if isinstance(item, dict)],
    )


async def sectionizer_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Log the static instruction and per-document request payload before the ADK LLM call."""
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
    """Clamp any numeric-like value into the 0..1 range used by section scoring."""
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


def _section_key(value: Any) -> str:
    """Normalize section names for stable matching between config and Gemini output."""
    return str(value or "").strip().lower()


def _normalize_rule_scores(raw_section: dict[str, Any], section_def: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """Merge Gemini rule scores with plain-text config rules and compute the final score."""
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
    """Convert one raw Gemini section evaluation into the persisted output schema."""
    raw_section = raw_section or {}
    normalized_rule_scores, computed_score = _normalize_rule_scores(raw_section, section_def)
    min_score = _to_score(section_def.get("min_score"))
    summary = str(raw_section.get("summary") or "").strip()
    newsletter_title = str(raw_section.get("newsletter_title") or raw_section.get("title") or "").strip()
    summary_facts = _to_string_list(raw_section.get("summary_facts") or raw_section.get("facts"))

    return {
        "section": str(section_def.get("name") or "").strip(),
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
    """Normalize the full Gemini payload into all evaluations, passing matches, and a doc summary."""
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


def _content_to_text(content: types.Content | None) -> str:
    """Flatten ADK content parts into a single debug-friendly text string."""
    if content is None:
        return ""

    parts = getattr(content, "parts", None)
    if not isinstance(parts, list):
        return ""

    return "".join((getattr(part, "text", "") or "") for part in parts).strip()


class SectionizerAgent(BaseAgent):
    """ADK-backed Gemini agent that evaluates documents against configured newsletter sections."""

    def __init__(self) -> None:
        """Initialize the sectionizer agent and its internal ADK LLM reviewer."""
        config = _load_config()
        section_defs = _normalize_section_definitions(config.get("sections"))
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

        config = _load_config()
        section_defs = _normalize_section_definitions(config.get("sections"))
        logger.debug("Sectionizer will evaluate %d section definitions.", len(section_defs))

        standardized_rows, row_source = _load_standardized_rows(ctx)
        logger.debug("Sectionizer row source: %s (%d rows)", row_source, len(standardized_rows))

        persisted_outputs: list[dict[str, Any]] = []
        with sqlite3.connect(STANDARDIZER_DB_PATH) as connection:
            _ensure_sectionizer_schema(connection)

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

                raw_payload = _extract_json_object(raw_response)
                evaluations, matches, document_summary = _normalize_llm_result(section_defs, raw_payload)
                row_output_payload = {
                    "rowSource": row_source,
                    "sectionConfigPath": str(CONFIG_PATH),
                    "promptTemplatePath": str(PROMPT_TEMPLATE_PATH),
                    "runTimestamp": RUN_TIMESTAMP,
                    "doc_id": row.get("doc_id"),
                    "source_path": _path_get(row, "metadata.filesystem.path")
                    or _path_get(row, "metadata.source.path"),
                    "document_summary": document_summary,
                    "section_evaluations": evaluations,
                    "matches": matches,
                }
                persisted_record = _persist_row_output(
                    connection=connection,
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
                    "Row %s produced %d passing sections out of %d evaluations and was written to %s.",
                    row.get("doc_id"),
                    len(matches),
                    len(evaluations),
                    persisted_record["output_path"],
                )

        matched_rows = sum(1 for item in persisted_outputs if item.get("match_count"))
        output_payload = {
            "rowSource": row_source,
            "sectionConfigPath": str(CONFIG_PATH),
            "promptTemplatePath": str(PROMPT_TEMPLATE_PATH),
            "runTimestamp": RUN_TIMESTAMP,
            "totalRows": len(standardized_rows),
            "matchedRows": matched_rows,
            "outputDirectory": str(SECTIONIZER_OUTPUTS_DIR),
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
