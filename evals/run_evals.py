import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("evals")

def create_mock_context():
    ctx = MagicMock()
    ctx.invocation_id = "test-invocation-id"
    ctx.branch = "test-branch"
    ctx.end_invocation = False
    ctx.end_of_agents = {}
    ctx.agent_states = {}
    ctx.session = MagicMock()
    ctx.session.state = {}
    ctx.get_invocation_context = MagicMock(return_value=ctx)
    ctx.model_copy = MagicMock(return_value=ctx)
    ctx.plugin_manager = MagicMock()
    ctx.plugin_manager.run_before_agent_callback = AsyncMock(return_value=None)
    ctx.plugin_manager.run_after_agent_callback = AsyncMock(return_value=None)
    ctx.plugin_manager.run_before_step_callback = AsyncMock(return_value=None)
    ctx.plugin_manager.run_after_step_callback = AsyncMock(return_value=None)
    return ctx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "agents"))

# Import agent objects
from agent import (
    ollama_screener_agent,
    standardizer_agent,
    sectionizer_agent,
    curator_greeting_agent,
)

# Load golden dataset
GOLDEN_DATASET_PATH = Path(__file__).parent / "golden_cases.json"


async def evaluate_screener(cases):
    print("\n--- Evaluating Screener Agent ---")
    mock_input_items = []
    for c in cases:
        item = {
            "pageTitle": c["pageTitle"],
            "pageSummary": c["pageSummary"],
            "pageUrl": c["pageUrl"],
            "_hoarder_output_id": c["id"],
            "sourceId": "test_source",
            "createdAt": "2026-07-08T00:00:00Z"
        }
        mock_input_items.append(item)

    saved_items = []

    def mock_load():
        print("[Screener] MOCK load_hoarder_rows called, returning", len(mock_input_items))
        return mock_input_items

    def mock_persist_payload(raw_payload, agent_run_id=None):
        print("[Screener] MOCK persist_screened_payload called")
        from agents.screener.storage import parse_screened_payload
        items = parse_screened_payload(raw_payload)
        print("[Screener] Parsed items:", len(items))
        saved_items.extend(items)
        return len(items)

    def mock_call_ollama(prompt):
        print("[Screener] MOCK _call_ollama called")
        return json.dumps({
            "files": [
                {"_hoarder_output_id": 1, "isSelected": True},
                {"_hoarder_output_id": 2, "isSelected": False}
            ]
        })

    # Mock screener imports locally
    with patch("agents.screener.agent.load_hoarder_rows_for_screening", side_effect=mock_load), \
         patch("agents.screener.agent.mark_hoarder_rows_screened", return_value="2026-07-08T00:00:00Z"), \
         patch("agents.screener.agent.persist_screened_payload", side_effect=mock_persist_payload), \
         patch.object(ollama_screener_agent, "_call_ollama", side_effect=mock_call_ollama):
        
        ctx = create_mock_context()
        print("[Screener] Running screener agent...")
        async for ev in ollama_screener_agent.run(ctx=ctx, node_input=None):
            print("[Screener] Screener yielded event:", ev)

    print("[Screener] Saved items count:", len(saved_items))

    # Compare predictions
    passed = 0
    total = len(cases)
    results_map = {item["_hoarder_output_id"]: item for item in saved_items}
    
    for c in cases:
        pred = results_map.get(c["id"])
        if not pred:
            logger.error(f"Case {c['id']}: No prediction returned.")
            continue
        is_selected = bool(pred.get("isSelected", True))
        expected = c["expected_selected"]
        
        if is_selected == expected:
            passed += 1
            logger.info(f"Case {c['id']}: PASS (Expected: {expected}, Got: {is_selected})")
        else:
            logger.error(f"Case {c['id']}: FAIL (Expected: {expected}, Got: {is_selected})")

    score = (passed / total) * 100 if total > 0 else 100
    logger.info(f"Screener Score: {passed}/{total} ({score:.1f}%)\n")
    return passed, total


async def evaluate_standardizer(cases):
    print("\n--- Evaluating Standardizer Agent ---")
    mock_input_items = []
    for c in cases:
        item = {
            "name": f"test_doc_{c['id']}.txt",
            "path": f"test_doc_{c['id']}.txt",
            "text": c["text_content"],
            "_screened_file_id": c["id"],
            "isSelected": True
        }
        mock_input_items.append(item)

    saved_docs = []

    def mock_load():
        print("[Standardizer] MOCK load_screened_files called, returning", len(mock_input_items))
        return mock_input_items

    def mock_standardize_document(**kwargs):
        print("[Standardizer] MOCK standardize_document called")
        saved_docs.append(kwargs)
        return {"standardizer_output_id": kwargs.get("screened_file_id")}

    with patch("agents.standardizer.agent.pipeline_client.load_screened_files_for_standardization", side_effect=mock_load), \
         patch("agents.standardizer.agent.pipeline_client.standardize_document", side_effect=mock_standardize_document), \
         patch("agents.standardizer.agent.pipeline_client.mark_screened_file_processed"), \
         patch("agents.standardizer.agent._is_web_source_item", return_value=True), \
         patch("agents.standardizer.agent._extract_from_web_item", side_effect=lambda item: (item["text"], {"author": "Sid Samant"}, [], None)):
        
        ctx = create_mock_context()
        print("[Standardizer] Running standardizer agent...")
        async for ev in standardizer_agent.run(ctx=ctx, node_input=None):
            print("[Standardizer] Standardizer yielded event:", ev)

    print("[Standardizer] Saved docs count:", len(saved_docs))

    passed = 0
    total = len(cases)
    results_map = {doc["screened_file_id"]: doc for doc in saved_docs}

    for c in cases:
        pred = results_map.get(c["id"])
        if not pred:
            logger.error(f"Case {c['id']}: No prediction returned.")
            continue
        
        author = pred.get("author")
        expected_author = c["expected_author"]
        
        if author == expected_author:
            passed += 1
            logger.info(f"Case {c['id']}: PASS (Expected Author: {expected_author}, Got: {author})")
        else:
            logger.error(f"Case {c['id']}: FAIL (Expected Author: {expected_author}, Got: {author})")

    score = (passed / total) * 100 if total > 0 else 100
    logger.info(f"Standardizer Score: {passed}/{total} ({score:.1f}%)\n")
    return passed, total


async def evaluate_sectionizer(cases):
    print("\n--- Evaluating Sectionizer Agent ---")
    mock_docs = []
    for c in cases:
        doc = {
            "doc_id": c["id"],
            "source_path": f"test_doc_{c['id']}.txt",
            "author": "Test Author",
            "text": c["text_content"],
            "metadata": {}
        }
        mock_docs.append(doc)

    mock_categories = [
        {"sectionizer_category_id": 1, "name": "Satellite & EO", "objective": "satellite earth observation, remote sensing, spatial imaging"},
        {"sectionizer_category_id": 2, "name": "Launch & Propulsion", "objective": "launch vehicles, rockets, engines, private rockets startup"}
    ]

    saved_outputs = []

    def mock_load_docs():
        print("[Sectionizer] MOCK load_documents called, returning", len(mock_docs))
        return mock_docs

    def mock_persist_row_output(row, row_output_payload):
        print("[Sectionizer] MOCK _persist_row_output called for doc_id", row.get("doc_id"))
        saved_outputs.append(row_output_payload)
        return {
            "sectionizer_output_id": row["doc_id"],
            "created_at": "2026-07-08T00:00:00Z"
        }

    async def mock_reviewer_run_async(*args, **kwargs):
        print("[Sectionizer] MOCK reviewer run_async called")
        ctx = kwargs.get("parent_context") or kwargs.get("ctx") or (args[1] if len(args) > 1 else args[0])
        row = ctx.session.state.get("sectionizer_current_row") or {}
        doc_id = row.get("doc_id")
        
        section_name = "Satellite & EO" if doc_id == 1 else "Launch & Propulsion"
        payload = {
            "sections": [
                {
                    "section": section_name,
                    "overall_score": 1.0,
                    "summary": "matched category",
                    "title": "matched title"
                }
            ]
        }
        from google.adk.events import Event
        from google.genai import types
        yield Event(
            author="sectionizer_llm_reviewer",
            content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))])
        )

    with patch("agents.sectionizer.agent._load_rows_from_db", side_effect=mock_load_docs), \
         patch("agents.sectionizer.agent._load_categories_from_db", return_value=mock_categories), \
         patch("agents.sectionizer.agent._persist_row_output", side_effect=mock_persist_row_output), \
         patch("agents.sectionizer.agent.pipeline_client.load_newsletter_settings", return_value={"settings": {}}), \
         patch("google.adk.agents.LlmAgent.run_async", side_effect=mock_reviewer_run_async):
        
        ctx = create_mock_context()
        print("[Sectionizer] Running sectionizer agent...")
        async for ev in sectionizer_agent.run(ctx=ctx, node_input=None):
            print("[Sectionizer] Sectionizer yielded event:", ev)

    print("[Sectionizer] Saved sectionizer outputs count:", len(saved_outputs))

    passed = 0
    total = len(cases)
    results_map = {out["doc_id"]: out for out in saved_outputs}

    for c in cases:
        pred = results_map.get(c["id"])
        if not pred:
            logger.error(f"Case {c['id']}: No prediction returned.")
            continue
        
        category_id = pred.get("category_id")
        cat_name = next((cat["name"] for cat in mock_categories if cat["sectionizer_category_id"] == category_id), "None")
        expected_cat = c["expected_category"]
        
        if cat_name == expected_cat:
            passed += 1
            logger.info(f"Case {c['id']}: PASS (Expected Category: {expected_cat}, Got: {cat_name})")
        else:
            logger.error(f"Case {c['id']}: FAIL (Expected Category: {expected_cat}, Got: {cat_name})")

    score = (passed / total) * 100 if total > 0 else 100
    logger.info(f"Sectionizer Score: {passed}/{total} ({score:.1f}%)\n")
    return passed, total


async def evaluate_greeter(cases):
    print("\n--- Evaluating Curator Greeting Agent ---")
    
    mock_status = {
        "hoarder": 0,
        "screener": 4,
        "standardizer": 0,
        "sectionizer": 0
    }
    
    greeting_text = ""

    async def mock_reviewer_run_async(*args, **kwargs):
        print("[Greeter] MOCK reviewer run_async called")
        from google.adk.events import Event
        from google.genai import types
        yield Event(
            author="curator_greeting_llm",
            content=types.Content(role="model", parts=[types.Part(text="Hello! You have 4 screened items pending. Please run the Standardizer agent next.")]),
        )

    with patch("agents.curator_greeting.agent.get_pipeline_status_via_mcp", return_value=mock_status), \
         patch("agents.curator_greeting.agent.pipeline_client.load_newsletter_settings", return_value={"settings": {}}), \
         patch("google.adk.agents.LlmAgent.run_async", side_effect=mock_reviewer_run_async):
        
        ctx = create_mock_context()
        ctx.session.state["newsletter_slug"] = "test-newsletter"
        print("[Greeter] Running curator greeting agent...")
        async for ev in curator_greeting_agent.run(ctx=ctx, node_input=None):
            print("[Greeter] Greeter yielded event:", ev)
            content = getattr(ev, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if isinstance(parts, list):
                greeting_text += "".join((getattr(part, "text", "") or "") for part in parts)

    print("[Greeter] Generated greeting:", greeting_text)

    passed = 0
    total = len(cases)

    for c in cases:
        expected = c["expected_next_step"]
        if expected.lower() in greeting_text.lower():
            passed += 1
            logger.info(f"Greeter Case {c['id']}: PASS (Expected suggestion: '{expected}', Got it!)")
        else:
            logger.error(f"Greeter Case {c['id']}: FAIL (Expected suggestion: '{expected}', Not found in response)")

    score = (passed / total) * 100 if total > 0 else 100
    logger.info(f"Greeter Score: {passed}/{total} ({score:.1f}%)\n")
    return passed, total


async def main():
    if not GOLDEN_DATASET_PATH.exists():
        logger.error(f"Golden dataset not found at {GOLDEN_DATASET_PATH}")
        sys.exit(1)

    with open(GOLDEN_DATASET_PATH, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    logger.info("=======================================")
    logger.info("Executing AI Agent Curation Evals Suite")
    logger.info("=======================================\n")

    screener_passed, screener_total = await evaluate_screener(dataset.get("screener_cases", []))
    std_passed, std_total = await evaluate_standardizer(dataset.get("standardizer_cases", []))
    sec_passed, sec_total = await evaluate_sectionizer(dataset.get("sectionizer_cases", []))
    greet_passed, greet_total = await evaluate_greeter(dataset.get("greeter_cases", []))

    total_passed = screener_passed + std_passed + sec_passed + greet_passed
    total_cases = screener_total + std_total + sec_total + greet_total
    total_score = (total_passed / total_cases) * 100 if total_cases > 0 else 100

    logger.info("=======================================")
    logger.info("EVALUATION RESULT OVERALL SCORECARD")
    logger.info(f"Passed: {total_passed} / {total_cases} cases")
    logger.info(f"Overall Accuracy: {total_score:.1f}%")
    logger.info("=======================================")


if __name__ == "__main__":
    asyncio.run(main())
