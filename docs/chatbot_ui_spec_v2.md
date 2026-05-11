# Chatbot UI Specification v2 — Newsletter Pipeline Assistant

**Supersedes:** `docs/chatbot_ui_spec.md` (v1)  
**Created:** 2026-05-07  
**Stack:** Django 5.x · Django Channels 4.x · HTMX · Celery · PostgreSQL  
**Scope:** Chat-first admin application that replaces the batch CLI pipeline. Full Django ORM — SQLAlchemy is retired. See `docs/django_orm_migration.md` for the ORM migration guide.

---

## 1. Design Principles

| Principle | Application |
|---|---|
| **Chat-first** | Every workflow — running agents, reviewing results, editing sectionizer outputs, changing settings — is reachable from the chat interface. Separate pages are read-only visualization panels, not entry points. |
| **DRY** | One LLM provider abstraction, one prompt-save function, one agent runner, one tool dispatcher. No per-app duplicates. |
| **Model-agnostic** | The chatbot and all AI refinements route through a single `LLMProvider` protocol. Swapping Gemini → Claude → Ollama is a config change, not a code change. |
| **Non-destructive** | No existing pipeline table rows are ever updated by the UI. Edits and refinements layer on top via `sectionizer_output_edits`. |
| **Auditable** | Every LLM call (system prompt, user prompt, response, tokens, latency, model) is recorded in `ui_prompts`. |

---

## 2. What "Chat-First for All Phases" Means

The chat is the control plane. Every workflow maps to a chat interaction:

| Phase | Chat interaction | What happens |
|---|---|---|
| Check pipeline health | "What's ready to run?" | `get_pipeline_status` tool → status card pushed to chat |
| Run a single stage | "Run the screener" | `run_screener` Celery task → progress streamed via WebSocket |
| Run full pipeline | "Run everything" | sequential Celery tasks, per-stage updates in chat |
| Review sectionizer results | "Show me unreviewed sections" | `list_sectionizer_outputs` tool → cards in chat with "Open" links |
| Edit a sectionizer result | "Rewrite Engineering summary for doc 42 to be more concise" | `refine_sectionizer_output` tool → diff in chat → accept/reject |
| Manual field override (chat) | "Set the newsletter title for doc 42 Engineering to 'New Release'" | `edit_sectionizer_field` tool → saved to `sectionizer_output_edits` with `edit_type='human'`; takes priority immediately |
| Manual field override (UI) | Open sectionizer detail page → click Edit on any field → type in textarea → Save | HTMX POST → `sectionizer_output_edits` with `edit_type='human'`; badge updates in place; chatbot aware on next turn |
| Manage categories | "Add a Finance category with min_score 0.3 and these rules: ..." | `upsert_sectionizer_category` tool → `sectionizer_categories` updated |
| Generate newsletter | "Generate the newsletter" | `run_newsletter_generator` Celery task → preview in chat |
| Change AI model | "Switch to Claude for refinements" | `set_active_model` tool → `ui_settings` updated, provider cache cleared |
| Review prompt history | "Show my last 5 refinements" | `get_prompt_history` tool → table in chat |

The **Review** and **History** pages are read-only. All writes go through chat tools.

---

## 3. LLM Abstraction Layer (`core/llm/`)

Single module tree for all LLM access. Zero duplication between Django apps.

### 3.1 File structure

```
core/llm/
  __init__.py           ← exports: get_provider, LLMConfig, LLMResponse, Message, ToolParam
  base.py               ← LLMProvider protocol, LLMConfig dataclass, shared types
  gemini_provider.py    ← GeminiProvider
  claude_provider.py    ← ClaudeProvider
  ollama_provider.py    ← OllamaProvider (OpenAI-compatible HTTP)
  factory.py            ← get_provider(config: LLMConfig) → LLMProvider
  memory_manager.py     ← MemoryManager
```

### 3.2 Shared types (`core/llm/base.py`)

```python
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Protocol

@dataclass
class Message:
    role: str           # 'user' | 'assistant' | 'tool_result'
    content: str
    tool_call_id: str | None = None

@dataclass
class ToolParam:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema object

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    finish_reason: str = ""      # 'stop' | 'tool_use' | 'length'

@dataclass
class LLMConfig:
    provider: str                # 'gemini' | 'claude' | 'ollama'
    model: str                   # e.g. 'gemini-2.5-flash-lite', 'claude-sonnet-4-6', 'llama3'
    api_key: str | None = None
    base_url: str | None = None  # for ollama: 'http://localhost:11434/v1'
    temperature: float = 0.0
    max_tokens: int = 4096

class LLMProvider(Protocol):
    @property
    def provider_name(self) -> str: ...
    @property
    def model_name(self) -> str: ...

    def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolParam] | None = None,
    ) -> LLMResponse: ...

    def stream(self, *, system: str, messages: list[Message]) -> Iterator[str]: ...

    async def astream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]: ...
```

`astream()` is the async variant used by the Django Channels WebSocket consumer for real-time token streaming to the browser.

### 3.3 Provider implementations

**`GeminiProvider`** — wraps `google.genai.Client`. Reads `GOOGLE_API_KEY` / `GEMINI_API_KEY` / `GENAI_API_KEY` (same resolution order as the sectionizer agent). Maps `ToolParam` to Gemini function declarations. `astream()` uses the async Gemini streaming API.

**`ClaudeProvider`** — wraps `anthropic.AsyncAnthropic` for `astream()` and `anthropic.Anthropic` for `complete()`. Reads `ANTHROPIC_API_KEY`. Always sends `anthropic-beta: prompt-caching-2024-07-31` header; mark the system prompt as `cache_control: {"type": "ephemeral"}` to cache it across turns (~70% cost reduction on repeated calls with the same long system prompt).

**`OllamaProvider`** — POST to `{base_url}/chat/completions` using the OpenAI-compatible schema. Uses `httpx.AsyncClient` for `astream()`. Attempts structured tool use; falls back to text extraction heuristic for models without native function calling (see §10.2).

### 3.4 Factory (`core/llm/factory.py`)

```python
_provider_cache: dict[str, LLMProvider] = {}

def get_provider(config: LLMConfig) -> LLMProvider:
    """Return a cached provider for the given config. Thread-safe for read-heavy use."""
    cache_key = f"{config.provider}:{config.model}:{config.base_url}"
    if cache_key not in _provider_cache:
        match config.provider:
            case "gemini":  _provider_cache[cache_key] = GeminiProvider(config)
            case "claude":  _provider_cache[cache_key] = ClaudeProvider(config)
            case "ollama":  _provider_cache[cache_key] = OllamaProvider(config)
            case _:         raise ValueError(f"Unknown provider: {config.provider}")
    return _provider_cache[cache_key]

def invalidate_provider(config: LLMConfig) -> None:
    """Clear cache entry — called by set_active_model tool after config change."""
    _provider_cache.pop(f"{config.provider}:{config.model}:{config.base_url}", None)
```

### 3.5 Multiple active configs

Three independent `LLMConfig` instances, each switchable at runtime:

| Setting key (in `ui_settings`) | Purpose | Default |
|---|---|---|
| `llm_config_chatbot` | Main chat assistant | Gemini `gemini-2.5-flash-lite` |
| `llm_config_refinement` | Sectionizer AI refinements | Same as chatbot |
| `llm_config_memory` | Session summarization + preference inference | Smallest available model |

Configs are loaded from `UiSetting.objects.get(setting_key=...)` on WebSocket `connect()` and cached in the consumer instance for the connection lifetime. On `set_active_model` tool call: update `UiSetting` in DB and call `invalidate_provider()`.

---

## 4. AI Memory Architecture

Four tiers, each served from a different source.

### 4.1 Working Memory (rebuilt every turn)

`MemoryManager.build_context(session_id)` assembles a text block injected at the top of the system prompt before every LLM call. Derived entirely from live DB queries — never stored separately.

```
## Current Pipeline State
Hoarder pending: {n} | Screener pending: {n} | Standardizer pending: {n}
Sectionizer pending: {n} | Last newsletter: {date or "never"}

## Recent Activity (this session)
{last 3 tool call outcomes from ui_chat_messages WHERE role='tool_result'}

## Human Edits (active, used with priority over AI output)
{SectionizerOutputEdit rows WHERE edit_type='human' AND accepted=1, ordered by created_at DESC, last 10}
Format: Doc {doc_id} | {section_name} | {field_name}: "{edited_value[:80]}..."

## Your Preferences
{top 5 active UiMemory rows WHERE memory_type='user_preference' AND confidence >= 0.5}
```

Pipeline status is cached in the consumer instance for 60 seconds (`self._status_cache`, `self._status_cache_ts`). Refreshed on the next turn after expiry. Human edits are fetched fresh each turn (not cached) — they change infrequently but must always reflect the latest save.

### 4.2 Episodic Memory (conversation history)

The last **30** `UiChatMessage` rows for the current session are loaded on `connect()` and maintained as `self.message_history: list[Message]` in the consumer. Each new user message and assistant response is appended to this list and persisted to DB.

When the list exceeds 30 entries, oldest entries are dropped from `self.message_history` (DB rows are kept for audit). For Claude, the system prompt uses `cache_control` so growing conversation history does not re-bill for the stable system prompt portion.

On page load (HTTP request before WebSocket upgrade): the most recent `UiChatSession` is loaded and its last 30 messages pre-rendered in the HTML so the user sees history immediately, before the WebSocket connects.

### 4.3 Semantic Memory (`ui_memory` table)

Persistent facts about the user's working style, managed by `MemoryManager`:

- **Explicit:** user says "I always use hosted screener" → `MemoryManager.save_preference('screener_backend', 'hosted', source='explicit', confidence=1.0)`
- **Inferred:** `infer_preferences_from_history()` scans recent tool calls on disconnect; writes `source='inferred'` rows if confidence ≥ 0.5
- **Session summaries:** `summarize_session()` calls `llm_config_memory` on disconnect, stores 2–3 sentence summary as `memory_type='session_summary'`

Injected into system prompt: last 5 `session_summary` rows + all active `user_preference` rows with confidence ≥ 0.5.

### 4.4 Few-Shot Refinement Memory

When calling `refine_sectionizer_output`, `MemoryManager.get_refinement_examples(section_name)` fetches the 3 most recent accepted AI refinements for that section and injects them as examples into the refinement system prompt. Query:

```python
SectionizerOutputEdit.objects.filter(
    accepted=1,
    edit_type='ai_refinement',
    section_name=section_name,
).select_related('prompt').order_by('-created_at')[:3]
```

### 4.5 Human Edit Priority

`edit_type='human'` covers both direct UI textarea edits and the `edit_sectionizer_field` chat tool. Human edits always take priority over AI refinements.

Effective value resolution (used by `get_sectionizer_output` tool and newsletter generator):

```python
def get_effective_value(sectionizer_output_id, section_name, field_name):
    """Returns (value, source) where source is 'human' | 'ai_refinement' | 'original'."""
    base_qs = SectionizerOutputEdit.objects.filter(
        sectionizer_output_id=sectionizer_output_id,
        section_name=section_name,
        field_name=field_name,
        accepted=1,
    ).order_by('-created_at')

    human = base_qs.filter(edit_type='human').first()
    if human:
        return human.edited_value, 'human'

    ai = base_qs.filter(edit_type='ai_refinement').first()
    if ai:
        return ai.edited_value, 'ai_refinement'

    output = SectionizerOutput.objects.get(pk=sectionizer_output_id)
    return output.output_json.get(section_name, {}).get(field_name), 'original'
```

Human edits are also injected into the chatbot's working memory (see §4.1) so every conversation turn is aware of them.

### 4.5 Memory Manager (`core/llm/memory_manager.py`)

```python
class MemoryManager:
    def build_context(self, session_id: str) -> str:
        """Assemble the working memory block for injection into the system prompt."""

    def get_refinement_examples(self, section_name: str) -> list[dict]:
        """Return up to 3 accepted AI refinement examples for this section."""

    def save_preference(self, key: str, value: str, *, session_id: str, source: str, confidence: float = 1.0) -> None:
        """Write or update a user_preference row in ui_memory."""

    def infer_preferences_from_history(self, session_id: str) -> None:
        """Scan tool_result messages; infer preferences; write if confidence >= 0.5."""

    async def summarize_session(self, session_id: str, provider: LLMProvider) -> str:
        """Generate and store a session summary. Called on WebSocket disconnect."""
```

---

## 5. DRY Shared Utilities

### 5.1 Prompt persistence (`core/db/prompts.py`)

One function for every LLM call across the entire project:

```python
def save_prompt(
    *,
    session_id: str | None,
    prompt_type: str,
    provider: str,
    model: str,
    system_prompt: str | None,
    user_prompt: str,
    response_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
) -> int:
    """Insert one UiPrompt row. Returns prompt_id."""
    from chat.models import UiPrompt
    from django.utils.timezone import now
    obj = UiPrompt.objects.create(
        session_id=session_id,
        prompt_type=prompt_type,
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_text=response_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        target_type=target_type,
        target_id=target_id,
        created_at=now().isoformat(),
    )
    return obj.prompt_id
```

### 5.2 Tool dispatcher (`core/tools/dispatcher.py`)

```python
TOOL_REGISTRY: dict[str, Callable[..., dict]] = {}

def register_tool(name: str):
    """Decorator: @register_tool("run_hoarder")"""
    def decorator(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return decorator

def execute_tool(tool_name: str, arguments: dict) -> dict:
    """Route a tool call. Returns a JSON-serializable dict."""
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return fn(**arguments)
    except Exception as exc:
        return {"error": str(exc)}

def all_tool_params() -> list[ToolParam]:
    """Return ToolParam definitions for all registered tools (passed to LLM)."""
```

All tools in `core/tools/pipeline_tools.py`, `core/tools/sectionizer_tools.py`, and `core/tools/memory_tools.py` self-register with `@register_tool` at import time. The consumer imports `execute_tool` and `all_tool_params` only — it never imports individual tool modules.

### 5.3 Agent runner (`core/runner.py`)

```python
def run_agent_sync(
    agent: Any,
    *,
    app_name: str,
    prompt_text: str,
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    Run one ADK agent synchronously via asyncio.run().
    on_chunk is called for each streamed text part (used by Celery task to push
    progress via channel layer back to the WebSocket consumer).
    Returns (success, full_output_text).
    """
```

### 5.4 Django template partials (replacing Streamlit components)

All reusable UI fragments live in `core/templates/partials/`. They are returned by Django views in response to HTMX requests (`HX-Request: true`) using `render(request, "partials/_name.html", context)`.

```
core/templates/partials/
  _message.html          ← single chat bubble (role, content, tool_name)
  _status_bar.html       ← pipeline stage metrics strip
  _sectionizer_card.html ← one sectionizer output with score badges and edit history
  _diff.html             ← before/after diff for AI refinements (two-column table)
  _model_selector.html   ← model config form (provider + model dropdowns)
```

---

## 6. Database Tables

All tables are Django models. Schema created and evolved via `python manage.py migrate`.  
`db/standardizer_db.py` is deprecated (renamed to `db/standardizer_db_deprecated.py`). Import from the relevant Django app:

| Table group | App | Import |
|---|---|---|
| All pipeline tables | `pipeline` | `from pipeline.models import ...` |
| Chat tables | `chat` | `from chat.models import ...` |
| Sectionizer edits | `sectionizer` | `from sectionizer.models import ...` |
| Memory + settings | `memory` | `from memory.models import ...` |

See `docs/django_orm_migration.md` §3 for complete model definitions.

### Table summary

**`pipeline` app:** `hoarder_outputs`, `hoarder_source_runs`, `hoarder_source_artifacts`, `screened_files`, `documents`, `document_screened_files`, `media_assets`, `hoarder_output_media_assets`, `sectionizer_categories`, `sectionizer_outputs`, `sectionizer_output_documents`, `newsletter_runs`, `newsletter_run_sectionizer_outputs`, `newsletter_run_configs`, `homepage_items`

**`chat` app:** `ui_chat_sessions`, `ui_chat_messages`, `ui_prompts`

**`sectionizer` app:** `sectionizer_output_edits`

**`memory` app:** `ui_memory`, `ui_settings`

### `ui_settings` schema

```sql
CREATE TABLE ui_settings (
    setting_key   TEXT PRIMARY KEY,  -- 'llm_config_chatbot' | 'llm_config_refinement' | 'llm_config_memory'
    setting_value TEXT NOT NULL,     -- JSON-serialized LLMConfig
    updated_at    TEXT NOT NULL
);
```

Loaded on WebSocket `connect()`; updated in DB on `set_active_model` tool call; provider cache invalidated immediately.

---

## 7. Tool Catalog

All tools registered via `@register_tool`. The consumer calls `all_tool_params()` to get the schema list passed to the LLM.

### 7.1 Pipeline tools (`core/tools/pipeline_tools.py`)

| Tool | Parameters | Returns |
|---|---|---|
| `get_pipeline_status` | none | `{hoarder_pending, screener_pending, standardizer_pending, sectionizer_pending, last_newsletter_run, ...}` |
| `run_hoarder` | none | `{task_id, status: "queued"}` |
| `run_screener` | `backend: "hosted"\|"ollama"` | `{task_id, status: "queued"}` |
| `run_standardizer` | none | `{task_id, status: "queued"}` |
| `run_sectionizer` | none | `{task_id, status: "queued"}` |
| `run_newsletter_generator` | none | `{task_id, status: "queued"}` |
| `run_full_pipeline` | `backend: "hosted"\|"ollama"` | `{task_ids: [...], status: "queued"}` |

Each `run_*` tool dispatches a Celery task rather than blocking. The Celery task calls `run_agent_sync()`, then pushes progress and completion back to the consumer via the channel layer (see §9.4). `save_prompt()` is called with `prompt_type='pipeline_run'` once the task completes.

### 7.2 Sectionizer tools (`core/tools/sectionizer_tools.py`)

| Tool | Parameters | Returns |
|---|---|---|
| `list_sectionizer_outputs` | `limit: int = 10, unreviewed_only: bool = False` | `[{sectionizer_output_id, doc_id, source_path, category_name, match_count, has_edits}]` |
| `get_sectionizer_output` | `sectionizer_output_id: int` | `{original: dict, effective: dict, edits: [...]}` — effective applies priority: human > ai_refinement > original; each field in edits carries `source` label |
| `refine_sectionizer_output` | `sectionizer_output_id: int, section_name: str, instruction: str` | `{proposed: dict, prompt_id: int}` — propose only, no save |
| `accept_refinement` | `sectionizer_output_id: int, section_name: str, field_name: str, edited_value: str, prompt_id: int` | `{edit_id: int}` |
| `reject_refinement` | `prompt_id: int` | `{edit_id: int}` |
| `edit_sectionizer_field` | `sectionizer_output_id: int, section_name: str, field_name: str, new_value: str, notes?: str` | `{edit_id: int}` — saves with `edit_type='human'`; takes priority over AI refinements immediately |
| `list_sectionizer_categories` | none | list of category rows |
| `upsert_sectionizer_category` | `name: str, objective?: str, min_score: float, rules: list[str]` | `{sectionizer_category_id: int, action: "created"\|"updated"}` |

`refine_sectionizer_output` calls the LLM synchronously (it is fast — single document JSON), stores the prompt and proposed output in `ui_prompts`, and returns the diff. The chatbot presents the diff and waits for user confirmation before calling `accept_refinement` or `reject_refinement`.

### 7.3 Memory and settings tools (`core/tools/memory_tools.py`)

| Tool | Parameters | Returns |
|---|---|---|
| `get_prompt_history` | `prompt_type?: str, limit: int = 10` | recent `UiPrompt` rows as dicts |
| `set_active_model` | `config_key: "chatbot"\|"refinement"\|"memory", provider: str, model: str, base_url?: str` | `{updated: bool, previous_model: str}` |
| `save_user_preference` | `key: str, value: str` | `{memory_id: int}` |
| `get_memory_summary` | none | `{preferences: [...], recent_sessions: [...]}` |

---

## 8. Application File Structure

```
sentinelpress/                ← Django project root
  manage.py
  sentinelpress/              ← Django settings package
    __init__.py
    settings.py                  ← DB, Channels, Celery, INSTALLED_APPS
    urls.py                      ← top-level URL routing
    asgi.py                      ← Channels ASGI app (WebSocket + HTTP routing)

  pipeline/                      ← pipeline table models + Django Admin
    models.py
    admin.py                     ← read-only admin for pipeline outputs; full CRUD for categories
    migrations/

  chat/                          ← chat UI
    models.py                    ← UiChatSession, UiChatMessage, UiPrompt
    consumers.py                 ← ChatConsumer (AsyncWebsocketConsumer)
    views.py                     ← HTTP: render chat page; HTMX: status bar partial
    admin.py
    urls.py
    templates/chat/
      chat.html                  ← base chat page (loads history, opens WebSocket)
      _message.html              ← HTMX partial: single chat bubble
      _status_bar.html           ← HTMX partial: pipeline status strip (polled every 60 s)
    migrations/

  sectionizer/                   ← sectionizer review + editing
    models.py                    ← SectionizerOutputEdit
    admin.py
    views.py                     ← list view, detail view, HTMX diff partial
    urls.py
    templates/sectionizer/
      list.html
      detail.html
      _diff.html                 ← HTMX partial: before/after refinement diff
      _card.html                 ← HTMX partial: single output card
    migrations/

  memory/                        ← AI memory + settings
    models.py                    ← UiMemory, UiSetting
    admin.py
    views.py                     ← settings page (model config form)
    urls.py
    templates/memory/
      settings.html
    migrations/

  core/                          ← shared business logic, no Django models
    __init__.py
    runner.py                    ← run_agent_sync()
    tasks.py                     ← Celery tasks: run_hoarder_task, run_screener_task, etc.
    llm/
      __init__.py
      base.py
      gemini_provider.py
      claude_provider.py
      ollama_provider.py
      factory.py
      memory_manager.py
    tools/
      __init__.py
      dispatcher.py
      pipeline_tools.py
      sectionizer_tools.py
      memory_tools.py
    db/
      prompts.py                 ← save_prompt()
    templates/partials/          ← reusable HTML partials used by multiple apps
      _message.html
      _status_bar.html
      _sectionizer_card.html
      _diff.html
      _model_selector.html
```

**Rule:** `chat/`, `sectionizer/`, `memory/` views and consumers import from `core/` only. They never duplicate LLM calls, tool dispatch, or prompt-save logic.

---

## 9. Chat Implementation (`chat/consumers.py`)

### 9.1 Layout (rendered by `chat/views.py` + `chat.html`)

```
┌─ Nav sidebar ──────────────┐  ┌─ Main area ─────────────────────────────────┐
│  [_status_bar.html]        │  │  [Message history — pre-rendered server-side]│
│  (HTMX poll every 60s)     │  │    You: "Run the screener"                   │
│  ────────────────────────  │  │    Bot: Running screener (hosted)...         │
│  Chat          (active)    │  │    [tool_result card: 8 items screened]      │
│  Review                    │  │    Bot: Done! 3 selected. Standardizer has   │
│  History                   │  │    3 docs pending.                           │
│  Settings                  │  │                                              │
│  ────────────────────────  │  │  [Text input + Send button]                  │
│  [_model_selector.html]    │  └──────────────────────────────────────────────┘
└────────────────────────────┘
```

The status bar uses `hx-get="/chat/status-bar/" hx-trigger="every 60s" hx-swap="outerHTML"` — HTMX polling, no WebSocket needed for status updates.

### 9.2 WebSocket connection lifecycle

```python
class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # 1. Resolve or create UiChatSession
        self.session_id = self.scope["session"].get("chat_session_id") or str(uuid.uuid4())
        self.scope["session"]["chat_session_id"] = self.session_id
        await database_sync_to_async(self._ensure_session)()

        # 2. Load LLM configs from UiSetting
        self.llm_configs = await database_sync_to_async(self._load_llm_configs)()

        # 3. Load last 30 messages for episodic memory
        self.message_history = await database_sync_to_async(self._load_message_history)()

        # 4. Join personal channel group for Celery task push-backs
        await self.channel_layer.group_add(f"user_{self.session_id}", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # Summarize session and infer preferences asynchronously
        provider = get_provider(self.llm_configs["memory"])
        memory = MemoryManager()
        await memory.summarize_session(self.session_id, provider)
        await database_sync_to_async(memory.infer_preferences_from_history)(self.session_id)
        await self.channel_layer.group_discard(f"user_{self.session_id}", self.channel_name)
```

### 9.3 Receive loop (per user message)

```python
    async def receive(self, text_data):
        payload = json.loads(text_data)
        user_text = payload["message"]

        # Persist user message
        await database_sync_to_async(self._save_message)("user", user_text)
        self.message_history.append(Message(role="user", content=user_text))

        # Persist prompt record (partial — response filled in after)
        memory = MemoryManager()
        system_prompt = CHATBOT_BASE_SYSTEM + "\n\n" + await database_sync_to_async(memory.build_context)(self.session_id)
        prompt_id = await database_sync_to_async(save_prompt)(
            session_id=self.session_id,
            prompt_type="chatbot_turn",
            provider=self.llm_configs["chatbot"].provider,
            model=self.llm_configs["chatbot"].model,
            system_prompt=system_prompt,
            user_prompt=user_text,
        )

        # Call LLM (may return tool calls)
        provider = get_provider(self.llm_configs["chatbot"])
        response = await asyncio.to_thread(
            provider.complete,
            system=system_prompt,
            messages=self.message_history[-30:],
            tools=all_tool_params(),
        )

        # Handle tool calls
        while response.tool_calls:
            for tool_call in response.tool_calls:
                result = await asyncio.to_thread(execute_tool, tool_call.name, tool_call.arguments)
                result_text = json.dumps(result)
                await database_sync_to_async(self._save_message)("tool_result", result_text, tool_name=tool_call.name)
                self.message_history.append(Message(role="tool_result", content=result_text, tool_call_id=tool_call.id))
                # Push tool result card to browser
                await self.send(json.dumps({"type": "tool_result", "tool": tool_call.name, "result": result}))

            response = await asyncio.to_thread(
                provider.complete,
                system=system_prompt,
                messages=self.message_history[-30:],
            )

        # Stream final text response token-by-token
        full_response = ""
        await self.send(json.dumps({"type": "stream_start"}))
        async for chunk in provider.astream(system=system_prompt, messages=self.message_history[-30:]):
            full_response += chunk
            await self.send(json.dumps({"type": "chunk", "text": chunk}))
        await self.send(json.dumps({"type": "stream_end"}))

        # Persist response
        await database_sync_to_async(self._save_message)("assistant", full_response)
        self.message_history.append(Message(role="assistant", content=full_response))
        await database_sync_to_async(UiPrompt.objects.filter(prompt_id=prompt_id).update)(
            response_text=full_response,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
```

### 9.4 Celery task push-back (for pipeline runs)

When a `run_*` tool dispatches a Celery task, the task posts progress back to the consumer via the channel layer:

```python
# core/tasks.py
from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

@shared_task
def run_screener_task(session_id: str, backend: str):
    channel_layer = get_channel_layer()
    push = lambda msg: async_to_sync(channel_layer.group_send)(
        f"user_{session_id}", {"type": "task.update", "message": msg}
    )
    push(f"Screener ({backend}) started...")
    success, output = run_agent_sync(agent, app_name="screener", prompt_text="Screen files")
    save_prompt(prompt_type="pipeline_run", ...)
    push(f"Screener complete. {output[:200]}")
```

```python
# chat/consumers.py — handler for channel layer messages
    async def task_update(self, event):
        await self.send(json.dumps({"type": "task_update", "text": event["message"]}))
```

The browser appends task updates to the current assistant message bubble until `stream_end` is received.

### 9.5 Model switching at runtime

```
User: "Switch to Claude for refinements"
  → LLM calls set_active_model(config_key="refinement", provider="claude", model="claude-sonnet-4-6")
  → UiSetting.objects.update_or_create(setting_key="llm_config_refinement", ...)
  → invalidate_provider(old_config)
  → self.llm_configs["refinement"] updated in consumer instance
  → Bot: "Done. Refinements will now use Claude claude-sonnet-4-6."
```

The ADK pipeline agents (hoarder, screener, etc.) read their model from their own env vars and are unaffected.

---

## 10. Model Support Details

### 10.1 Supported models by provider

| Provider | Suggested models | Notes |
|---|---|---|
| Gemini | `gemini-2.5-flash-lite` (default), `gemini-2.5-flash`, `gemini-2.5-pro` | Native tool use. ADK pipeline agents use Gemini via their own env vars — this only affects chatbot + refinements. |
| Claude | `claude-haiku-4-5-20251001` (cheapest), `claude-sonnet-4-6`, `claude-opus-4-7` | Enable prompt caching on system prompt. Native tool use. `AsyncAnthropic` client for streaming. |
| Ollama (local) | `llama3.1`, `mistral-nemo`, `qwen2.5`, `phi4` | Free, offline. Tool use varies by model — see §10.2. |

### 10.2 Tool use fallback for local models

`OllamaProvider.complete()` catches the API error when a model rejects the tool schema and retries with an instruction-based prompt:

```
Respond with JSON only:
{"tool_name": "<name>", "arguments": {...}}
or plain text if no tool is needed.
```

`_extract_tool_call_from_text()` parses the response. This fallback is completely internal to the provider — `execute_tool()` always receives a `ToolCall` object regardless.

---

## 11. Sectionizer Review Panel (`sectionizer/` Django app)

Read-only for pipeline data. Direct human editing is available on the detail view — no need to go through chat.

### List view (`sectionizer/views.py` → `sectionizer/list.html`)

Queryset: `SectionizerOutput.objects.select_related('doc', 'category').annotate(has_edits=Exists(SectionizerOutputEdit.objects.filter(sectionizer_output=OuterRef('pk'))))`. Rendered with `django-tables2`. Filterable by category, match count, date range, has_edits.

Columns: Doc ID, source path (truncated), primary category, match count, has edits badge, run timestamp, "Open Detail" link, "Refine in Chat" link.

"Refine in Chat" links to `/chat/?prefill=Refine+the+{section}+section+for+doc+{id}` which pre-fills the chat input.

### Detail view (`sectionizer/views.py` → `sectionizer/detail.html`)

Renders:
- Document info (source_path, author, document_summary from output_json) — read-only
- Section evaluations: score badges, per-rule scores
- For each editable field (`newsletter_title`, `summary`, `summary_facts`):
  - Displays effective value (human-edited if present, else AI refinement, else original) with a source badge (`Human` / `AI` / `Original`)
  - **"Edit" button** copies the effective value into a textarea in-place
  - Textarea is pre-populated; user edits freely
  - **"Save"** button sends HTMX POST to `/sectionizer/{output_id}/edit/`
  - **"Cancel"** button restores the display without saving
- Edit history table: `SectionizerOutputEdit` rows for this output, newest first
- "Refine in Chat" button for each section (AI-assisted rewrite via chat)

### Direct edit endpoint

```python
# POST /sectionizer/<int:output_id>/edit/
# Body: section_name, field_name, edited_value, editor_notes (optional)
# Creates SectionizerOutputEdit(edit_type='human', accepted=1)
# Returns HTMX partial: updated field display with 'Human' badge + updated edit history row
```

The save endpoint never touches `sectionizer_outputs`. It always inserts a new `sectionizer_output_edits` row. The view re-queries `get_effective_value()` and returns the updated field partial via HTMX `hx-swap="outerHTML"`.

HTMX attributes on the edit form:
```html
<form hx-post="/sectionizer/{{ output.pk }}/edit/"
      hx-target="#field-{{ section_name }}-{{ field_name }}"
      hx-swap="outerHTML"
      hx-include="[name='editor_notes']">
```

---

## 12. Django Admin Configuration

Pipeline outputs are read-only in admin (agents write them). Categories and UI tables are fully editable.

```python
# pipeline/admin.py
@admin.register(SectionizerCategory)      # full CRUD — editorial control
@admin.register(SectionizerOutput)        # read-only — has_add/change/delete = False
@admin.register(Document)                 # read-only
@admin.register(NewsletterRun)            # read-only

# chat/admin.py
@admin.register(UiPrompt)                 # read-only for audit; search by prompt text
@admin.register(UiChatSession)            # read-only

# memory/admin.py
@admin.register(UiMemory)                 # full CRUD — manage preferences manually
@admin.register(UiSetting)               # full CRUD — change model config from admin
```

---

## 13. Environment Variables

```
# LLM providers
GOOGLE_API_KEY        or GEMINI_API_KEY or GENAI_API_KEY  ← Gemini (chatbot + pipeline agents)
ANTHROPIC_API_KEY                                          ← Claude (optional)
OLLAMA_BASE_URL       default: http://localhost:11434/v1   ← local LLM (optional)
OLLAMA_MODEL          default: llama3.1

# Chatbot model defaults (overridable at runtime via set_active_model tool)
CHATBOT_LLM_PROVIDER  default: gemini   values: gemini | claude | ollama
CHATBOT_MODEL         default: gemini-2.5-flash-lite
REFINEMENT_LLM_PROVIDER  default: same as CHATBOT_LLM_PROVIDER
REFINEMENT_MODEL         default: same as CHATBOT_MODEL
MEMORY_LLM_PROVIDER   default: gemini
MEMORY_MODEL          default: gemini-2.5-flash-lite

# Pipeline agent models (unchanged — read by existing agents via their own env vars)
SECTIONIZER_GEMINI_MODEL  default: gemini-2.5-flash-lite
SCREENER_BACKEND          default: hosted   values: hosted | ollama

# Django + Celery + Database
DJANGO_SECRET_KEY         ← required in production
DJANGO_DEBUG              default: True
DATABASE_URL              postgresql://user:pass@localhost:5432/newsletter  (or set individual vars below)
DB_HOST                   default: localhost
DB_PORT                   default: 5432
DB_NAME                   default: newsletter
DB_USER                   default: postgres
DB_PASSWORD               ← required
CELERY_BROKER_URL         default: redis://localhost:6379/0
REDIS_URL                 default: redis://localhost:6379/0  (channel layer)

# UI behaviour
CHATBOT_MESSAGE_WINDOW       default: 30  ← messages passed to LLM per turn
CHATBOT_REFINEMENT_EXAMPLES  default: 3   ← few-shot examples per refinement call
STATUS_CACHE_SECONDS         default: 60  ← pipeline status cache TTL in consumer
```

---

## 14. Implementation Phases (chat-first throughout)

### Phase 0 — ORM Migration (prerequisite)

- Create `sentinelpress/` Django project; configure `settings.py` pointing at existing `data/standardizer.db`
- Define all Django models (`pipeline/models.py`, etc.) per `docs/django_orm_migration.md` §3
- Run `makemigrations` + `migrate`; verify existing data intact
- Update all agents: swap `from db.standardizer_db import ...` → `from pipeline.models import ...`; replace `session_scope()` with Django queryset patterns; add `django.setup()` to `run_*.py`
- Rename `db/standardizer_db.py` → `db/standardizer_db_deprecated.py`; remove `sqlalchemy` from `requirements.txt`
- End-to-end pipeline regression test

**Deliverable:** Pipeline runs on Django ORM. No UI yet.

### Phase 1 — Core Infrastructure

- Migrate new UI table models + migrations (`chat/`, `sectionizer/`, `memory/`)
- `core/llm/` — `base.py`, `gemini_provider.py`, `factory.py` (Gemini only)
- `core/runner.py` — `run_agent_sync()`
- `core/tools/dispatcher.py` — `register_tool`, `execute_tool`, `all_tool_params`
- `core/tools/pipeline_tools.py` — all `run_*` + `get_pipeline_status` (Celery dispatch stubs)
- `core/db/prompts.py` — `save_prompt()`
- `core/tasks.py` — Celery task shells (hoarder, screener, standardizer, sectionizer, newsletter)

**Deliverable:** All tools and tasks testable from Python shell. No views yet.

### Phase 2 — Chat with Pipeline Control

- `core/llm/memory_manager.py` — `MemoryManager.build_context()` (working memory only)
- `chat/consumers.py` — `ChatConsumer` with connect, receive, disconnect, task_update
- `chat/views.py` — chat page HTTP view + `_status_bar.html` HTMX endpoint
- `chat/templates/` — `chat.html`, `_message.html`, `_status_bar.html`
- `sentinelpress/asgi.py` — Channels routing (WebSocket `/ws/chat/` + HTTP)
- `sentinelpress/urls.py` — URL wiring
- Full Celery task implementations with channel layer push-back

**Deliverable:** Working chat. User can ask pipeline questions and trigger agents. Progress streams in real time. All prompts stored.

### Phase 3 — Multi-Model + Memory

- `core/llm/claude_provider.py`, `core/llm/ollama_provider.py`
- `core/tools/memory_tools.py` — `set_active_model`, `save_user_preference`, `get_memory_summary`
- `MemoryManager.infer_preferences_from_history()`, `summarize_session()`
- `memory/views.py` + `settings.html` — model config form (also editable from chat)
- `core/templates/partials/_model_selector.html`

**Deliverable:** Model switching from chat. Session summaries stored on disconnect. Preferences accumulate across sessions.

### Phase 4 — Sectionizer Editing via Chat

- `core/tools/sectionizer_tools.py` — all sectionizer tools with few-shot memory
- `MemoryManager.get_refinement_examples()` — few-shot injection
- `refine_sectionizer_output` two-step propose/confirm flow in consumer
- `core/templates/partials/_diff.html`, `_sectionizer_card.html`

**Deliverable:** Sectionizer review and editing entirely through chat. Each accepted refinement improves future suggestions.

### Phase 5 — Review and History Panels

- `sectionizer/views.py` — list + detail views using `django-tables2`
- `sectionizer/templates/` — list, detail, `_diff.html`, `_card.html`
- History: `UiPrompt` admin list view is sufficient (no separate app needed)
- "Refine in Chat" prefill links from review panel to chat

**Deliverable:** Complete application. All four navigation sections functional.

---

## 15. AI Memory Needs — Summary

| Need | Tier | Source | Injected where |
|---|---|---|---|
| "What is the pipeline's current state?" | Working memory | Live DB query (60 s cache in consumer) | System prompt, every turn |
| "What did I just do this session?" | Episodic memory | `self.message_history` (last 30) | LLM `messages` list |
| "What does the user prefer?" | Semantic memory | `UiMemory` user_preference rows | System prompt |
| "What happened in prior sessions?" | Semantic memory | `UiMemory` session_summary rows (last 5) | System prompt |
| "How should I write Engineering summaries?" | Few-shot memory | Accepted `SectionizerOutputEdit` AI edits | Refinement system prompt |
| "What has the user manually corrected?" | Working memory | `SectionizerOutputEdit` human edits (last 10) | System prompt, every turn |
| "What pipeline patterns does the user follow?" | Semantic memory | Inferred from tool_result messages on disconnect | System prompt (confidence ≥ 0.5 only) |

**Context window budget:** Keep the injected memory block under ~500 tokens. Session summaries: 2–3 sentences each. If context grows too large (Ollama local models may have 8K–32K limits), truncate the memory block first — raw conversation history takes priority.

---

## 16. Constraints and Decisions

- **Full Django ORM.** SQLAlchemy retired. See `docs/django_orm_migration.md`.
- **ADK pipeline agents always use Gemini** via their own env vars. `LLMProvider` is for the UI layer only.
- **Pipeline agents run as Celery tasks.** The WebSocket consumer is never blocked. Progress is pushed back via the channel layer.
- **`asyncio.to_thread()`** wraps all synchronous LLM `complete()` and tool calls inside the async consumer. `astream()` is used for token streaming.
- **`database_sync_to_async()`** wraps all Django ORM calls inside the async consumer.
- **Sectionizer edits are non-destructive.** `sectionizer_outputs` rows are never updated. Effective value = latest accepted human edit (`edit_type='human'`) → latest accepted AI refinement → original `output_json`.
- **Human edits take absolute priority.** Both the in-page textarea save and the `edit_sectionizer_field` chat tool write `edit_type='human'`. These override AI refinements in effective value resolution and are injected into the chatbot's working memory every turn.
- **Two-step confirm for AI refinements.** `refine_sectionizer_output` proposes; `accept_refinement` / `reject_refinement` commits. The LLM cannot auto-accept.
- **`db/standardizer_db.py` is renamed to `db/standardizer_db_deprecated.py`**, not deleted. Do not import from it in new code.
- **`django.setup()` required** in `run_*.py` entry points before any model imports.
- **Backup files** (`config copy.json`, `prompt_template copy*.md`, `config1.json333`) are ignored.
