from __future__ import annotations

from google.adk.agents import SequentialAgent

from .config import SourceConfig, load_hoarder_config
from .sources.confluence.agent import create_confluence_hoarder_agent
from .sources.filesource.agent import filesource_agent
from .sources.gdocs.agent import create_gdocs_hoarder_agent
from .sources.sharepoint.agent import create_sharepoint_hoarder_agent
from .sources.slack.agent import create_slack_hoarder_agent
from .sources.whatsapp.agent import create_whatsapp_hoarder_agent


def _build_source_agent(source: SourceConfig):
    builders = {
        "filesystem": lambda s: filesource_agent,
        "confluence": create_confluence_hoarder_agent,
        "gdocs": create_gdocs_hoarder_agent,
        "sharepoint": create_sharepoint_hoarder_agent,
        "slack": create_slack_hoarder_agent,
        "whatsapp": create_whatsapp_hoarder_agent,
    }
    builder = builders.get(source.id)
    if not builder:
        raise ValueError(f"Unsupported source id in config: {source.id}")
    return builder(source)


def _load_enabled_sub_agents():
    config = load_hoarder_config()
    enabled_sources = [s for s in config.sources if s.enabled]
    return [_build_source_agent(source) for source in enabled_sources]


root_agent = SequentialAgent(
    name="document_hoarder_coordinator",
    description="Sequential hoarder agent for configured source retrieval.",
    sub_agents=_load_enabled_sub_agents(),
)
