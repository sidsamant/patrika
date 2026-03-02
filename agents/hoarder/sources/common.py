from __future__ import annotations

from google.adk.agents import BaseAgent

from ..config import SourceConfig


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
