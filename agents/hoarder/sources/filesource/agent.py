from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ...config import SourceConfig
from ..common import merge_file_list

logger = logging.getLogger(__name__)


def _get_response_text(mcp_response: object) -> str:
    """Extract plain text from an MCP tool response across supported client shapes."""
    # MCP call_tool responses can be typed objects or plain dicts based on client version.
    if isinstance(mcp_response, dict):
        structured = mcp_response.get("structuredContent")
        if isinstance(structured, dict):
            content = structured.get("content")
            if isinstance(content, str):
                return content
        content_list = mcp_response.get("content")
        if isinstance(content_list, list) and content_list:
            first = content_list[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                return first["text"]

    structured = getattr(mcp_response, "structuredContent", None)
    if isinstance(structured, dict):
        content = structured.get("content")
        if isinstance(content, str):
            return content

    content_list = getattr(mcp_response, "content", None)
    if isinstance(content_list, list) and content_list:
        first = content_list[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text

    return ""


def _extract_file_entries(mcp_response: object) -> list[str]:
    """Parse filesystem MCP directory output into a flat list of file names."""
    raw_text = _get_response_text(mcp_response)
    entries: list[str] = []

    for line in raw_text.splitlines():
        line = line.strip()
        if line.startswith("[FILE]"):
            value = line.replace("[FILE]", "", 1).strip()
            if value:
                entries.append(value)

    return entries


def _parse_file_info(info_response: object, name: str, path: str) -> dict[str, object]:
    """Normalize file-info tool output into the shared hoarder metadata schema."""
    metadata: dict[str, object] = {"name": name, "path": path}

    structured: object | None = None
    if isinstance(info_response, dict):
        structured = info_response.get("structuredContent")
    else:
        structured = getattr(info_response, "structuredContent", None)

    if isinstance(structured, dict):
        size = structured.get("size") or structured.get("fileSize") or structured.get("length")
        created = structured.get("createdAt") or structured.get("created") or structured.get("birthtime")
        modified = structured.get("modifiedAt") or structured.get("modified") or structured.get("mtime")

        if isinstance(size, (int, float)):
            metadata["sizeBytes"] = int(size)
        if isinstance(created, str):
            metadata["createdAt"] = created
        if isinstance(modified, str):
            metadata["modifiedAt"] = modified

    return metadata


class FilesourceHoarderAgent(BaseAgent):
    """Retrieve local filesystem metadata through the MCP filesystem server."""

    def __init__(self, source: SourceConfig) -> None:
        """Initialize the filesystem hoarder agent for one configured source."""
        super().__init__(
            name="filesource_hoarder",
            description="Custom hoarder agent that retrieves files and metadata from the filesystem MCP server.",
        )
        self._source = source

    def _source_path(self) -> Path:
        """Resolve the configured filesystem root for this source."""
        return Path(self._source.path).resolve()

    @staticmethod
    def _server_params(source_path: Path) -> StdioServerParameters:
        """Build MCP server startup parameters for the filesystem source root."""
        return StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", str(source_path)],
        )

    async def _collect_file_metadata_with_mcp(self, source_path: Path) -> list[dict[str, object]]:
        """Query the filesystem MCP server for files and per-file metadata."""
        file_metadata: list[dict[str, object]] = []

        server_params = self._server_params(source_path)
        logger.debug("Starting filesystem MCP session for %s", source_path)
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                directory_response = await session.call_tool("list_directory", {"path": str(source_path)})
                files = _extract_file_entries(directory_response)
                logger.debug("Filesystem MCP reported %d entries for %s", len(files), source_path)

                for name in files:
                    file_path = str((source_path / name).resolve())
                    info_response = await session.call_tool("get_file_info", {"path": file_path})
                    file_metadata.append(_parse_file_info(info_response, name, file_path))

        logger.debug("Collected filesystem metadata for %d files from %s", len(file_metadata), source_path)
        return file_metadata

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Collect filesystem metadata, merge it into session state, and emit the source payload."""
        source_path = self._source_path()
        logger.debug("Running filesource hoarder for source_id=%s path=%s", self._source.id, source_path)
        try:
            file_metadata = await self._collect_file_metadata_with_mcp(source_path)
            output_text = json.dumps(
                {
                    "sourceId": self._source.id,
                    "sourcePath": str(source_path),
                    "fileCount": len(file_metadata),
                    "files": file_metadata,
                },
                indent=2,
            )
        except Exception as error:
            file_metadata = []
            logger.exception("Filesystem hoarder failed for %s", source_path)
            output_text = f"Failed to list files or fetch metadata from filesystem MCP server: {error}"

        merged_items = merge_file_list(ctx.session.state.get("file_list"), file_metadata)
        ctx.session.state["file_list"] = json.dumps(merged_items)
        logger.debug("Merged filesystem results into session state. total_items=%d", len(merged_items))

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=output_text)]),
        )


def create_filesource_hoarder_agent(source: SourceConfig) -> BaseAgent:
    """Create a filesystem hoarder agent for the resolved source configuration."""
    return FilesourceHoarderAgent(source)
