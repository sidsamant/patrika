

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from typing_extensions import TypedDict
from typing import Any, Literal, Optional
from pathlib import Path
from cleantext import clean
import json

MIN_SOURCE_WORDS = 40     # below this an item is really just a headline: skipped, not sent
SKIP_REASON_THIN = "thin_source"
MAX_ARTICLE_CHARS = 4000  # same cap as before (content_text[:4000])

ESTIMATED_OUTPUT_TOKENS_PER_DOC = 1000   # flat; thinking tokens are most of the real output

# 1. Define the structure of your dynamic dictionary
class NodeInput(TypedDict, total=False):
    load_documents_for_sectionizing: list[dict[str, Any]]
    load_categories_for_categories: list[dict[str, Any]]


class ArticleIn(BaseModel):
    """One document as returned by the API. Only whitelisted fields ever reach the model."""
    model_config = ConfigDict(extra="ignore")  # source_path, metadata, timestamps, ... dropped
    # The API returns the body as "text"; "text_content" is accepted too.
    text: str | None = Field(default=None, validation_alias=AliasChoices("text_content", "text","contentText"))
    # In your data "author" is the crawl source name, not a person, so it is sent as <source>.
    source_site: str | None = Field(default=None, validation_alias="author")
    # Not provided by the API today (created_at is the DB insert time, not the publication
    # date). Fill these only if the API ever returns real values.
    title: str | None = Field(default=None, validation_alias=AliasChoices("pageTitle"))
    published: str | None = Field(default=None, validation_alias="published_date") 

class SectionizerOutputBatchMetadata(BaseModel):
    batch_job_name: str
    key: str
    content_type: str
    category_ids: list[str]
    is_primary: bool
    confidence: float
    finish_reason: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    emulated: bool = False

class SectionizerOutput(BaseModel):
    document_summary: str
    # Replace `dict[str, Any]` with a specific SectionEvaluation model if you have its schema
    section_evaluations: list[dict[str, Any]] = Field(default_factory=list)
    llm_instruction: str
    llm_content: str
    section_config_source: Literal["gemini-batch"] = Field(
        default="gemini-batch", 
        alias="sectionConfigSource"
    )
    batch: SectionizerOutputBatchMetadata

# ---------- Input models (pydantic does the parsing; unknown keys are ignored) ----------
class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")  # drops rules, min_score
    id: int = Field(validation_alias="sectionizer_category_id")
    name: str
    objective: str
    ai_instructions: str | None = None

# ---------- Output schema (edit fields here only) ----------
class Result(BaseModel):
    category_ids: list[str] = Field(
        max_length=3,
        description="Matching category IDs, most relevant first. Empty if none fit.")
    content_type: Literal["news", "analysis", "opinion"]
    confidence: float = Field(ge=0, le=1, description="0-1 certainty of the primary category")
    new_title: str
    new_text: str

# Prompt templates (Jinja2) live next to this file. StrictUndefined = a typo'd variable raises
# instead of silently rendering as empty text.
_jinja = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent)),
    undefined=StrictUndefined,
    autoescape=False,        # plain-text prompts: article text must not be HTML-escaped
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)
SYSTEM_PROMPT_TEMPLATE = _jinja.get_template("system_prompt.md.j2")
ARTICLE_PROMPT_TEMPLATE = _jinja.get_template("article_prompt.md.j2")

def load_categories(raw: list[dict[str, Any]]) -> list[Category]:
    # sorted by id -> byte-identical system prompt on every request (cache friendly)
    return sorted((Category.model_validate(c) for c in raw), key=lambda c: c.id)

def build_schema(cats: list[Category]) -> dict[str, Any]:
    schema = Result.model_json_schema()
    schema.pop("title", None)
    for p in schema["properties"].values():
        p.pop("title", None)
    schema["properties"]["category_ids"]["items"] = {
        "type": "string",
        "enum": [str(c.id) for c in cats],
    }
    return schema



def clean_text(t: str | None) -> str:
    return clean(
        t or "",
        fix_unicode=True, to_ascii=False, lower=False, no_line_breaks=False,
        no_urls=True, replace_with_url="",
        no_emails=True, replace_with_email="",
    )


MAX_ARTICLE_CHARS = 4000  # same cap as before (content_text[:4000])
def build_body(a: ArticleIn) -> str:
    # The template omits <title> and <published> when empty.
    return ARTICLE_PROMPT_TEMPLATE.render(
        title=clean_text(a.title),
        source=a.source_site,
        published=a.published,
        text=clean_text(a.text)[:MAX_ARTICLE_CHARS],
    )


def parse_result_line(line: str, cats: list[Category]) -> dict[str, Any]:
    """Use this where the batch results file is read."""
    rec = json.loads(line)
    key = rec["key"]
    resp = rec.get("response") or {}
    cands = resp.get("candidates") or []
    parts = (cands[0].get("content") or {}).get("parts") if cands else None
    if not parts:
        reason = ((resp.get("promptFeedback") or {}).get("blockReason")
                  or (cands[0].get("finishReason") if cands else None)
                  or rec.get("error") or "NO_CANDIDATE")
        return {"key": key, "error": f"blocked_or_empty: {reason}"}
    try:
        res = Result.model_validate_json("".join(p.get("text", "") for p in parts))
    except Exception as e:
        return {"key": key, "error": f"invalid_output: {e}"}
    names = {str(c.id): c.name for c in cats}
    valid = [i for i in res.category_ids if i in names]
    return {
        "key": key,
        "doc_id": key.removeprefix("doc_"),
        **res.model_dump(),
        "category_ids": [int(i) for i in valid],
        "category_names": [names[i] for i in valid],
    }


def build_system_prompt(cats: list[Category]) -> str:
    return SYSTEM_PROMPT_TEMPLATE.render(categories=cats)


# ---------- Claude editor (chat on the sectionizer detail page) ----------
EDITOR_ADDENDUM_TEMPLATE = _jinja.get_template("editor_conversation.md.j2")


class EditorTurn(BaseModel):
    """One Claude reply in the editor conversation. draft follows the batch job's Result."""
    reply: str
    draft: Result | None = None


def build_editor_system_prompt(cats: list[Category]) -> str:
    """The batch job's system prompt, unchanged, followed by the conversation rules."""
    return build_system_prompt(cats) + "\n\n" + EDITOR_ADDENDUM_TEMPLATE.render()


# Claude structured outputs reject these JSON Schema keywords; Result.model_validate still
# enforces them on the reply.
_CLAUDE_UNSUPPORTED_KEYS = ("maxItems", "minItems", "minimum", "maximum",
                            "exclusiveMinimum", "exclusiveMaximum", "maxLength", "minLength")


def build_editor_schema(cats: list[Category]) -> dict[str, Any]:
    """JSON schema for EditorTurn that Claude structured outputs accept."""
    draft = build_schema(cats)
    for prop in draft["properties"].values():
        for key in _CLAUDE_UNSUPPORTED_KEYS:
            prop.pop(key, None)
    draft["additionalProperties"] = False
    draft["required"] = list(draft["properties"])
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "draft"],
        "properties": {
            "reply": {"type": "string", "description": "Short message to the editor."},
            "draft": {"anyOf": [draft, {"type": "null"}]},
        },
    }