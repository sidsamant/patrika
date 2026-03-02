from __future__ import annotations

import json
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import LlmRequest, LlmResponse


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


def reviewer_instruction_provider(context: ReadonlyContext | Any) -> str:
    file_list = _state_get(context, "file_list")
    screening_rules = _state_get(context, "screening_rules")

    file_list_json = _to_pretty_json(file_list, "[]")
    screening_rules_json = _to_pretty_json(screening_rules, "{}")

    return f"""You are a metadata screening agent.
## List of files to screen
    Input files with metadata as JSON array:
    {file_list_json}

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
- Output must be a JSON array.
- Include every input file exactly once.
- Preserve all original metadata fields from each input item.
- If input is missing/invalid, return an empty JSON array.

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


async def simple_before_model_modifier(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    # Keep callback side effects minimal and deterministic. The instruction
    # provider remains the source of truth for prompt construction.
    runtime_instruction = reviewer_instruction_provider(callback_context)

    callback_context.state["runtime_instruction"] = runtime_instruction
    return None


# Alias preserves the JavaScript naming convention.
simpleBeforeModelModifier = simple_before_model_modifier
reviewerInstructionProvider = reviewer_instruction_provider
