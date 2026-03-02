from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _get_response_text(mcp_response: object) -> str:
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
    def __init__(self) -> None:
        super().__init__(
            name="filesource_hoarder",
            description="Custom hoarder agent that retrieves files and metadata from the filesystem MCP server.",
        )

    @staticmethod
    def _source_path() -> Path:
        return Path(os.getenv("HOARDER_FILESOURCE_PATH", "D:/ai/adk/demofiles")).resolve()

    @staticmethod
    def _server_params(source_path: Path) -> StdioServerParameters:
        return StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", str(source_path)],
        )

    async def _collect_file_metadata_with_mcp(self, source_path: Path) -> list[dict[str, object]]:
        file_metadata: list[dict[str, object]] = []

        server_params = self._server_params(source_path)
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                directory_response = await session.call_tool("list_directory", {"path": str(source_path)})
                files = _extract_file_entries(directory_response)

                for name in files:
                    file_path = str((source_path / name).resolve())
                    info_response = await session.call_tool("get_file_info", {"path": file_path})
                    file_metadata.append(_parse_file_info(info_response, name, file_path))

        return file_metadata

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        source_path = self._source_path()
        try:
            file_metadata = await self._collect_file_metadata_with_mcp(source_path)
            output_text = json.dumps(
                {
                    "sourcePath": str(source_path),
                    "fileCount": len(file_metadata),
                    "files": file_metadata,
                },
                indent=2,
            )
        except Exception as error:
            file_metadata = []
            output_text = f"Failed to list files or fetch metadata from filesystem MCP server: {error}"

        ctx.session.state["file_list"] = json.dumps(file_metadata)

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=output_text)]),
        )


filesource_agent = FilesourceHoarderAgent()
