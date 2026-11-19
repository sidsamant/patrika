"""Claude editor: a back-and-forth conversation about one article, using the batch job's prompt.

Used by the `api` agent when the chat runs on a sectionizer detail page. Each call sends the
whole conversation (first turn plus the most recent ones) and returns a reply and, when the
article changes, a draft that follows the batch job's Result schema.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from sectionizer_prompt import PreparedArticle
from utils import ESTIMATED_OUTPUT_TOKENS_PER_DOC, EditorTurn, build_editor_schema, build_editor_system_prompt
from workflows.pricing_matrix import calculate_claude_cost

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000
# Opt-in server-side fallback: if Claude declines on policy grounds, the API retries the same
# request on a fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class HistoryTurn:
    user_text: str        # what the editor typed
    assistant_json: str   # Claude's raw JSON reply, replayed as the assistant message


@dataclass
class EditorResult:
    ok: bool
    reply: str
    draft: dict[str, Any] | None = None
    raw_text: str | None = None          # Claude's JSON, stored for replay
    served_model: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] | None = None
    latency_ms: int | None = None


class ClaudeArticleEditor:
    """Calls Claude with the editor conversation for one article."""

    MAX_HISTORY_TURNS = 10   # the first turn (with the article) is always kept

    def __init__(self, model: str | None = None, effort: str | None = None,
                 client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = model or os.getenv("CLAUDE_EDITOR_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.getenv("CLAUDE_EDITOR_EFFORT", DEFAULT_EFFORT)
        self._client = client

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        # Created lazily so the ADK server starts even when ANTHROPIC_API_KEY is not set yet.
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return self._client

    # ── Prompt ────────────────────────────────────────────────────────────────

    def build_messages(self, article: PreparedArticle, history: list[HistoryTurn],
                       user_text: str) -> list[dict[str, Any]]:
        kept = history
        if len(history) >= self.MAX_HISTORY_TURNS:
            kept = [history[0]] + history[-(self.MAX_HISTORY_TURNS - 1):]
        messages: list[dict[str, Any]] = []
        for turn in kept:
            messages.append({"role": "user", "content": turn.user_text})
            messages.append({"role": "assistant", "content": turn.assistant_json})
        messages.append({"role": "user", "content": user_text})
        # The article goes with the first user message, outside of any instruction text.
        first = messages[0]["content"]
        messages[0]["content"] = f"{article.body}\n\nEditor: {first}"
        return messages

    def estimate(self, article: PreparedArticle, history: list[HistoryTurn],
                 user_text: str) -> dict[str, Any]:
        """Rough pre-call estimate (characters / 4, as the batch job does)."""
        system = build_editor_system_prompt(article.categories)
        chars = len(system) + sum(len(m["content"]) for m in self.build_messages(article, history, user_text))
        input_tokens = max(1, chars // 4)
        output_tokens = ESTIMATED_OUTPUT_TOKENS_PER_DOC * 2   # thinking is most of the output
        cost = calculate_claude_cost(self.model, input_tokens, output_tokens)
        return {"model": self.model, "effort": self.effort,
                "estimated_input_tokens": input_tokens, "estimated_output_tokens": output_tokens,
                "estimated_cost_usd": cost["total_cost_usd"], "estimated_cost_inr": cost["total_cost_inr"]}

    # ── Call ──────────────────────────────────────────────────────────────────

    async def run(self, article: PreparedArticle, history: list[HistoryTurn],
                  user_text: str) -> EditorResult:
        system = build_editor_system_prompt(article.categories)
        t0 = time.monotonic()
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=self.build_messages(article, history, user_text),
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": build_editor_schema(article.categories)},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            logger.exception("Claude editor: authentication failed")
            return EditorResult(ok=False, reply="Claude rejected the API key. Check ANTHROPIC_API_KEY in newsletter_adk/.env.")
        except anthropic.RateLimitError:
            logger.warning("Claude editor: rate limited")
            return EditorResult(ok=False, reply="Claude is rate limiting requests. Try again in a minute.")
        except anthropic.BadRequestError as exc:
            logger.exception("Claude editor: bad request")
            return EditorResult(ok=False, reply=f"Claude rejected the request: {exc.message}")
        except anthropic.APIStatusError as exc:
            logger.exception("Claude editor: API error %s", exc.status_code)
            return EditorResult(ok=False, reply=f"Claude API error {exc.status_code}. Try again later.")
        except anthropic.APIConnectionError:
            logger.exception("Claude editor: connection error")
            return EditorResult(ok=False, reply="Could not reach the Claude API.")

        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = self._usage(response)
        cost = self._cost(usage)
        base = dict(served_model=response.model, stop_reason=response.stop_reason,
                    usage=usage, cost=cost, latency_ms=latency_ms)
        logger.info("Claude editor: output #%s model=%s stop=%s usage=%s latency=%dms",
                    article.output_id, response.model, response.stop_reason, usage, latency_ms)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return EditorResult(ok=False, reply="Claude declined this request"
                                + (f" ({category})." if category else "."), **base)
        if response.stop_reason == "max_tokens":
            return EditorResult(ok=False, reply="Claude's reply was cut off at the length limit.", **base)

        raw = "".join(b.text for b in response.content if b.type == "text")
        try:
            turn = EditorTurn.model_validate_json(raw)
        except ValidationError:
            logger.exception("Claude editor: reply did not match the schema: %.300s", raw)
            return EditorResult(ok=False, reply="Claude's reply did not match the expected format.",
                                raw_text=None, **base)
        return EditorResult(ok=True, reply=turn.reply, draft=self._draft(turn, article),
                            raw_text=raw, **base)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _draft(turn: EditorTurn, article: PreparedArticle) -> dict[str, Any] | None:
        if turn.draft is None:
            return None
        names = article.category_names
        valid = [i for i in turn.draft.category_ids if i in names]   # never invent IDs
        return {**turn.draft.model_dump(),
                "category_ids": [int(i) for i in valid],
                "category_names": [names[i] for i in valid]}

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        u = response.usage
        details = getattr(u, "output_tokens_details", None)
        iterations = getattr(u, "iterations", None) or []
        return {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,   # includes thinking
            "thinking_tokens": getattr(details, "thinking_tokens", None) if details else None,
            "cache_read_input_tokens": u.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
            "fallback_used": any(getattr(i, "type", None) == "fallback_message" for i in iterations),
            "served_model": response.model,
        }

    def _cost(self, usage: dict[str, Any]) -> dict[str, Any] | None:
        model = usage.get("served_model") or self.model
        try:
            return calculate_claude_cost(
                model, usage["input_tokens"], usage["output_tokens"],
                cache_read_tokens=usage["cache_read_input_tokens"],
                cache_write_tokens=usage["cache_creation_input_tokens"])
        except KeyError:
            logger.warning("Claude editor: no price for model %s; cost not recorded", model)
            return None
