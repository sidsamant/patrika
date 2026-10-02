import asyncio
import json
from pathlib import Path
import sys
import time
import types
from typing import Any

from google.adk import Agent
from google.adk import Workflow
from google.adk import Event
from google.adk.workflow import JoinNode
from opik import track
from pydantic import BaseModel
from typing_extensions import TypedDict
from datetime import datetime
from dotenv import load_dotenv
import logging
from google import genai
from google.genai import types
from google.adk.agents.context import Context
from google.adk.events import RequestInput
import os
import pipeline_client

# import pipeline_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENV_PATH = PROJECT_ROOT / ".env"
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt_template.md")

load_dotenv(ENV_PATH)

logger = logging.getLogger(__name__)

# Ensure ADK uses AI Studio instead of forcing Vertex AI
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
print("GOOGLE_API_KEY_TYPE: "+os.environ["GOOGLE_API_KEY_TYPE"])
MODEL_TOUSE="gemini-3.1-flash-lite"


from google.adk.telemetry.setup import maybe_set_otel_providers
from google.adk.agents.run_config import RunConfig
from google.adk.telemetry import TelemetryConfig
from opentelemetry import trace

maybe_set_otel_providers() # Explicitly tells ADK to start exporting to your OTel variables
print("PROVIDER:", type(trace.get_tracer_provider()))   # must NOT be ProxyTracerProvider

# Jaeger debug code from claude. 
# provider = trace.get_tracer_provider()

# # 1. What exporters did ADK actually attach?
# try:
#     for sp in provider._active_span_processor._span_processors:
#         exp = getattr(sp, "span_exporter", None)
#         print("PROCESSOR:", type(sp).__name__, "| EXPORTER:", type(exp).__module__, type(exp).__name__,
#               "| ENDPOINT:", getattr(exp, "_endpoint", None))
# except Exception as e:
#     print("inspect failed:", e)

# # 2. Manual span, bypassing ADK entirely
# with trace.get_tracer("manual-check").start_as_current_span("manual-check-span"):
#     pass
# print("FLUSH OK:", provider.force_flush(timeout_millis=5000))

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

def attach_jaeger_exporter():
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        print("No SDK provider registered; cannot attach exporter")
        return
    # adk web may import this module more than once; avoid duplicate exporters
    if getattr(provider, "_jaeger_attached", False):
        return
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces")
        )
    )
    provider._jaeger_attached = True
    print("JAEGER EXPORTER ATTACHED | resource:", dict(provider.resource.attributes))
    print("ENV SERVICE NAME:", os.environ.get("OTEL_SERVICE_NAME"))

attach_jaeger_exporter()

from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

if not getattr(trace.get_tracer_provider(), "_httpx_instrumented", False):
    HTTPXClientInstrumentor().instrument()
    try:
        trace.get_tracer_provider()._httpx_instrumented = True
    except Exception:
        pass

# from google.adk.agents.app import AdkApp

from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor
GoogleGenAiSdkInstrumentor().instrument()

# Pass the telemetry configuration flag directly to your run config
# run_config = RunConfig(
#     telemetry=TelemetryConfig(adk_experimental_telemetry_opt_in=True)
# )

# print("Workflow complete! Flushing traces...")
# Force OpenTelemetry to push remaining spans out to Jaeger
# trace.get_tracer_provider().force_flush()
# time.sleep(2) # Give the network socket two seconds to clear
# Pass the tracing flag to the primary application block
# app = AdkApp(enable_tracing=True)


# Constant for batch LLM model - cheapest model for Gemini Batch API
BATCH_MODEL_NAME = os.getenv("BATCH_SECTIONIZER_MODEL", "gemini-1.5-flash")

# def router(node_input: str):
#     """Route to task B or C based on node_input."""
#     if condition(node_input):
#         return Event(route="RUN_TASK_C")
#     return Event(route="RUN_TASK_B")

# 1. Define the structure of your dynamic dictionary
class NodeInput(TypedDict, total=False):
    load_documents_for_sectionizing: list[dict[str, Any]]
    load_categories_for_categories: list[dict[str, Any]]


tracer = trace.get_tracer("bulk_newsgenerator")

async def load_documents_for_sectionizing():
    """Get the articles for sectionizing"""
    with tracer.start_as_current_span("load_documents_for_sectionizing"):
        docs = (await asyncio.to_thread(pipeline_client.load_documents_for_sectionizing))[:2]
    if not docs:
        yield Event(output="No documents to sectionize", route="default")
        return
    yield Event(output=docs, route="route_docs_found", state={"docs_len": len(docs)})

async def load_categories_for_categories():
    """Get the categories for sectionizing"""
    # docs = ["some1thing","sie1"]
    docs = pipeline_client.load_sectionizer_categories()
    yield Event(output=docs, route="route_cats_found")
    # return pipeline_client.load_sectionizer_categories()

def prepare_batch_jsonl(
    documents: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    output_jsonl_path: str,
    model_name: str = BATCH_MODEL_NAME,
) -> tuple[int, dict[str, Any]]:
    """Formats a list of documents into JSONL format for Gemini Batch API and calculates estimated token counts & costs."""
    from workflows.pricing_matrix import calculate_cost

    cats_block = "\n".join([f"- [{cat.get('id')}] {cat.get('name')}: {cat.get('description', '')}" for cat in categories])
    
    with open(PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as tf:
        template = tf.read()

    lines = []
    total_prompt_chars = 0
    for doc in documents:
        doc_id = doc.get("standardized_doc_id") or doc.get("doc_id")
        content_text = doc.get("text_content") or ""
        source_path = doc.get("source_path") or ""
        
        prompt = template.replace("{{source_path}}", source_path)
        prompt = prompt.replace("{{title}}", str(doc.get("title") or ""))
        prompt = prompt.replace("{{author}}", str(doc.get("author") or ""))
        prompt = prompt.replace("{{text_content}}", content_text[:4000])
        prompt = prompt.replace("{{categories_block}}", cats_block)

        total_prompt_chars += len(prompt)

        # Standard Gemini Batch API request format (key + request)
        request_row = {
            "key": f"doc_{doc_id}",
            "request": {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json"
                },
            },
        }
        lines.append(json.dumps(request_row))

    with open(output_jsonl_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    estimated_input_tokens = max(1, total_prompt_chars // 4)
    estimated_output_tokens = len(documents) * 1000

    cost_proof = calculate_cost(
        model_name=model_name,
        input_tokens=estimated_input_tokens,
        output_tokens=estimated_output_tokens,
    )

    batch_cost_usd = round(cost_proof["total_cost_usd"] * 0.5, 6)
    batch_cost_inr = round(cost_proof["total_cost_inr"] * 0.5, 2)

    usage_meta = {
        "model": model_name,
        "total_documents": len(documents),
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_total_tokens": estimated_input_tokens + estimated_output_tokens,
        "standard_cost_usd": cost_proof["total_cost_usd"],
        "batch_cost_usd": batch_cost_usd,
        "batch_cost_inr": batch_cost_inr,
        "discount_applied": "50% Gemini Batch API discount",
    }

    return len(lines), usage_meta

# @track
async def prepare_jsonl_node(node_input: NodeInput):
    """Prepare the jsonl document for submission for batch API"""

    docs = node_input["load_documents_for_sectionizing"];
    cats = node_input["load_categories_for_categories"];

    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = os.path.join(PROJECT_ROOT, ".output", f"batch_input_{run_ts}.jsonl")
    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

    yield Event(state={
        "jsonl_path": jsonl_path,
        "prepare_batch_jsonl":prepare_batch_jsonl(docs, cats, jsonl_path)
    })

def _get_genai_client() -> genai.Client:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


def submit_batch_job(jsonl_file_path: str, model_name: str = BATCH_MODEL_NAME) -> types.BatchJob:
    """Uploads batch JSONL and submits Gemini Batch API job, returning the SDK types.BatchJob object."""
    client = _get_genai_client()
    logger.info("Uploading batch JSONL file to Gemini API: %s", jsonl_file_path)
    file_ref = client.files.upload(
        file=jsonl_file_path,
        config=types.UploadFileConfig(mime_type="text/plain"),
    )
    
    logger.info("Creating Gemini Batch Job using model %s...", model_name)
    batch_job: types.BatchJob = client.batches.create(
        model=model_name,
        src=file_ref.name,
    )
    logger.info("Batch job created successfully. Job Name: %s", batch_job.name)
    return batch_job

async def submit_batch_job_node(ctx: Context):
    print(f"attempts state: {ctx.state}") # attempts state: attempts: 1
    jsonl_path=ctx.state.get("jsonl_path")
    batch_job: types.BatchJob = submit_batch_job(jsonl_path)
    
    yield Event(output=batch_job)


async def record_batch_job_node(ctx: Context, batch_job: types.BatchJob ):
    count, usage_meta = ctx.state.prepare_batch_jsonl
    
    batch_job_name = batch_job.name
    try:
        usage_meta["create_batch_response"] = json.loads(batch_job.model_dump_json())
    except Exception as e:
        logger.warning("Could not dump BatchJob response: %s", e)

    doc_ids = [d.get("standardized_doc_id") or d.get("doc_id") for d in docs]
    
    pipeline_client.record_batch_sectionizer_job(
        batch_job_name=batch_job_name,
        doc_ids=doc_ids,
        status="PENDING",
        usage_meta=usage_meta,
    )
    yield batch_job

async def request_approval_node(): # Human input step
  yield RequestInput(message="Should we proceed (yes/no)?:")

async def request_approval_response_node(node_input: str): # Human input step
    if node_input=="yes":
        yield Event(route="route_proceed")
    else:
        yield Event(route="route_do_not_proceed")

async def end_node(ctx: Context,batch_job: types.BatchJob):
    print(f"Context: {ctx}")
    count, usage_meta = ctx.state.prepare_batch_jsonl
    
    batch_job_name = batch_job.name
    jsonl_path=ctx.state.jsonl_path
    docs_len=ctx.state.docs_len

    output_payload = {
        "batch_job_name": batch_job_name,
        "status": "PENDING",
        "usage_meta": usage_meta,
        "totalRows": docs_len,
        "submittedRequests": count,
        "jsonl_path": jsonl_path,
    }

    yield Event(
        #author=self.name,
        invocation_id=ctx.invocation_id,
        content=types.Content(role="model", parts=[types.Part(text=json.dumps(output_payload, indent=2))]),
    )


# city_report_agent = Agent(
#     name="city_report_agent",
#     model=MODEL_TOUSE,
#     input_schema=CityTime,
#     instruction="""Output following line:
#     It is {CityTime.time_info} in {CityTime.city} right now.""",
# )

def completed_message_function(node_input: str):
    return Event(
        message=f"{node_input}\n WORKFLOW COMPLETED.",
    )



def getall(node_input: NodeInput):
    # Use .get() because the keys are optional and might not be in the dict
    docs = node_input.get("load_documents_for_sectionizing")
    if docs:
        print(docs)
        
    cats = node_input.get("load_categories_for_categories")
    if cats:
        print(cats)
   
    return Event(
        message=f"GET ALL COMPLETED.",
    )

my_join_node = JoinNode(name="my_join_node")

root_agent = Workflow(
    name="bulk_newsgenerator",
    edges=[
        # ("START",load_documents_for_sectionizing)
        ("START",load_documents_for_sectionizing,my_join_node),
        ("START",load_categories_for_categories,my_join_node),
        (
            my_join_node, prepare_jsonl_node, request_approval_node, request_approval_response_node
        ),
        (
            request_approval_response_node,
            {
                "route_proceed": submit_batch_job_node,
                "route_do_not_proceed": end_node
            }
        ),
        (
            submit_batch_job_node, record_batch_job_node, end_node
        )
        
        
        
        # (load_documents_for_sectionizing,
        #     {
        #         "route_docs_found": city_report_agent,
        #         "default": completed_message_function
        #     },
        # ),
        # (city_report_agent, completed_message_function)
    ],
)

# batch_sectionizer_agent = BatchSectionizerAgent()
# root_agent = batch_sectionizer_agent