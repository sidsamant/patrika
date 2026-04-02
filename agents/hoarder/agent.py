from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from .config import SourceConfig, load_hoarder_config
from .sources.confluence.agent import create_confluence_hoarder_agent
from .sources.filesource.agent import create_filesource_hoarder_agent
from .sources.gdocs.agent import create_gdocs_hoarder_agent
from .sources.sharepoint.agent import create_sharepoint_hoarder_agent
from .sources.slack.agent import create_slack_hoarder_agent
from .sources.twitter.agent import create_twitter_hoarder_agent
from .sources.whatsapp.agent import create_whatsapp_hoarder_agent
from .sources.websource.agent import create_websource_hoarder_agent
from .storage import DB_PATH, persist_hoarder_payload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
HOARDER_LOG_PATH = LOGS_DIR / f"hoarder-{RUN_TIMESTAMP}.debug.log"


def _configure_logging() -> logging.Logger:
    """Attach console and file handlers for hoarder debug logs."""
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in root_logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    resolved_log_path = HOARDER_LOG_PATH.resolve()
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
load_dotenv(ENV_PATH)


def build_hoarder_source_agent(source: SourceConfig):
    """Create the sub-agent responsible for one enabled hoarder source."""
    builders = {
        "filesystem": create_filesource_hoarder_agent,
        "confluence": create_confluence_hoarder_agent,
        "gdocs": create_gdocs_hoarder_agent,
        "sharepoint": create_sharepoint_hoarder_agent,
        "slack": create_slack_hoarder_agent,
        "twitter": create_twitter_hoarder_agent,
        "whatsapp": create_whatsapp_hoarder_agent,
        "websource": create_websource_hoarder_agent,
    }
    builder = builders.get(source.id)
    if not builder:
        raise ValueError(f"Unsupported source id in config: {source.id}")
    return builder(source)


def _load_enabled_sub_agents():
    """Load config.yaml and instantiate the enabled source agents."""
    config = load_hoarder_config()
    enabled_sources = [source for source in config.sources if source.enabled]
    logger.debug("Building hoarder sub-agents for %d enabled sources", len(enabled_sources))
    return [build_hoarder_source_agent(source) for source in enabled_sources]


class PersistentHoarderSequentialAgent(SequentialAgent):
    """Sequential hoarder agent that persists the final merged output to SQLite."""

    async def _run_async_impl(self, ctx: InvocationContext):
        """Run hoarder sub-agents in memory, then append the final merged output to the DB."""
        async for event in super()._run_async_impl(ctx):
            yield event

        raw_file_list = ctx.session.state.get("file_list")
        persisted_count = persist_hoarder_payload(raw_file_list)
        logger.debug("Persisted %d hoarder rows to %s", persisted_count, DB_PATH)

    async def _run_live_impl(self, ctx: InvocationContext):
        """Mirror async execution for live runs."""
        async for event in self._run_async_impl(ctx):
            yield event


root_agent = PersistentHoarderSequentialAgent(
    name="document_hoarder_coordinator",
    description="Sequential hoarder agent for configured source retrieval.",
    sub_agents=_load_enabled_sub_agents(),
)
