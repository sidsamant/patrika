from __future__ import annotations

from ...config import SourceConfig
from ..common import create_not_implemented_source_agent


def create_confluence_hoarder_agent(source: SourceConfig):
    return create_not_implemented_source_agent(source=source, source_name="Confluence")
