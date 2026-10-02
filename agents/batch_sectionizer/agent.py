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

# Constant for batch LLM model - cheapest model for Gemini Batch API
BATCH_MODEL_NAME = os.getenv("BATCH_SECTIONIZER_MODEL", "gemini-1.5-flash")


def _get_genai_client() -> genai.Client:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


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


def check_and_process_batch_job(batch_job_id: str, run_timestamp: str, agent_run_id: int | None = None, model_name: str = BATCH_MODEL_NAME) -> str:
    """Checks Gemini Batch Job status. If complete, downloads results and inserts NEW SectionizerOutput rows."""
    client = _get_genai_client()
    batch_job = client.batches.get(name=batch_job_id)
    state = getattr(batch_job, "state", "UNKNOWN")
    logger.info("Batch Job %s state: %s", batch_job_id, state)

    state_str = str(state)
    if state_str.endswith("SUCCEEDED") or state_str == "BATCH_JOB_STATE_SUCCEEDED" or state_str == "JOB_STATE_SUCCEEDED":
        dest = getattr(batch_job, "dest", None)
        output_file_name = None
        if isinstance(dest, str):
            output_file_name = dest
        elif hasattr(dest, "file_name"):
            output_file_name = getattr(dest, "file_name")
        elif isinstance(dest, dict) and "file_name" in dest:
            output_file_name = dest["file_name"]

        result_lines: list[Any] = []
        if output_file_name:
            try:
                content_bytes = client.files.download(file=output_file_name)
                content_text = content_bytes.decode("utf-8") if isinstance(content_bytes, bytes) else str(content_bytes)
                result_lines = [line.strip() for line in content_text.splitlines() if line.strip()]
            except Exception as e:
                logger.error("Failed to download batch output file %s: %s", output_file_name, e)

        if not result_lines and isinstance(dest, (list, tuple)):
            result_lines = list(dest)

        created_count = 0
        total_prompt_tokens = 0
        total_candidates_tokens = 0

        for item in result_lines:
            if isinstance(item, str):
                try:
                    item_dict = json.loads(item)
                except Exception:
                    continue
            elif isinstance(item, dict):
                item_dict = item
            elif hasattr(item, "model_dump"):
                try:
                    item_dict = item.model_dump()
                except Exception:
                    continue
            else:
                continue

            custom_id = item_dict.get("key") or item_dict.get("custom_id") or ""
            doc_id = int(custom_id.replace("doc_", "")) if custom_id.startswith("doc_") else None
            if not doc_id:
                continue

            if item_dict.get("error"):
                logger.warning("Error in batch result for doc_%s: %s", doc_id, item_dict.get("error"))
                continue

            response_obj = item_dict.get("response") or item_dict.get("body") or item_dict

            usage_raw = response_obj.get("usageMetadata") or response_obj.get("usage_metadata") or response_obj.get("usage") or {}
            prompt_tok = (
                usage_raw.get("promptTokenCount")
                or usage_raw.get("prompt_token_count")
                or usage_raw.get("prompt_tokens")
                or 0
            )
            cand_tok = (
                usage_raw.get("candidatesTokenCount")
                or usage_raw.get("candidates_token_count")
                or usage_raw.get("completion_tokens")
                or 0
            )
            total_prompt_tokens += int(prompt_tok)
            total_candidates_tokens += int(cand_tok)

            parsed_json = {}
            text_out = ""
            candidates = response_obj.get("candidates") or []
            if candidates and isinstance(candidates, list) and len(candidates) > 0:
                cand0 = candidates[0]
                content = cand0.get("content") or {}
                parts = content.get("parts") or []
                if parts and isinstance(parts, list) and len(parts) > 0:
                    text_out = parts[0].get("text", "")

            if not text_out:
                choices = response_obj.get("choices") or []
                if choices and isinstance(choices, list) and len(choices) > 0:
                    text_out = choices[0].get("message", {}).get("content", "")

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
                llm_content=json.dumps(item_dict),
                output_json=parsed_json,
                match_count=len(parsed_json.get("matches", [])),
                run_timestamp=run_timestamp,
                agent_run_id=agent_run_id,
            )
            created_count += 1

        # Calculate actual post-completion token counts & cost proof
        from workflows.pricing_matrix import calculate_cost
        cost_proof = calculate_cost(
            model_name=model_name,
            input_tokens=total_prompt_tokens,
            output_tokens=total_candidates_tokens,
        )
        actual_batch_cost_usd = round(cost_proof["total_cost_usd"] * 0.5, 6)
        actual_batch_cost_inr = round(cost_proof["total_cost_inr"] * 0.5, 2)

        actual_usage_meta = {
            "model": model_name,
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
        try:
            actual_usage_meta["get_batch_response"] = json.loads(batch_job.model_dump_json())
        except Exception:
            pass

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
        docs = pipeline_client.load_documents_for_sectionizing()[:2]
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
        batch_job: types.BatchJob = submit_batch_job(jsonl_path)
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


# batch_sectionizer_agent = BatchSectionizerAgent()
# root_agent = batch_sectionizer_agent
