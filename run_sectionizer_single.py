from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("run_sectionizer_single")

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(PROJECT_ROOT / ".env")
import pipeline_client


def _get_genai_client() -> genai.Client:
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GENAI_API_KEY"):
        api_key = os.getenv(env_name)
        if api_key:
            return genai.Client(api_key=api_key)
    return genai.Client()


def regenerate_single_article(doc_id: int) -> dict:
    """Executes a direct real-time Gemini LLM call for a single article and ALWAYS creates a NEW SectionizerOutput row."""
    logger.info("Executing direct real-time sectionizer generation for doc_id=%d...", doc_id)
    
    cats = pipeline_client.load_sectionizer_categories()
    cats_block = "\n".join([f"- [{c.get('id')}] {c.get('name')}: {c.get('description', '')}" for c in cats])
    
    client = _get_genai_client()
    prompt = f"Sectionize standardized document {doc_id} using categories:\n{cats_block}"
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    
    raw_text = response.text or "{}"
    try:
        output_json = json.loads(raw_text)
    except Exception:
        output_json = {"raw": raw_text}

    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # ALWAYS create a NEW SectionizerOutput row in Django DB (append-only)
    new_output = pipeline_client.create_sectionizer_output(
        doc_id=doc_id,
        category_id=None,
        source_path="",
        llm_instruction="direct_single_article_regen",
        llm_content=raw_text,
        output_json=output_json,
        match_count=len(output_json.get("matches", [])) if isinstance(output_json, dict) else 0,
        run_timestamp=run_timestamp,
    )
    
    logger.info("Successfully created NEW SectionizerOutput row (ID: %s) for doc_id=%d", new_output.get("sectionizer_output_id"), doc_id)
    return new_output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Single Article Direct Sectionizer Regeneration")
    parser.add_argument("--doc_id", type=int, required=True, help="Standardized Document ID")
    args = parser.parse_args()
    
    result = regenerate_single_article(args.doc_id)
    print(json.dumps(result, indent=2))
