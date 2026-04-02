from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.agents import BaseAgent

from ..config import SourceConfig
logger = logging.getLogger(__name__)


class NotImplementedSourceAgent(BaseAgent):
    def __init__(self, *, source: SourceConfig, source_name: str) -> None:
        super().__init__(
            name=f"{source.id}_hoarder",
            description=f"Placeholder {source_name} hoarder agent.",
        )
        self._source = source
        self._source_name = source_name

    async def _run_async_impl(self, ctx):
        raise NotImplementedError(
            f"{self._source_name} sub-agent is not implemented yet for source: {self._source.id}"
        )


def create_not_implemented_source_agent(*, source: SourceConfig, source_name: str) -> BaseAgent:
    return NotImplementedSourceAgent(source=source, source_name=source_name)


def parse_state_json_list(value: Any) -> list[dict[str, object]]:
    # Hoarder agents exchange JSON through session state, so accept either the
    # raw Python list or the serialized string form.
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if not isinstance(value, str):
        return []

    stripped = value.strip()
    if not stripped:
        return []

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


def merge_file_list(existing: Any, new_items: list[dict[str, object]]) -> list[dict[str, object]]:
    # Keep source agents append-only so multiple enabled sources can
    # contribute items during the same hoarder run.
    logger.debug("existing=%d", existing)
    merged = parse_state_json_list(existing)
    logger.debug("existing count=%d", len(merged))
    logger.debug("new_items count=%d", len(new_items))
    merged.extend(new_items)
    return merged
