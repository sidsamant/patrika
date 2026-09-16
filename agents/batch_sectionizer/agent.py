from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pipeline_client
ENV_PATH = PROJECT_ROOT / ".env"
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt_template.md")

load_dotenv(ENV_PATH)

logger = logging.getLogger(__name__)

def _get_genai_client() -> genai.Client:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


def prepare_batch_jsonl(documents: list[dict[str, Any]], categories: list[dict[str, Any]], output_jsonl_path: str, model_name: str = "gemini-2.5-flash") -> tuple[int, dict[str, Any]]:
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

        request_row = {
            "custom_id": f"doc_{doc_id}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
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


def submit_batch_job(jsonl_file_path: str) -> str:
    """Uploads batch JSONL and submits Gemini Batch API job."""
    client = _get_genai_client()
    logger.info("Uploading batch JSONL file to Gemini API: %s", jsonl_file_path)
    file_ref = client.files.upload(
        file=jsonl_file_path,
        config=types.UploadFileConfig(mime_type="text/plain"),
    )
    
    logger.info("Creating Gemini Batch Job using model gemini-2.5-flash...")
    batch_job = client.batches.create(
        model="gemini-2.5-flash",
        src=file_ref.name,
    )
    logger.info("Batch job created successfully. Job Name: %s", batch_job.name)
    return batch_job.name


def check_and_process_batch_job(batch_job_id: str, run_timestamp: str, agent_run_id: int | None = None) -> str:
    """Checks Gemini Batch Job status. If complete, downloads results and inserts NEW SectionizerOutput rows."""
    client = _get_genai_client()
    batch_job = client.batches.get(name=batch_job_id)
    state = getattr(batch_job, "state", "UNKNOWN")
    logger.info("Batch Job %s state: %s", batch_job_id, state)

    if str(state).endswith("SUCCEEDED") or str(state) == "BATCH_JOB_STATE_SUCCEEDED":
        # Process results
        results = getattr(batch_job, "dest", None) or []
        created_count = 0
        total_prompt_tokens = 0
        total_candidates_tokens = 0

        for item in results:
            custom_id = getattr(item, "custom_id", "")
            doc_id = int(custom_id.replace("doc_", "")) if custom_id.startswith("doc_") else None
            if not doc_id:
                continue

            response_body = getattr(item, "response", {})
            if not isinstance(response_body, dict) and hasattr(response_body, "model_dump"):
                try:
                    response_body = response_body.model_dump()
                except Exception:
                    response_body = {}

            body_dict = response_body.get("body") if isinstance(response_body, dict) else {}
            if not body_dict:
                body_dict = response_body if isinstance(response_body, dict) else {}

            usage_raw = body_dict.get("usage") or body_dict.get("usageMetadata") or body_dict.get("usage_metadata") or {}
            prompt_tok = usage_raw.get("prompt_tokens") or usage_raw.get("promptTokenCount") or usage_raw.get("prompt_token_count") or 0
            cand_tok = usage_raw.get("completion_tokens") or usage_raw.get("candidatesTokenCount") or usage_raw.get("candidates_token_count") or 0
            total_prompt_tokens += int(prompt_tok)
            total_candidates_tokens += int(cand_tok)

            parsed_json = {}
            text_out = ""
            choices = body_dict.get("choices") or []
            if choices and isinstance(choices, list) and len(choices) > 0:
                msg = choices[0].get("message") or {}
                text_out = msg.get("content") or ""
            if not text_out:
                cands = body_dict.get("candidates") or []
                if cands and isinstance(cands, list) and len(cands) > 0:
                    text_out = cands[0].get("content", {}).get("parts", [{}])[0].get("text", "")

            try:
                if text_out:
                    parsed_json = json.loads(text_out)
            except Exception as e:
                logger.warning("Failed parsing output for doc_%s: %s", doc_id, e)

            # ALWAYS create a NEW SectionizerOutput row (append-only)
            pipeline_client.create_sectionizer_output(
                doc_id=doc_id,
                category_id=None,
                source_path="",
                llm_instruction="batch_sectionizer_job",
                llm_content=json.dumps(response_body),
                output_json=parsed_json,
                match_count=len(parsed_json.get("matches", [])),
                run_timestamp=run_timestamp,
                agent_run_id=agent_run_id,
            )
            created_count += 1

        # Calculate actual post-completion token counts & cost proof
        from workflows.pricing_matrix import calculate_cost
        cost_proof = calculate_cost(
            model_name="gemini-2.5-flash",
            input_tokens=total_prompt_tokens,
            output_tokens=total_candidates_tokens,
        )
        actual_batch_cost_usd = round(cost_proof["total_cost_usd"] * 0.5, 6)
        actual_batch_cost_inr = round(cost_proof["total_cost_inr"] * 0.5, 2)

        actual_usage_meta = {
            "model": "gemini-2.5-flash",
            "batch_job_id": batch_job_id,
            "status": "COMPLETED",
            "total_documents_processed": created_count,
            "actual_prompt_tokens": total_prompt_tokens,
            "actual_candidates_tokens": total_candidates_tokens,
            "actual_total_tokens": total_prompt_tokens + total_candidates_tokens,
            "standard_cost_usd": cost_proof["total_cost_usd"],
            "actual_batch_cost_usd": actual_batch_cost_usd,
            "actual_batch_cost_inr": actual_batch_cost_inr,
            "discount_applied": "50% Gemini Batch API discount",
        }

        logger.info(
            "Batch Job %s COMPLETED | Post-Completion Usage Meta: docs=%d, prompt_tokens=%d, candidates_tokens=%d, total_tokens=%d, actual_batch_cost=$%.6f (₹%.2f)",
            batch_job_id,
            created_count,
            total_prompt_tokens,
            total_candidates_tokens,
            total_prompt_tokens + total_candidates_tokens,
            actual_batch_cost_usd,
            actual_batch_cost_inr,
        )

        pipeline_client.update_batch_sectionizer_job_status(
            batch_job_name=batch_job_id,
            status="COMPLETED",
            usage_meta=actual_usage_meta,
        )

        return "COMPLETED"
    
    return str(state)


class BatchSectionizerAgent(BaseAgent):
    """ADK Agent that submits standardized documents for bulk batch sectionizing via Gemini Batch API."""

    def __init__(self) -> None:
        super().__init__(
            name="batch_sectionizer",
            description="Submits standardized documents for bulk batch sectionizing via Gemini Batch API.",
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        docs = pipeline_client.load_documents_for_sectionizing()
        cats = pipeline_client.load_sectionizer_categories()

        if not docs:
            payload = {"status": "complete", "message": "No documents found for batch sectionizing.", "totalRows": 0}
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload, indent=2))]),
            )
            return

        run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        jsonl_path = os.path.join(PROJECT_ROOT, ".output", f"batch_input_{run_ts}.jsonl")
        os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

        count, usage_meta = prepare_batch_jsonl(docs, cats, jsonl_path)
        batch_job_name = submit_batch_job(jsonl_path)
        doc_ids = [d.get("standardized_doc_id") or d.get("doc_id") for d in docs]
        
        pipeline_client.record_batch_sectionizer_job(batch_job_name=batch_job_name, doc_ids=doc_ids, status="PENDING")

        output_payload = {
            "batch_job_name": batch_job_name,
            "status": "PENDING",
            "usage_meta": usage_meta,
            "totalRows": len(docs),
            "submittedRequests": count,
            "jsonl_path": jsonl_path,
        }
        
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(output_payload, indent=2))]),
        )


batch_sectionizer_agent = BatchSectionizerAgent()
root_agent = batch_sectionizer_agent
