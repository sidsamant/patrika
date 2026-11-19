"""`api` agent: called by the backend chat island (ChatApp.jsx) and by Django views.

Routes each message:
- JSON {"function": "estimatetokens", "payload": {...}}  -> token estimate for one article
- chat on a sectionizer article page (/sectionizer/<id>/)  -> Claude editor conversation
- anything else                                          -> nothing to do

Claude editor flow: prepare the batch job's prompt -> ask for cost approval (first draft of an
article only) -> call Claude -> save the turn (ui.ui_prompts) and the actual cost
(pipeline.llm_cost_approval_requests) -> show a draft card in the chat.
"""
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from google.adk import Context, Event, Workflow
from google.adk.events import RequestInput
from google.adk.workflow import node
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

import pipeline_client  # noqa: E402
from claude_editor import ClaudeArticleEditor, EditorResult, HistoryTurn  # noqa: E402
from sectionizer_prompt import ArticleNotReady, SectionizerPromptBuilder  # noqa: E402
from utils import ESTIMATED_OUTPUT_TOKENS_PER_DOC, build_editor_system_prompt  # noqa: E402

logger = logging.getLogger(__name__)

_builder = SectionizerPromptBuilder()
_editor = ClaudeArticleEditor()

# Only article pages: /sectionizer/<id>/ and /sectionizer/<id>/edit/. Other sectionizer pages
# also end in a number (batches/<id>, history/<id>, categories/<id>/edit) but are not articles.
_ARTICLE_PATH = re.compile(r"^/sectionizer/(\d+)/(?:edit/)?$")
TURN_TARGET_TYPE = "sectionizer_output"
TURN_PROVIDER = "claude"
TURN_PROMPT_TYPE = "refinement"


def sectionizer_output_id(page_url: str) -> int | None:
    match = _ARTICLE_PATH.match(urlsplit(page_url or "").path)
    return int(match.group(1)) if match else None


def _message_text(node_input: Any) -> str:
    if isinstance(node_input, str):
        return node_input
    parts = getattr(node_input, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _chat_message(ctx: Context, text: str) -> Event:
    return Event(invocation_id=ctx.invocation_id,
                 content=types.Content(role="model", parts=[types.Part(text=text)]))


# ── Router ────────────────────────────────────────────────────────────────────

async def router_node(ctx: Context, node_input: Any):
    text = _message_text(node_input)
    page_url = ctx.state.get("pageUrl", "")   # sent by ChatApp.jsx in the /run_sse stateDelta
    logger.info("api router: page_url=%s", page_url)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("function") == "estimatetokens":
        yield Event(route="route_estimate", state={"estimate_payload": payload.get("payload") or {}})
        return

    output_id = sectionizer_output_id(page_url)
    if output_id is not None and text.strip():
        yield Event(route="route_editor",
                    state={"editor_output_id": output_id, "editor_user_text": text.strip()})
        return
    yield Event(route="route_nothing")


@node(name="do_nothing_node")
def do_nothing_node(node_input: Any):
    yield Event(output="nothing to do")


# ── Token estimate ────────────────────────────────────────────────────────────

@node(name="estimatetokens_node")
async def estimatetokens_node(estimate_payload: dict):
    """Estimated tokens for one sectionizer output, from the same prompt the batch job sends."""
    try:
        article = await asyncio.to_thread(_builder.prepare, int(estimate_payload["output_id"]))
    except ArticleNotReady as exc:
        yield Event(output=str(exc), route="default")
        return
    # The system prompt is billed on every request (before any cache discount). Measured
    # against two real result lines: within 3-5% of the actual prompt tokens.
    yield Event(output={
        "estimated_input_tokens": (len(article.system_prompt) + len(article.body)) // 4,
        "estimated_output_tokens": ESTIMATED_OUTPUT_TOKENS_PER_DOC,
    })


# ── Claude editor ─────────────────────────────────────────────────────────────

def _load_history(output_id: int) -> list[HistoryTurn]:
    """Successful earlier turns for this article, oldest first."""
    history: list[HistoryTurn] = []
    for row in pipeline_client.list_chat_turns(target_type=TURN_TARGET_TYPE, target_id=output_id,
                                               provider=TURN_PROVIDER, prompt_type=TURN_PROMPT_TYPE):
        try:
            envelope = json.loads(row.get("response_text") or "{}")
        except json.JSONDecodeError:
            continue
        if envelope.get("claude"):
            history.append(HistoryTurn(user_text=row.get("user_prompt") or "",
                                       assistant_json=envelope["claude"]))
    return history


def format_editor_approval(estimate: dict[str, Any], output_id: int, title: str | None) -> str:
    """Markdown shown at the approval step (first draft of an article only)."""
    return "\n".join([
        "## Send this article to Claude?",
        "",
        f"Article #{output_id}" + (f": {title}" if title else ""),
        "",
        "| | |",
        "|---|---|",
        f"| Model | `{estimate['model']}` (effort {estimate['effort']}) |",
        f"| Est. input tokens | {estimate['estimated_input_tokens']:,} |",
        f"| Est. output tokens (incl. thinking) | {estimate['estimated_output_tokens']:,} |",
        f"| **Est. cost** | **${estimate['estimated_cost_usd']:,.4f} (₹{estimate['estimated_cost_inr']:,.2f})** |",
        "",
        "_Estimate only. You are asked once per article: later messages in this conversation are "
        "sent without asking, and every reply shows its actual cost._",
        "",
        "**Send to Claude? Reply `yes` or `no`.**",
    ])


async def editor_prepare_node(editor_output_id: int, editor_user_text: str):
    try:
        article = await asyncio.to_thread(_builder.prepare, editor_output_id)
        history = await asyncio.to_thread(_load_history, editor_output_id)
    except ArticleNotReady as exc:
        yield Event(route="route_stop", state={"editor_message": str(exc)})
        return
    except Exception:   # pipeline API down or returned an error
        logger.exception("Claude editor: could not load article #%s", editor_output_id)
        yield Event(route="route_stop", state={"editor_message": "Could not load the article from the pipeline API."})
        return

    estimate = _editor.estimate(article, history, editor_user_text)
    workflow_id = f"claude-editor-{editor_output_id}-{uuid4().hex[:12]}"
    needs_approval = not history   # approval for the first draft only
    try:
        created = await asyncio.to_thread(
            pipeline_client.create_approval_request,
            workflow_id=workflow_id, step_name="claude_editor", target_model=_editor.model,
            input_tokens=estimate["estimated_input_tokens"],
            estimated_output_tokens=estimate["estimated_output_tokens"],
            estimated_cost_usd=estimate["estimated_cost_usd"],
            estimated_cost_inr=estimate["estimated_cost_inr"],
            prompt_summary=f"Claude editor, sectionizer output #{editor_output_id}"
                           + (f": {article.title}" if article.title else ""),
            token_breakdown={"estimate": estimate},
        )
        if not needs_approval:
            await asyncio.to_thread(pipeline_client.decide_approval, workflow_id, True)
    except Exception:
        logger.exception("Claude editor: could not record the cost request")
        yield Event(route="route_stop", state={"editor_message": "Could not record the cost request, so nothing was sent."})
        return

    yield Event(
        route="route_approve" if needs_approval else "route_call",
        state={
            "editor_estimate": estimate,
            "editor_title": article.title,
            "editor_approval": {"approval_id": created.get("approval_id"), "workflow_id": workflow_id},
        },
    )


async def editor_approval_node(editor_estimate: dict, editor_output_id: int, editor_title: str | None = None):
    yield RequestInput(message=format_editor_approval(editor_estimate, editor_output_id, editor_title))


async def editor_approval_response_node(node_input: Any, editor_approval: dict):
    approved = _message_text(node_input).strip().lower() in ("yes", "y", "approved", "approve")
    try:
        await asyncio.to_thread(pipeline_client.decide_approval, editor_approval["workflow_id"], approved)
    except Exception:
        logger.exception("Claude editor: could not record the approval decision")
    yield Event(route="route_proceed" if approved else "route_reject")


async def editor_rejected_node():
    yield Event(output="Not sent to Claude. Nothing was billed.")


async def editor_stop_node(editor_message: str):
    yield Event(output=f"Cannot send this article to Claude: {editor_message}")


def _record_actuals(approval_id: int | None, result: EditorResult) -> None:
    if not approval_id or not result.usage:
        return
    u = result.usage
    prompt_tokens = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
    pipeline_client.update_approval_actuals(
        approval_id,
        actual_prompt_tokens=prompt_tokens,
        actual_completion_tokens=u["output_tokens"],
        actual_total_tokens=prompt_tokens + u["output_tokens"],
        actual_cost_usd=(result.cost or {}).get("total_cost_usd", 0),
        actual_cost_inr=(result.cost or {}).get("total_cost_inr", 0),
        actual_breakdown={**u, "stop_reason": result.stop_reason, "latency_ms": result.latency_ms,
                          "priced": result.cost is not None},
    )


async def editor_call_node(ctx: Context, editor_output_id: int, editor_user_text: str, editor_approval: dict):
    try:
        article = await asyncio.to_thread(_builder.prepare, editor_output_id)
        history = await asyncio.to_thread(_load_history, editor_output_id)
    except Exception as exc:
        logger.exception("Claude editor: could not reload article #%s", editor_output_id)
        yield _chat_message(ctx, f"Could not load the article: {exc}")
        return

    result = await _editor.run(article, history, editor_user_text)

    try:
        await asyncio.to_thread(_record_actuals, editor_approval.get("approval_id"), result)
    except Exception:
        logger.exception("Claude editor: could not record actual cost")

    prompt_id = None
    if result.ok:
        try:
            saved = await asyncio.to_thread(
                pipeline_client.create_chat_turn,
                session_id=getattr(getattr(ctx, "session", None), "id", None),
                prompt_type=TURN_PROMPT_TYPE, provider=TURN_PROVIDER,
                model=result.served_model or _editor.model,
                system_prompt=build_editor_system_prompt(article.categories),
                user_prompt=editor_user_text,
                response_text=json.dumps({"claude": result.raw_text, **editor_approval}),
                input_tokens=result.usage.get("input_tokens"),
                output_tokens=result.usage.get("output_tokens"),
                latency_ms=result.latency_ms,
                target_type=TURN_TARGET_TYPE, target_id=editor_output_id,
            )
            prompt_id = saved.get("prompt_id")
        except Exception:
            logger.exception("Claude editor: could not save the chat turn")

    card = {
        "type": "claude_turn",
        "ok": result.ok,
        "output_id": editor_output_id,
        "prompt_id": prompt_id,
        "reply": result.reply,
        "draft": result.draft,
        "model": result.served_model or _editor.model,
        "usage": result.usage,
        "cost_usd": (result.cost or {}).get("total_cost_usd"),
        "cost_inr": (result.cost or {}).get("total_cost_inr"),
    }
    yield _chat_message(ctx, f"{result.reply}\n\n```json\n{json.dumps(card, indent=2)}\n```")


# ── Workflow ──────────────────────────────────────────────────────────────────

root_agent = Workflow(
    name="root_agent",
    edges=[
        ("START", router_node),
        (router_node, {
            "route_estimate": estimatetokens_node,
            "route_editor": editor_prepare_node,
            "route_nothing": do_nothing_node,
        }),
        (editor_prepare_node, {
            "route_approve": editor_approval_node,
            "route_call": editor_call_node,
            "route_stop": editor_stop_node,
        }),
        (editor_approval_node, editor_approval_response_node),
        (editor_approval_response_node, {
            "route_proceed": editor_call_node,
            "route_reject": editor_rejected_node,
        }),
    ],
)
