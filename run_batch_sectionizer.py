from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / ".logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("run_batch_sectionizer")

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import pipeline_client
from agents.batch_sectionizer.agent import prepare_batch_jsonl, submit_batch_job, check_and_process_batch_job


def main():
    logger.info("Starting Batch Sectionizer Runner...")
    
    agent_run = pipeline_client.create_agent_run(agent_name="batch_sectionizer")
    agent_run_id = agent_run.get("agent_run_id")

    docs = pipeline_client.load_documents_for_sectionizing()
    cats = pipeline_client.load_sectionizer_categories()
    
    if not docs:
        logger.info("No documents found for batch sectionizing.")
        pipeline_client.finish_agent_run(agent_run_id, status="complete", items_in=0, items_out=0)
        return

    logger.info("Found %d documents for batch sectionizing.", len(docs))
    jsonl_path = os.path.join(PROJECT_ROOT, ".output", f"batch_input_{RUN_TIMESTAMP}.jsonl")
    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

    count, usage_meta = prepare_batch_jsonl(docs, cats, jsonl_path)
    logger.info(
        "Prepared %d requests in %s | Token & Cost Estimate: docs=%d, input_tokens=%d, est_output_tokens=%d, total_tokens=%d, batch_cost=$%.6f (₹%.2f)",
        count,
        jsonl_path,
        usage_meta["total_documents"],
        usage_meta["estimated_input_tokens"],
        usage_meta["estimated_output_tokens"],
        usage_meta["estimated_total_tokens"],
        usage_meta["batch_cost_usd"],
        usage_meta["batch_cost_inr"],
    )

    batch_job_name = submit_batch_job(jsonl_path)
    doc_ids = [d.get("standardized_doc_id") or d.get("doc_id") for d in docs]
    
    # Record batch job in Django DB via pipeline_client
    pipeline_client.record_batch_sectionizer_job(batch_job_name=batch_job_name, doc_ids=doc_ids, status="PENDING")
    
    pipeline_client.finish_agent_run(agent_run_id, status="complete", items_in=len(docs), items_out=len(docs))
    logger.info("Batch sectionizer job submitted successfully: %s | Estimated Cost: $%.6f", batch_job_name, usage_meta["batch_cost_usd"])


if __name__ == "__main__":
    main()
