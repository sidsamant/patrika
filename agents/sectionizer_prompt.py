"""Builds the batch job's prompt for one sectionizer output.

Shared by the token estimate and the Claude editor chat (agents/api), so both see exactly the
article the batch job would send: same cleaning, same length cap, same thin-source check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

import pipeline_client
from utils import (
    MIN_SOURCE_WORDS,
    ArticleIn,
    Category,
    SectionizerOutput,
    build_body,
    build_system_prompt,
    clean_text,
    load_categories,
)


class ArticleNotReady(Exception):
    """The article cannot be sent to a model. str(exc) is a message for the user."""


class ThinArticle(ArticleNotReady):
    """Below MIN_SOURCE_WORDS. The batch job skips it; the editor may send it if the user agrees."""

    def __init__(self, words: int) -> None:
        self.words = words
        super().__init__(f"Only {words} word{'' if words == 1 else 's'} of text (minimum {MIN_SOURCE_WORDS}).")


@dataclass(frozen=True)
class PreparedArticle:
    output_id: int
    categories: list[Category]
    system_prompt: str   # the batch job's system prompt
    body: str            # the <article> user message
    words: int
    title: str | None

    @property
    def category_names(self) -> dict[str, str]:
        return {str(c.id): c.name for c in self.categories}


class SectionizerPromptBuilder:
    """Loads a sectionizer output, its source article and the categories via the pipeline API."""

    def __init__(self, client: Any = pipeline_client) -> None:
        self._client = client

    def prepare(self, output_id: int, allow_thin: bool = False) -> PreparedArticle:
        """allow_thin=True skips the minimum-length check (the user chose to send it anyway)."""
        raw = self._client.load_sectionizer_output(str(output_id))
        if not raw:
            raise ArticleNotReady(f"Sectionizer output #{output_id} was not found.")
        try:
            output = SectionizerOutput.model_validate(raw)
        except ValidationError as exc:
            raise ArticleNotReady(
                f"Sectionizer output #{output_id} was not created by the batch job, so its "
                f"source article cannot be found ({exc.error_count()} field error(s)).") from exc

        doc = self._client.load_hoarder_output(output.batch.key.removeprefix("doc_"))
        article = ArticleIn.model_validate(doc["source_payload_json"])
        words = len(clean_text(article.text).split())
        if words < MIN_SOURCE_WORDS and not allow_thin:
            # A headline-only item has little to rewrite; the batch job never sends it.
            raise ThinArticle(words)

        cats = load_categories(self._client.load_sectionizer_categories())
        return PreparedArticle(
            output_id=output_id,
            categories=cats,
            system_prompt=build_system_prompt(cats),
            body=build_body(article),
            words=words,
            title=article.title,
        )
