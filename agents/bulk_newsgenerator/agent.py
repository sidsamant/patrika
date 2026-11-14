import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Literal

from utils import ESTIMATED_OUTPUT_TOKENS_PER_DOC, MIN_SOURCE_WORDS, SKIP_REASON_THIN, ArticleIn, Category, NodeInput, build_body, build_schema, build_system_prompt, clean_text, load_categories
from google.adk import Agent
from google.adk import Workflow
from google.adk import Event
from google.adk.workflow import JoinNode
from opik import track
from cleantext import clean
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
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
BATCH_MODEL_NAME = os.getenv("BATCH_SECTIONIZER_MODEL", "gemini-3.8-flash")

# def router(node_input: str):
#     """Route to task B or C based on node_input."""
#     if condition(node_input):
#         return Event(route="RUN_TASK_C")
#     return Event(route="RUN_TASK_B")

tracer = trace.get_tracer("bulk_newsgenerator")


# TEST LIMIT: at most this many documents are submitted to Gemini per run (0 = no limit).
# It counts documents that really go to the model: thin documents are skipped first and do not
# use up the limit. Documents beyond the limit are left untouched ('ready') for a later run.
# Set BATCH_MAX_DOCS=0 in .env to turn it off. The ADK server must be restarted to pick it up.
BATCH_MAX_DOCS = int(os.getenv("BATCH_MAX_DOCS", "2") or 0)
# BATCH_MAX_DOCS = 1

# MANUAL TESTING WITHOUT COST: set GEMINI_EMULATOR_URL (for example http://127.0.0.1:8099) and run
# gemini_batch_emulator.py. The real SDK then talks to the emulator: no real LLM call, nothing billed.
# Unset it (and restart the ADK server) to use the real Gemini API again.
EMULATOR_URL = os.getenv("GEMINI_EMULATOR_URL", "").rstrip("/")


def _as_json(model) -> dict[str, Any]:
    """An SDK type in the camelCase JSON form the batch file expects."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


SAFETY_SETTINGS = [
    _as_json(types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE))
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]



async def load_documents_for_sectionizing():
    """Get the articles for sectionizing"""
    with tracer.start_as_current_span("load_documents_for_sectionizing"):
        docs = await asyncio.to_thread(pipeline_client.load_documents_for_sectionizing)
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
    max_docs: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """Formats documents into JSONL for Gemini Batch API and calculates estimated token counts & costs.

    Thin-source documents (fewer than MIN_SOURCE_WORDS words) are NOT written to the JSONL.
    They are listed in usage_meta["skipped_documents"] with the reason, so the caller can
    show them to the user and record them against each document id.
    """
    from workflows.pricing_matrix import calculate_cost

    cats = load_categories(categories)
    system_prompt = build_system_prompt(cats)   # rendered once, identical on every line
    schema = build_schema(cats)

    # identical on every line, so built once
    system_instruction = _as_json(types.Content(parts=[types.Part(text=system_prompt)]))
    generation_config = _as_json(types.GenerationConfig(
        temperature=0.4, response_mime_type="application/json", response_json_schema=schema))

    lines: list[str] = []
    skipped: list[dict[str, Any]] = []
    sample: list[dict[str, Any]] = []
    line_estimates: dict[str, dict[str, Any]] = {}     # per document, saved in usage_meta["lines"]
    deferred = 0                                       # documents left for a later run by max_docs
    for doc in documents:
        if max_docs and len(lines) >= max_docs:
            deferred += 1          # not examined, not skipped, not recorded: stays 'ready'
            continue
        doc_id = doc.get("standardized_doc_id") or doc.get("doc_id")
        article = ArticleIn.model_validate(doc)
        text = clean_text(article.text)
        words = len(text.split())

        if words < MIN_SOURCE_WORDS:
            # A headline-only item has nothing to rewrite, so it never reaches the model.
            skipped.append({
                "doc_id": doc_id,
                "reason": SKIP_REASON_THIN,
                "detail": f"Only {words} word{'' if words == 1 else 's'} of text (minimum {MIN_SOURCE_WORDS})",
                "word_count": words,
                "content_sha256": doc.get("content_sha256"),
            })
            continue

        body = build_body(article)
        if len(sample) < 5:
            sample.append({"doc_id": doc_id, "preview": " ".join(text.split())[:100]})

        # Estimate for this line. The system prompt is billed on every request (before any cache
        # discount). Measured against two real result lines: within 3-5% of the actual prompt tokens.
        line_estimates[str(doc_id)] = {
            "key": f"doc_{doc_id}",
            "estimated_input_tokens": (len(system_prompt) + len(body)) // 4,
            "estimated_output_tokens": ESTIMATED_OUTPUT_TOKENS_PER_DOC,
        }

        # Standard Gemini Batch API request format (key + request). No "tools" key -> no web search.
        request_row = {
            "key": f"doc_{doc_id}",
            "request": {
                "systemInstruction": system_instruction,
                "contents": [_as_json(types.Content(role="user", parts=[types.Part(text=body)]))],
                "safetySettings": SAFETY_SETTINGS,
                "generationConfig": generation_config,
            },
        }
        lines.append(json.dumps(request_row, ensure_ascii=False))

    with open(output_jsonl_path, "w", encoding="utf-8", newline="\n") as f:  # \n even on Windows
        f.write("\n".join(lines) + ("\n" if lines else ""))

    if skipped:
        logger.warning("Skipping %d docs with < %d words of text: %s",
                       len(skipped), MIN_SOURCE_WORDS, [d["doc_id"] for d in skipped[:10]])

    estimated_input_tokens = max(1, sum(e["estimated_input_tokens"] for e in line_estimates.values()))
    estimated_output_tokens = sum(e["estimated_output_tokens"] for e in line_estimates.values())

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
        "submitted_documents": len(lines),
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_total_tokens": estimated_input_tokens + estimated_output_tokens,
        "standard_cost_usd": cost_proof["total_cost_usd"],
        "batch_cost_usd": batch_cost_usd,
        "batch_cost_inr": batch_cost_inr,
        "discount_applied": "50% Gemini Batch API discount",
        "skipped_documents": skipped,
        "max_docs": max_docs or None,
        # lets the UI send this job's status checks to the emulator, and mark its results as emulated
        "emulated": bool(EMULATOR_URL),
        "emulator_url": EMULATOR_URL or None,
        "deferred_documents": deferred,
        "sample_articles": sample,
        # per-line estimates; the batch detail page adds the actual counts when Gemini finishes
        "lines": line_estimates,
        # snapshot, so results can still be mapped if categories change before the batch finishes
        "categories_snapshot": [{"id": c.id, "name": c.name} for c in cats],
    }

    return len(lines), usage_meta


MAX_SKIPPED_ROWS_SHOWN = 10


def format_approval_summary(count: int, meta: dict[str, Any]) -> str:
    """Markdown shown to the user at the approval step."""
    skipped = meta.get("skipped_documents") or []
    cats = meta.get("categories_snapshot") or []
    out = [
        "## Batch ready for review",
        "",
    ] + (["> **EMULATOR MODE**: no real LLM call will be made and nothing is billed. "
          "The cost below is what a real run would cost.", ""] if meta.get("emulated") else []) + [
        f"**{count} article(s)** will be sent to `{meta['model']}` "
        f"({meta['total_documents']} loaded, {len(skipped)} skipped"
        + (f", {meta['deferred_documents']} held back by the test limit" if meta.get("deferred_documents") else "")
        + ").",
        "",
        "| | |",
        "|---|---|",
        f"| Articles to submit | {count} |",
        f"| Skipped (not sent) | {len(skipped)} |",
    ] + ([f"| Held back by test limit (BATCH_MAX_DOCS={meta['max_docs']}) | {meta['deferred_documents']} |"]
         if meta.get("max_docs") else []) + [
        f"| Categories | {len(cats)} |",
        f"| Est. input tokens | {meta['estimated_input_tokens']:,} |",
        f"| Est. output tokens | {meta['estimated_output_tokens']:,} |",
        f"| **Est. batch cost** | **${meta['batch_cost_usd']:,.4f} (₹{meta['batch_cost_inr']:,.2f})** |",
        f"| Standard price, for reference | ${meta['standard_cost_usd']:,.4f} |",
        "",
        "_Estimates only: output is assumed at about 1,000 tokens per article._",
    ]
    if cats:
        out += ["", "**Categories:** " + ", ".join(f"{c['name']} ({c['id']})" for c in cats)]
    if meta.get("sample_articles"):
        out += ["", "**First articles in this batch**"]
        out += [f"- #{a['doc_id']}: {a['preview']}…" for a in meta["sample_articles"]]
    if skipped:
        out += [
            "",
            f"### Skipped: {len(skipped)} article(s) not sent to the model",
            "| Doc ID | Words | Reason |",
            "|---|---|---|",
        ]
        out += [f"| {d['doc_id']} | {d['word_count']} | {d['reason']}: {d['detail']} |"
                for d in skipped[:MAX_SKIPPED_ROWS_SHOWN]]
        if len(skipped) > MAX_SKIPPED_ROWS_SHOWN:
            out.append(f"| … | | and {len(skipped) - MAX_SKIPPED_ROWS_SHOWN} more |")
        out += ["", "_If you approve, these are recorded as skipped (with the reason and the batch "
                    "name) against each document id once the batch is submitted. If you reply "
                    "no, nothing is recorded._"]
    out += ["", "**Submit this batch to Gemini? Reply `yes` or `no`.**"]
    return "\n".join(out)


# @track
async def _record_skipped_documents(
    skipped: list[dict[str, Any]], batch_job_name: str | None = None
) -> str | None:
    """Record why documents were skipped, against each document id.

    Only called after the user approved. Never raises: returns an error message, or None.
    """
    if not skipped:
        return None
    items = [{**d, "batch_job_name": batch_job_name} for d in skipped]
    try:
        await asyncio.to_thread(pipeline_client.record_skipped_documents, items)
    except Exception as e:
        logger.exception("Could not record skipped documents")
        return str(e)
    return None


async def prepare_jsonl_node(node_input: NodeInput):
    """Prepare the jsonl document for submission for batch API"""

    docs = node_input["load_documents_for_sectionizing"];
    cats = node_input["load_categories_for_categories"];

    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = os.path.join(PROJECT_ROOT, ".output", f"batch_input_{run_ts}.jsonl")
    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

    count, usage_meta = prepare_batch_jsonl(docs, cats, jsonl_path, max_docs=BATCH_MAX_DOCS or None)

    # Skipped docs are left out of the JSONL here, but nothing is saved yet: the skip reasons
    # are recorded only after the user approves (see record_batch_job_node).
    submitted = set(usage_meta["lines"])        # keys are str(doc_id): exactly the documents in the JSONL
    all_ids = [d.get("standardized_doc_id") or d.get("doc_id") for d in docs]
    yield Event(
        route="route_has_requests" if count else "route_nothing_to_submit",
        state={
            "jsonl_path": jsonl_path,
            "doc_ids": [i for i in all_ids if str(i) in submitted],  # submitted docs only
            "prepare_batch_jsonl": (count, usage_meta),
        },
    )

def _get_genai_client() -> genai.Client:
    if EMULATOR_URL:
        # a dummy key: the real key must never be sent to a local server
        logger.warning("GEMINI_EMULATOR_URL is set: using the Gemini emulator at %s. "
                       "NO real LLM call is made and nothing is billed.", EMULATOR_URL)
        return genai.Client(api_key="emulator", http_options=types.HttpOptions(base_url=EMULATOR_URL))
    for env_name in ("GOOGLE_API_KEY",): #GOOGLE_API_KEY_PAID
        api_key = os.getenv(env_name)
        if api_key:
            logger.info("Gemini client uses %s (ends ...%s)", env_name, api_key[-4:])
            return genai.Client(api_key=api_key)
    logger.info("Gemini client uses the SDK's default env lookup (GOOGLE_API_KEY / GEMINI_API_KEY)")
    return genai.Client()


def submit_batch_job(jsonl_file_path: str, model_name: str = BATCH_MODEL_NAME) -> types.BatchJob:
    """Uploads batch JSONL and submits Gemini Batch API job, returning the SDK types.BatchJob object."""
    client = _get_genai_client()
    logger.info("Uploading batch JSONL file to Gemini API: %s", jsonl_file_path)
    file_ref = client.files.upload(
        file=jsonl_file_path,
        config=types.UploadFileConfig(mime_type="jsonl"),
    )
    
    logger.info("Creating Gemini Batch Job using model %s...", model_name)
    batch_job: types.BatchJob = client.batches.create(
        model=model_name,
        src=file_ref.name,
    )
    logger.info("Batch job created successfully. Job Name: %s", batch_job.name)
    return batch_job

# Where the batch job detail page lives, for the link in the final chat message.
# The path is the "sectionizer:batch_job_detail" route; adjust it if the app is mounted elsewhere.
UI_BASE_URL = os.getenv("SENTINEL_UI_URL", "http://localhost:8000").rstrip("/")
BATCH_JOB_PATH = os.getenv("SENTINEL_BATCH_JOB_PATH", "/sectionizer/batches/{job_id}/")


def batch_job_url(job_id: int | None) -> str | None:
    return f"{UI_BASE_URL}{BATCH_JOB_PATH.format(job_id=job_id)}" if job_id else None


# ── Passing values between nodes ──────────────────────────────────────────────
# Values travel through ctx.state. A node declares what it needs as function parameters:
# ADK looks every parameter except ctx / node_input up in state BY NAME and converts it to the
# annotated type. node_input is kept only for real inputs: the joined load results
# (prepare_jsonl_node) and the user's yes/no reply (request_approval_response_node).
#
# State keys:  jsonl_path, docs_len, doc_ids, prepare_batch_jsonl = (count, usage_meta),
#              batch_job (JSON form of the Gemini BatchJob), batch_job_id (our database id),
#              skip_recording_error

async def submit_batch_job_node(ctx: Context, jsonl_path: str):
    print(f"attempts state: {ctx.state}") # attempts state: attempts: 1
    batch_job: types.BatchJob = submit_batch_job(jsonl_path)

    # JSON-safe, so it also survives a database-backed session service.
    yield Event(state={"batch_job": batch_job.model_dump(mode="json")})


async def record_batch_job_node(
    batch_job: types.BatchJob,
    prepare_batch_jsonl: tuple[int, dict[str, Any]],
    doc_ids: list[Any],
):
    count, usage_meta = prepare_batch_jsonl
    batch_job_name = batch_job.name
    try:
        usage_meta["create_batch_response"] = json.loads(batch_job.model_dump_json())
    except Exception as e:
        logger.warning("Could not dump BatchJob response: %s", e)

    recorded = pipeline_client.record_batch_sectionizer_job(
        batch_job_name=batch_job_name,
        doc_ids=doc_ids,
        status="PENDING",
        usage_meta=usage_meta,
    )
    batch_job_id = (recorded or {}).get("batch_job_id")    # needed for the link to the detail page

    # The user approved: now record why the thin docs were skipped, against each document id.
    skip_error = await _record_skipped_documents(
        usage_meta.get("skipped_documents", []), batch_job_name=batch_job_name)

    yield Event(state={
        "skip_recording_error": skip_error,
        "batch_job_id": batch_job_id,
        # written back explicitly: the node worked on a copy, and end_node shows create_batch_response
        "prepare_batch_jsonl": (count, usage_meta),
    })

async def request_approval_node(prepare_batch_jsonl: tuple[int, dict[str, Any]]): # Human input step
    count, usage_meta = prepare_batch_jsonl
    yield RequestInput(message=format_approval_summary(count, usage_meta))

async def request_approval_response_node(node_input: str): # Human input step
    print(node_input)
    if node_input.strip().lower() in ("yes", "y","approved"):
        yield Event(route="route_proceed")
    else:
        yield Event(route="route_do_not_proceed")

async def end_node(
    ctx: Context,
    batch_job: types.BatchJob,
    prepare_batch_jsonl: tuple[int, dict[str, Any]],
    jsonl_path: str,
    docs_len: int,
    skip_recording_error: str | None = None,
    batch_job_id: int | None = None,
):
    print(f"Context: {ctx}")
    count, usage_meta = prepare_batch_jsonl

    output_payload = {
        "batch_job_name": batch_job.name,
        "status": "PENDING",
        "usage_meta": usage_meta,
        "totalRows": docs_len,
        "submittedRequests": count,
        "skippedDocuments": len(usage_meta.get("skipped_documents", [])),
        "jsonl_path": jsonl_path,
    }

    if skip_recording_error:
        output_payload["skipRecordingError"] = skip_recording_error

    url = batch_job_url(batch_job_id)
    output_payload["batch_job_id"] = batch_job_id
    output_payload["batch_job_url"] = url
    message = json.dumps(output_payload, indent=2)
    if url:   # the link to the batch job detail page, shown in the chat message
        message = f"Batch job submitted: [Open batch job #{batch_job_id}]({url})\n\n```json\n{message}\n```"

    yield Event(
        #author=self.name,
        invocation_id=ctx.invocation_id,
        content=types.Content(role="model", parts=[types.Part(text=message)]),
    )

async def end_node_nothing(prepare_batch_jsonl: tuple[int, dict[str, Any]]):
    # Nothing is sent to the model, so there is no approval step and no cost to approve.
    _, usage_meta = prepare_batch_jsonl
    skipped = usage_meta["skipped_documents"]
    err = await _record_skipped_documents(skipped)
    msg = f"Nothing to submit: all {len(skipped)} loaded document(s) were skipped. "
    msg += (f"Could not record the skip reasons: {err}" if err
            else "The skip reason is recorded against each document id.")
    yield Event(output=msg)

async def end_node_rejected(ctx: Context):
    print(f"Context: {ctx}")
    return Event(output="Not proceeding")

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
        (my_join_node, prepare_jsonl_node),
        (
            prepare_jsonl_node,
            {
                "route_has_requests": request_approval_node,
                "route_nothing_to_submit": end_node_nothing,
            },
        ),
        (request_approval_node, request_approval_response_node),
        (
            request_approval_response_node,
            {
                "route_proceed": submit_batch_job_node,
                "route_do_not_proceed": end_node_rejected
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