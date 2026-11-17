
from utils import ESTIMATED_OUTPUT_TOKENS_PER_DOC, MIN_SOURCE_WORDS, ArticleIn, SectionizerOutput, build_body, build_schema, build_system_prompt, clean_text, load_categories
from google.adk import Context
from google.adk import Workflow
from google.adk.workflow import node
from typing import Any
import asyncio
import pipeline_client
from google.adk import Event
import json
from pathlib import Path
import sys
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from dotenv import load_dotenv
import logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)

logger = logging.getLogger(__name__)


# # Prompt templates (Jinja2) live next to this file. StrictUndefined = a typo'd variable raises
# # instead of silently rendering as empty text.
# _jinja = Environment(
#     loader=FileSystemLoader(str(Path(__file__).parent)),
#     undefined=StrictUndefined,
#     autoescape=False,        # plain-text prompts: article text must not be HTML-escaped
#     trim_blocks=True,
#     lstrip_blocks=True,
#     keep_trailing_newline=False,
# )
# SYSTEM_PROMPT_TEMPLATE = _jinja.get_template("system_prompt.md.j2")
# ARTICLE_PROMPT_TEMPLATE = _jinja.get_template("article_prompt.md.j2")

@node(name="estimatetokens_node")
async def estimatetokens_node(node_input: str):
    """Get the articles for sectionizing"""
    payload = json.loads(node_input)
    # with tracer.start_as_current_span("load_documents_for_sectionizing"):
    earlier_sectionizeroutput = pipeline_client.load_sectionizer_output(payload["output_id"])
    if not earlier_sectionizeroutput:
        yield Event(output="No earlier_output to sectionize", route="default")
        return
    sectionizerOutput = SectionizerOutput.model_validate(earlier_sectionizeroutput)
    # 
    # yield Event(output=doc)

    # Prepare prompt
    categories = pipeline_client.load_sectionizer_categories()

    cats = load_categories(categories)
    system_prompt = build_system_prompt(cats)   # rendered once, identical on every line
    schema = build_schema(cats)
    # identical on every line, so built once
    system_instruction = _as_json(types.Content(parts=[types.Part(text=system_prompt)]))
    generation_config = _as_json(types.GenerationConfig(
        temperature=0.4, response_mime_type="application/json", response_json_schema=schema))


    doc = pipeline_client.load_hoarder_output(sectionizerOutput.batch.key[4:])
    # doc_id = doc.get("standardized_doc_id") or doc.get("doc_id")
    article = ArticleIn.model_validate(doc["source_payload_json"])
    text = clean_text(article.text)
    words = len(text.split())

    if words < MIN_SOURCE_WORDS:
        # A headline-only item has nothing to rewrite, so it never reaches the model.
        yield Event(output=f"Only {words} word{'' if words == 1 else 's'} of text (minimum {MIN_SOURCE_WORDS})", route="default")
        return

    body = build_body(article)

    # Estimate for this line. The system prompt is billed on every request (before any cache
    # discount). Measured against two real result lines: within 3-5% of the actual prompt tokens.
    yield Event(output={
        "estimated_input_tokens": (len(system_prompt) + len(body)) // 4,
        "estimated_output_tokens": ESTIMATED_OUTPUT_TOKENS_PER_DOC,
    })
    # return '{"estimated_cost":"0.0001USD","estimated_INR":"0.0001 INR"}'

@node(name="do_nothing_node")
def do_nothing_node(node_input: Any):
    return

# define a dynamic workflow node
@node(rerun_on_resume=True)
async def front_node(ctx: Context, node_input: str) -> str:
    # run_node executes a node and returns its output
    # Set by the backend chat UI (ChatApp.jsx) via the /run_sse stateDelta.
    page_url = ctx.state.get("pageUrl", "")
    logger.info("front_node page_url=%s", page_url)

    try:
        payload = json.loads(node_input) or {}
    except json.JSONDecodeError:
        logger.warning("front_node received non-JSON input: %.200s", node_input)
        payload = {}

    if payload.get("function") == "estimatetokens":
        return await ctx.run_node(estimatetokens_node, node_input=json.dumps(payload.get("payload", {})))
    await ctx.run_node(do_nothing_node, node_input="hello")


# Run the workflow
root_agent = Workflow(
    name="root_agent",
    edges=[("START", front_node)],
)
