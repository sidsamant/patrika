from __future__ import annotations

import json
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types


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


def _to_pretty_json(value: Any, empty_fallback: str) -> str:
    if value is None:
        return empty_fallback
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return empty_fallback
        try:
            return json.dumps(json.loads(stripped), indent=2)
        except Exception:
            return json.dumps(stripped, indent=2)
    return json.dumps(value, indent=2)


def _screening_rules_json(context: ReadonlyContext | Any) -> str:
    """Render additional screening rules from state as pretty JSON."""
    screening_rules = _state_get(context, "screening_rules")
    return _to_pretty_json(screening_rules, "{}")


def reviewer_instruction_provider(context: ReadonlyContext | Any) -> str:
    """Build the stable system instruction for the screener reviewer."""
    custom_prompt = _state_get(context, "screener_prompt")
    if custom_prompt:
        return custom_prompt

    screening_rules_json = _screening_rules_json(context)

    return f"""You are a metadata screening agent.
Your job is to review file metadata and decide which files are eligible for ingestion.
Use the user message as the current batch of files to evaluate.

## Screening rules

### Apply these baseline rules:
1. Reject files that are draft.
2. Reject files that are confidential.
3. For multiple versions of the same logical document, select only the latest version.

### Interpretation hints:
- Use file name/path patterns like draft, wip, tmp for draft detection.
- Use file name/path patterns like confidential, secret, internal-only for confidentiality detection.
- For latest version, use version hints (v1, v2, final, rev) and timestamps (modifiedAt first, then createdAt).

### Additional screening rules:
{screening_rules_json}

## Output requirements
- Return ONLY valid JSON.
- Output must be a JSON object with a top-level "files" array.
- Include every input file exactly once.
- Preserve all original metadata fields from each input item.
- If input is missing/invalid, return {{"files":[]}}.

### JSON Schema of output items:
   {{
    "files":[
            {{
                "path":"path/to/file",
                "name": "file name",
                "createdAt": "timestamp",
                "modifiedAt": "timestamp",
                "isSelected": true,
                "rejectionReason": "reason for rejection if isSelected is false, otherwise null"
            }}
    ]
}}"""


def reviewer_content_provider(context: ReadonlyContext | Any) -> str:
    """Build the per-request user content containing the current screening batch."""
    file_list = _state_get(context, "file_list")
    file_list_json = _to_pretty_json(file_list, "[]")
    return (
        "Screen the following file metadata batch and return JSON only.\n\n"
        "Input files:\n"
        f"{file_list_json}"
    )


async def simple_before_model_modifier(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Split screener behavior into system instruction and per-request user content."""
    runtime_instruction = reviewer_instruction_provider(callback_context)
    runtime_content = reviewer_content_provider(callback_context)

    callback_context.state["runtime_instruction"] = runtime_instruction
    callback_context.state["runtime_content"] = runtime_content
    llm_request.contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=runtime_content)],
        )
    ]
    return None


# Alias preserves the JavaScript naming convention.
simpleBeforeModelModifier = simple_before_model_modifier
reviewerInstructionProvider = reviewer_instruction_provider
