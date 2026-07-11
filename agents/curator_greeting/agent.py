from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from google.adk.tools.mcp_tool import McpToolset
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import pipeline_client

logger = logging.getLogger(__name__)

async def get_pipeline_status_via_mcp() -> dict[str, int]:
    sentinelpress_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "sentinelpress")
    )
    python_exe = os.path.join(sentinelpress_root, ".venv", "Scripts", "python.exe")
    manage_py = os.path.join(sentinelpress_root, "manage.py")

    server_params = StdioServerParameters(
        command=python_exe,
        args=[manage_py, "stdio_server"],
        env=None
    )
    
    counts = {
        "hoarder": 0,
        "screener": 0,
        "standardizer": 0,
        "sectionizer": 0,
    }

    try:
        logger.info("Connecting to Sentinel Press MCP stdio_server...")
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # 1. Hoarder
                res = await session.call_tool("query_data_collections", {
                    "collection": "hoarderoutput",
                    "search_pipeline": [
                        {"$match": {"status": "ready"}},
                        {"$group": {"_id": None, "count": {"$count": "$status"}}}
                    ]
                })
                if res.content and res.content[0].text:
                    data = json.loads(res.content[0].text)
                    if data and isinstance(data, list):
                        counts["hoarder"] = data[0].get("count") or 0
                
                # 2. Screener
                res = await session.call_tool("query_data_collections", {
                    "collection": "screeneroutput",
                    "search_pipeline": [
                        {"$match": {"status": "ready"}},
                        {"$group": {"_id": None, "count": {"$count": "$status"}}}
                    ]
                })
                if res.content and res.content[0].text:
                    data = json.loads(res.content[0].text)
                    if data and isinstance(data, list):
                        counts["screener"] = data[0].get("count") or 0
                
                # 3. Standardizer
                res = await session.call_tool("query_data_collections", {
                    "collection": "standardizeroutput",
                    "search_pipeline": [
                        {"$match": {"status": "ready"}},
                        {"$group": {"_id": None, "count": {"$count": "$status"}}}
                    ]
                })
                if res.content and res.content[0].text:
                    data = json.loads(res.content[0].text)
                    if data and isinstance(data, list):
                        counts["standardizer"] = data[0].get("count") or 0

                # 4. Sectionizer
                res = await session.call_tool("query_data_collections", {
                    "collection": "sectionizeroutput",
                    "search_pipeline": [
                        {"$match": {"status": "ready", "newsletter_run": None}},
                        {"$group": {"_id": None, "count": {"$count": "$status"}}}
                    ]
                })
                if res.content and res.content[0].text:
                    data = json.loads(res.content[0].text)
                    if data and isinstance(data, list):
                        counts["sectionizer"] = data[0].get("count") or 0

    except Exception as e:
        logger.error(f"Error fetching pipeline status via MCP: {e}")
        
    return counts


class CuratorGreetingAgent(BaseAgent):
    """ADK-backed Gemini/Ollama agent that queries pipeline status via Django's MCP server and greets the user."""

    def __init__(self) -> None:
        sentinelpress_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "sentinelpress")
        )
        python_exe = os.path.join(sentinelpress_root, ".venv", "Scripts", "python.exe")
        manage_py = os.path.join(sentinelpress_root, "manage.py")

        mcp_server_params = StdioServerParameters(
            command=python_exe,
            args=[manage_py, "stdio_server"],
        )
        
        mcp_toolset = McpToolset(
            connection_params=mcp_server_params,
            tool_filter=["query_data_collections"],
        )

        greeter = LlmAgent(
            name="curator_greeting_llm",
            model=os.getenv("GREETER_GEMINI_MODEL", "gemini-3.5-flash"),
            description="Generates an encouraging, action-oriented greeting suggesting next steps.",
            static_instruction=(
                "You are an encouraging and helpful pipeline assistant for Sentinel Press. "
                "You must call the 'query_data_collections' tool for each of the collections below to check their counts where status = 'ready':\n"
                "1. Collection: 'hoarderoutput'\n"
                "2. Collection: 'screeneroutput'\n"
                "3. Collection: 'standardizeroutput'\n"
                "4. Collection: 'sectionizeroutput' (also match newsletter_run = null)\n\n"
                "CRITICAL PIPELINE STAGE RULES:\n"
                "- The count returned for a collection indicates items that have FINISHED that stage and are ready/pending for the NEXT stage.\n"
                "- 'hoarderoutput' (status='ready') means these items are completed in Hoarder and are ready/pending for the SCREENER stage.\n"
                "- 'screeneroutput' (status='ready') means these items are completed in Screener and are ready/pending for the STANDARDIZER stage.\n"
                "- 'standardizeroutput' (status='ready') means these items are completed in Standardizer and are ready/pending for the SECTIONIZER stage.\n"
                "- 'sectionizeroutput' (status='ready') means these items are completed in Sectionizer and are ready/pending for the NEWSLETTER GENERATOR stage.\n\n"
                "Review the counts, list them clearly, suggest next steps considering the pipeline rules above, and be innovative in suggesting actions the user can take (e.g. prompt tuning, reviewing history, starting a draft, or running ingest agents)."
            ),
            tools=[mcp_toolset],
            output_key="greeting_message",
            generate_content_config=types.GenerateContentConfig(
                temperature=0.7,
            ),
        )
        super().__init__(
            name="curator_greeting_agent",
            description="Greets the user and suggests next steps based on the pipeline status.",
            sub_agents=[greeter],
        )
        self._greeter = greeter

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        newsletter_slug = ctx.session.state.get("newsletter_slug")
        greeter_model = "gemini-3.5-flash"
        greeter_prompt = None

        if newsletter_slug:
            try:
                settings_payload = pipeline_client.load_newsletter_settings(newsletter_slug)
                settings = settings_payload.get("settings", {})
                greeter_model = settings.get("greeter_model") or greeter_model
                greeter_prompt = settings.get("greeter_prompt") or greeter_prompt
            except Exception as e:
                logger.error(f"Failed to load greeter newsletter settings: {e}")

        if "ollama" in greeter_model.lower():
            model_name = greeter_model.split("/", 1)[1] if "/" in greeter_model else greeter_model
            if model_name.lower() == "ollama":
                model_name = "gemma"

            prompt_instructions = greeter_prompt or self._greeter.static_instruction
            
            # Since Ollama runs directly via python library here (independent of ADK's LlmAgent),
            # we fetch the status using the helper and prompt Ollama
            status = await get_pipeline_status_via_mcp()
            prompt = f"{prompt_instructions}\n\nCurrent Pipeline status counts:\n{json.dumps(status, indent=2)}"
            
            import ollama
            logger.info(f"Calling Ollama model {model_name}...")
            response = ollama.chat(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "keep_alive": "30m",
                    "temperature": 0.7,
                },
            )
            
            response_text = ""
            if isinstance(response, dict):
                response_text = response.get("message", {}).get("content") or ""
            else:
                message = getattr(response, "message", None)
                response_text = getattr(message, "content", "") if message else str(response)

            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=response_text)]),
            )
        else:
            self._greeter.model = greeter_model
            if greeter_prompt:
                self._greeter.static_instruction = greeter_prompt

            # Instruct Gemini to execute the query tool and summarize
            ctx.user_content = types.Content(
                parts=[types.Part(text="Check the current pipeline counts using the query tool and write the welcome greeting.")]
            )

            async for event in self._greeter.run_async(ctx):
                yield event


curator_greeting_agent = CuratorGreetingAgent()
