# SQLAlchemy → Django ORM Migration Guide

**Created:** 2026-05-07  
**Decision:** User approved full ORM migration. Django ORM replaces SQLAlchemy across all pipeline agents and the new chatbot UI. `db/standardizer_db.py` is deprecated — renamed to `db/standardizer_db_deprecated.py` and kept for reference; do not import from it in new code.

---

## 1. What Changes, What Stays

| Component | Change |
|---|---|
| `db/standardizer_db.py` | Renamed to `db/standardizer_db_deprecated.py`. Kept for reference; do not import from it in new code. |
| `ensure_standardizer_schema()` | Replaced by `python manage.py migrate`. |
| `_ensure_column()` | Replaced by Django `AlterField` / `AddField` migrations. |
| `_seed_sectionizer_categories()` | Replaced by a Django data migration. |
| `_seed_homepage_items()` | Replaced by a Django data migration. |
| `session_scope()` | Replaced by `django.db.transaction.atomic()` or plain queryset calls. |
| SQLAlchemy ORM classes (`SectionizerOutput`, etc.) | Replaced by Django model classes with same names. |
| `from db.standardizer_db import ...` in agents | Replaced by `from pipeline.models import ...` (Django app). |
| Agent logic (queries, inserts, updates) | Rewritten using Django queryset API. See §4. |
| Pipeline agent entry points (`run_*.py`) | Require `django.setup()` call before any model imports. |
| `requirements.txt` | Remove `sqlalchemy`, `alembic` (if present). Add `django`, `channels`, etc. |

---

## 2. Django Project Layout

```
newsletter_adk/                  ← existing pipeline root (agents/ stays here)
  agents/                        ← pipeline agents (modified to use Django ORM)
  .env
  run_pipeline.py                ← updated: calls django.setup() first
  run_hoarder.py                 ← updated: calls django.setup() first
  ... other run_*.py             ← updated: calls django.setup() first

sentinelpress/                ← Django project root (new directory, sibling to newsletter_adk)
  manage.py
  sentinelpress/              ← Django settings package
    __init__.py
    settings.py
    urls.py
    asgi.py                      ← Channels ASGI routing
  
  pipeline/                      ← Django app: all pipeline tables
    __init__.py
    apps.py
    models.py                    ← all pipeline ORM models (§3.1)
    admin.py                     ← Django Admin views for pipeline tables
    migrations/
      0001_initial.py            ← creates all pipeline tables
      0002_seed_categories.py    ← data migration: seeds sectionizer_categories
  
  chat/                          ← Django app: chat UI + WebSocket
    __init__.py
    apps.py
    models.py                    ← UiChatSession, UiChatMessage, UiPrompt
    consumers.py                 ← Django Channels WebSocket consumer
    views.py
    admin.py
    urls.py
    templates/chat/
    migrations/
  
  sectionizer/                   ← Django app: sectionizer editing
    __init__.py
    apps.py
    models.py                    ← SectionizerOutputEdit
    admin.py
    views.py
    urls.py
    templates/sectionizer/
    migrations/
  
  memory/                        ← Django app: AI memory + settings
    __init__.py
    apps.py
    models.py                    ← UiMemory, UiSetting
    admin.py
    migrations/
  
  core/                          ← No models; shared business logic
    __init__.py
    llm/                         ← LLMProvider abstraction (from spec v2)
    tools/                       ← Tool dispatcher + implementations
    runner.py                    ← run_agent_sync() ADK wrapper
    memory_manager.py            ← MemoryManager
```

### Django settings (key entries)

```python
# sentinelpress/sentinelpress/settings.py
import os

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "newsletter"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

# Optional: override the whole thing with DATABASE_URL (e.g. for Heroku/Railway)
# import dj_database_url
# DATABASES["default"] = dj_database_url.config(conn_max_age=60)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "pipeline",
    "chat",
    "sectionizer",
    "memory",
]

ASGI_APPLICATION = "sentinelpress.asgi.application"

# In-memory channel layer for development; swap for Redis in production
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
```

### `django.setup()` in pipeline entry points

Any script that imports Django models must call `django.setup()` before the first import. Add to the top of every `run_*.py` and `agent.py`:

```python
import django
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sentinelpress.settings")
django.setup()
```

---

## 3. Django Model Definitions

All models go in `pipeline/models.py` unless noted.

### 3.1 Pipeline models (`pipeline/models.py`)

```python
from django.db import models

class HoarderOutput(models.Model):
    hoarder_output_id = models.AutoField(primary_key=True)
    source_id = models.TextField(null=True, blank=True)
    source_path = models.TextField()
    name = models.TextField(null=True, blank=True)
    source_created_at = models.TextField(null=True, blank=True)
    source_modified_at = models.TextField(null=True, blank=True)
    source_payload_json = models.JSONField()
    created_at = models.TextField()
    screened_at = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hoarder_outputs"


class HoarderSourceRun(models.Model):
    hoarder_source_run_id = models.AutoField(primary_key=True)
    source_id = models.TextField()
    source_path = models.TextField(null=True, blank=True)
    status = models.TextField()
    item_count = models.IntegerField(null=True, blank=True)
    error_text = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "hoarder_source_runs"


class HoarderSourceArtifact(models.Model):
    hoarder_source_artifact_id = models.AutoField(primary_key=True)
    source_id = models.TextField()
    artifact_path = models.TextField()
    status = models.TextField()
    error_text = models.TextField(null=True, blank=True)
    processed_at = models.TextField()

    class Meta:
        db_table = "hoarder_source_artifacts"


class ScreenedFile(models.Model):
    screened_file_id = models.AutoField(primary_key=True)
    source_path = models.TextField()
    name = models.TextField(null=True, blank=True)
    source_created_at = models.TextField(null=True, blank=True)
    source_modified_at = models.TextField(null=True, blank=True)
    is_selected = models.IntegerField()
    rejection_reason = models.TextField(null=True, blank=True)
    source_payload_json = models.JSONField()
    created_at = models.TextField()
    processed_at = models.TextField(null=True, blank=True)
    hoarder_output = models.ForeignKey(
        HoarderOutput, null=True, blank=True,
        on_delete=models.SET_NULL, db_column="hoarder_output_id",
    )

    class Meta:
        db_table = "screened_files"


class Document(models.Model):
    doc_id = models.AutoField(primary_key=True)
    source_path = models.TextField()
    author = models.TextField(null=True, blank=True)
    text_content = models.TextField(null=True, blank=True)
    metadata_json = models.JSONField()
    extraction_status = models.TextField()
    extraction_error = models.TextField(null=True, blank=True)
    content_sha256 = models.TextField(null=True, blank=True)
    modified_at = models.TextField(null=True, blank=True)
    created_at = models.TextField(null=True, blank=True)
    persisted_at = models.TextField()

    class Meta:
        db_table = "documents"


class DocumentScreenedFile(models.Model):
    document_screened_file_id = models.AutoField(primary_key=True)
    doc = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="doc_id")
    screened_file = models.ForeignKey(ScreenedFile, on_delete=models.CASCADE, db_column="screened_file_id")
    created_at = models.TextField()

    class Meta:
        db_table = "document_screened_files"


class MediaAsset(models.Model):
    media_id = models.AutoField(primary_key=True)
    doc = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="doc_id")
    artifact_path = models.TextField()
    mime_type = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "media_assets"


class HoarderOutputMediaAsset(models.Model):
    hoarder_output_media_asset_id = models.AutoField(primary_key=True)
    hoarder_output = models.ForeignKey(HoarderOutput, on_delete=models.CASCADE, db_column="hoarder_output_id")
    media = models.ForeignKey(MediaAsset, on_delete=models.CASCADE, db_column="media_id")
    created_at = models.TextField()

    class Meta:
        db_table = "hoarder_output_media_assets"


class SectionizerCategory(models.Model):
    sectionizer_category_id = models.AutoField(primary_key=True)
    name = models.TextField(unique=True)
    objective = models.TextField(null=True, blank=True)
    min_score = models.FloatField()
    rules = models.JSONField(default=list)   # list of rule strings
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "sectionizer_categories"


HOMEPAGE_SLOT_CHOICES = [("headline", "Headline"), ("latest", "Latest")]


class SectionizerOutput(models.Model):
    sectionizer_output_id = models.AutoField(primary_key=True)
    doc = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="doc_id")
    category = models.ForeignKey(
        SectionizerCategory, null=True, blank=True,
        on_delete=models.SET_NULL, db_column="category_id",
    )
    homepage_slot = models.TextField(null=True, blank=True, choices=HOMEPAGE_SLOT_CHOICES)
    homepage_position = models.IntegerField(null=True, blank=True)
    homepage_active = models.IntegerField(null=True, blank=True)
    output_path = models.TextField()
    source_path = models.TextField(null=True, blank=True)
    llm_instruction = models.TextField(null=True, blank=True)
    llm_content = models.TextField(null=True, blank=True)
    output_json = models.JSONField(null=True, blank=True)
    match_count = models.IntegerField(null=True, blank=True)
    run_timestamp = models.TextField()
    created_at = models.TextField()

    class Meta:
        db_table = "sectionizer_outputs"


class SectionizerOutputDocument(models.Model):
    sectionizer_output_document_id = models.AutoField(primary_key=True)
    sectionizer_output = models.ForeignKey(SectionizerOutput, on_delete=models.CASCADE, db_column="sectionizer_output_id")
    doc = models.ForeignKey(Document, on_delete=models.CASCADE, db_column="doc_id")
    created_at = models.TextField()

    class Meta:
        db_table = "sectionizer_output_documents"


class NewsletterRun(models.Model):
    newsletter_run_id = models.AutoField(primary_key=True)
    run_timestamp = models.TextField()
    llm_instruction = models.TextField(null=True, blank=True)
    llm_content = models.TextField(null=True, blank=True)
    output_markdown = models.TextField()
    output_json = models.JSONField()
    output_html = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "newsletter_runs"


class NewsletterRunSectionizerOutput(models.Model):
    newsletter_run_sectionizer_output_id = models.AutoField(primary_key=True)
    newsletter_run = models.ForeignKey(NewsletterRun, on_delete=models.CASCADE, db_column="newsletter_run_id")
    sectionizer_output = models.ForeignKey(SectionizerOutput, on_delete=models.CASCADE, db_column="sectionizer_output_id")
    created_at = models.TextField()

    class Meta:
        db_table = "newsletter_run_sectionizer_outputs"


class NewsletterRunConfig(models.Model):
    newsletter_run = models.OneToOneField(
        NewsletterRun, primary_key=True, on_delete=models.CASCADE, db_column="newsletter_run_id",
    )
    newsletter_date = models.TextField(null=True, blank=True)
    config_json = models.JSONField()
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "newsletter_run_configs"


class HomepageItem(models.Model):
    homepage_item_id = models.AutoField(primary_key=True)
    source_type = models.TextField()
    source_id = models.TextField()
    slot = models.TextField(choices=HOMEPAGE_SLOT_CHOICES)
    position = models.IntegerField()
    is_active = models.IntegerField()
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "homepage_items"
```

### 3.2 Chat models (`chat/models.py`)

```python
from django.db import models

class UiChatSession(models.Model):
    session_id = models.TextField(primary_key=True)   # UUID string
    title = models.TextField(null=True, blank=True)
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "ui_chat_sessions"
        ordering = ["-created_at"]


class UiChatMessage(models.Model):
    message_id = models.AutoField(primary_key=True)
    session = models.ForeignKey(UiChatSession, on_delete=models.CASCADE, db_column="session_id", related_name="messages")
    role = models.TextField()           # 'user' | 'assistant' | 'tool_result'
    content = models.TextField()
    tool_name = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "ui_chat_messages"
        ordering = ["created_at"]


PROMPT_TYPE_CHOICES = [
    ("chatbot_turn", "Chatbot Turn"),
    ("chatbot_tool_call", "Chatbot Tool Call"),
    ("pipeline_run", "Pipeline Run"),
    ("sectionizer_ai_refinement", "Sectionizer AI Refinement"),
    ("sectionizer_system", "Sectionizer System Prompt"),
]


class UiPrompt(models.Model):
    prompt_id = models.AutoField(primary_key=True)
    session = models.ForeignKey(
        UiChatSession, null=True, blank=True,
        on_delete=models.SET_NULL, db_column="session_id",
    )
    prompt_type = models.TextField(choices=PROMPT_TYPE_CHOICES)
    target_type = models.TextField(null=True, blank=True)
    target_id = models.IntegerField(null=True, blank=True)
    provider = models.TextField()       # 'gemini' | 'claude' | 'ollama'
    model = models.TextField()
    system_prompt = models.TextField(null=True, blank=True)
    user_prompt = models.TextField()
    response_text = models.TextField(null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "ui_prompts"
        ordering = ["-created_at"]
```

### 3.3 Sectionizer editing models (`sectionizer/models.py`)

```python
from django.db import models
from pipeline.models import SectionizerOutput
from chat.models import UiPrompt

FIELD_NAME_CHOICES = [
    ("newsletter_title", "Newsletter Title"),
    ("summary", "Summary"),
    ("summary_facts", "Summary Facts"),
    ("passes_threshold", "Passes Threshold"),
    ("full_json", "Full JSON"),
]

class SectionizerOutputEdit(models.Model):
    edit_id = models.AutoField(primary_key=True)
    sectionizer_output = models.ForeignKey(
        SectionizerOutput, on_delete=models.CASCADE,
        db_column="sectionizer_output_id", related_name="edits",
    )
    edit_type = models.TextField()      # 'human' | 'ai_refinement'
    prompt = models.ForeignKey(
        UiPrompt, null=True, blank=True,
        on_delete=models.SET_NULL, db_column="prompt_id",
    )
    section_name = models.TextField(null=True, blank=True)
    field_name = models.TextField(null=True, blank=True, choices=FIELD_NAME_CHOICES)
    original_value = models.TextField(null=True, blank=True)
    edited_value = models.TextField()
    editor_notes = models.TextField(null=True, blank=True)
    accepted = models.IntegerField(default=1)   # 1=accepted, 0=rejected
    created_at = models.TextField()

    class Meta:
        db_table = "sectionizer_output_edits"
        ordering = ["-created_at"]
```

### 3.4 Memory models (`memory/models.py`)

```python
from django.db import models

MEMORY_TYPE_CHOICES = [
    ("user_preference", "User Preference"),
    ("session_summary", "Session Summary"),
    ("pipeline_pattern", "Pipeline Pattern"),
]

class UiMemory(models.Model):
    memory_id = models.AutoField(primary_key=True)
    memory_type = models.TextField(choices=MEMORY_TYPE_CHOICES)
    key = models.TextField(null=True, blank=True)
    value = models.TextField()
    source = models.TextField()         # 'explicit' | 'inferred' | 'system'
    session_id = models.TextField(null=True, blank=True)
    confidence = models.FloatField(default=1.0)
    is_active = models.IntegerField(default=1)
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "ui_memory"
        ordering = ["-created_at"]


class UiSetting(models.Model):
    setting_key = models.TextField(primary_key=True)
    setting_value = models.TextField()  # JSON-serialized
    updated_at = models.TextField()

    class Meta:
        db_table = "ui_settings"
```

---

## 4. SQLAlchemy → Django Pattern Mapping

### 4.1 Session and transactions

| SQLAlchemy | Django equivalent |
|---|---|
| `with session_scope() as session:` | `from django.db import transaction` then `with transaction.atomic():` |
| `session.add(record)` | `record.save()` or `Model.objects.create(...)` |
| `session.flush()` | `record.save()` (Django saves immediately, auto-generates PK) |
| `session.query(Model).all()` | `Model.objects.all()` |
| `session.query(Model).filter_by(x=1).all()` | `Model.objects.filter(x=1)` |
| `session.query(Model).order_by(Model.col.desc()).all()` | `Model.objects.order_by('-col')` |
| `session.query(Model).count()` | `Model.objects.count()` |
| `session.execute(text("SELECT ...")).all()` | `from django.db import connection; connection.cursor().execute(...)` |
| `session.rollback()` | Automatic on exception inside `transaction.atomic()` |

### 4.2 Insert patterns

**SQLAlchemy:**
```python
record = SectionizerOutput(
    doc_id=row.get("doc_id"),
    category_id=category_id,
    output_json=json.dumps(payload),
    run_timestamp=RUN_TIMESTAMP,
    created_at=created_at,
)
session.add(record)
session.flush()
sectionizer_output_id = int(record.sectionizer_output_id)
```

**Django:**
```python
record = SectionizerOutput.objects.create(
    doc_id=row.get("doc_id"),
    category_id=category_id,
    output_json=payload,          # JSONField — pass dict/list directly, no json.dumps()
    run_timestamp=RUN_TIMESTAMP,
    created_at=created_at,
)
sectionizer_output_id = record.sectionizer_output_id   # populated immediately after create()
```

### 4.3 Existence checks (used for deduplication)

**SQLAlchemy:**
```python
not_exists = ~session.query(SectionizerOutput).filter(
    SectionizerOutput.doc_id == d.doc_id
).exists()
```

**Django:**
```python
not SectionizerOutput.objects.filter(doc_id=doc.doc_id).exists()
```

### 4.4 Complex CTEs (sectionizer `_load_rows_from_db`)

The sectionizer uses a window-function CTE. Django's ORM does not support window functions in `WHERE` clauses directly. Use raw SQL via `connection.cursor()`:

```python
from django.db import connection

def load_unsectionized_documents() -> list[dict]:
    sql = """
        WITH ranked AS (
          SELECT d.doc_id, d.source_path, d.author, d.text_content,
                 d.metadata_json, d.extraction_status, d.extraction_error,
                 d.content_sha256, d.modified_at, d.created_at, d.persisted_at,
                 ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(NULLIF(LOWER(TRIM(d.source_path)), ''), 'doc:' || CAST(d.doc_id AS TEXT))
                   ORDER BY d.doc_id DESC
                 ) AS source_rank
          FROM documents d
          WHERE d.text_content IS NOT NULL
            AND TRIM(d.text_content) <> ''
            AND NOT EXISTS (SELECT 1 FROM sectionizer_outputs so WHERE so.doc_id = d.doc_id)
        )
        SELECT doc_id, source_path, author, text_content, metadata_json,
               extraction_status, extraction_error, content_sha256,
               modified_at, created_at, persisted_at
        FROM ranked WHERE source_rank = 1
        ORDER BY doc_id ASC
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
```

### 4.5 Update patterns

**SQLAlchemy:**
```python
session.execute(
    update(HoarderOutput)
    .where(HoarderOutput.hoarder_output_id.in_(ids))
    .values(screened_at=now)
)
```

**Django:**
```python
HoarderOutput.objects.filter(hoarder_output_id__in=ids).update(screened_at=now)
```

### 4.6 `ensure_standardizer_schema()` replacement

Delete this function. Replace all call sites with the assumption that `python manage.py migrate` has already been run. For the runtime schema-check pattern that individual agents used (calling `_ensure_*` before each DB write), simply remove those calls — Django migrations guarantee the schema exists.

If agents are called before `migrate` has been run, Django raises `OperationalError: no such table` which is a clear and actionable error.

---

## 5. Per-Agent Change Summary

### `agents/hoarder/storage.py`

| Remove | Replace with |
|---|---|
| `from db.standardizer_db import HoarderOutput, session_scope, ...` | `from pipeline.models import HoarderOutput, HoarderSourceRun, HoarderSourceArtifact` |
| `ensure_hoarder_outputs_schema()` | Delete entirely |
| `with session_scope() as session: session.add(...)` | `HoarderOutput.objects.create(...)` inside `transaction.atomic()` |
| `session.query(HoarderOutput).filter(...)` | `HoarderOutput.objects.filter(...)` |

### `agents/screener/storage.py`

| Remove | Replace with |
|---|---|
| `from db.standardizer_db import ScreenedFile, session_scope` | `from pipeline.models import ScreenedFile` |
| `ensure_screened_files_schema()` | Delete entirely |
| SQLAlchemy insert pattern | `ScreenedFile.objects.create(...)` |

### `agents/standardizer/agent.py`

| Remove | Replace with |
|---|---|
| `from db.standardizer_db import Document, DocumentScreenedFile, MediaAsset, ...` | `from pipeline.models import Document, DocumentScreenedFile, MediaAsset, ...` |
| `_ensure_schema()` | Delete entirely |
| All `session.add()` / `session.flush()` patterns | `.objects.create()` |
| `session.query(ScreenedFile).filter(ScreenedFile.is_selected==1, ScreenedFile.processed_at==None)` | `ScreenedFile.objects.filter(is_selected=1, processed_at__isnull=True)` |

### `agents/sectionizer/agent.py`

| Remove | Replace with |
|---|---|
| `from db.standardizer_db import SectionizerCategory, SectionizerOutput, ...` | `from pipeline.models import SectionizerCategory, SectionizerOutput, ...` |
| `_ensure_sectionizer_schema()` | Delete entirely |
| `ensure_standardizer_schema()` | Delete call |
| `session.query(SectionizerCategory).order_by(...)` | `SectionizerCategory.objects.order_by('sectionizer_category_id')` |
| Raw SQL CTE in `_load_rows_from_db()` | Move to `load_unsectionized_documents()` in `pipeline/` using `connection.cursor()` (§4.4) |
| `session.add(SectionizerOutput(...))` | `SectionizerOutput.objects.create(...)` |
| `session.add(SectionizerOutputDocument(...))` | `SectionizerOutputDocument.objects.create(...)` |

### `agents/newsletter_generator/agent.py`

| Remove | Replace with |
|---|---|
| `from db.standardizer_db import NewsletterRun, ...` | `from pipeline.models import NewsletterRun, ...` |
| `_ensure_newsletter_schema()` | Delete entirely |
| SQLAlchemy load + insert patterns | Django queryset equivalents |

### `run_observability_dashboard.py`

Replace all SQLAlchemy imports and `session_scope()` usage with Django ORM. This file is superseded by the Django admin dashboard — it can be deleted once the Django UI is in place.

---

## 6. Data Migration from Existing `standardizer.db`

PostgreSQL is a separate server — there is no shared DB file. Existing data in `data/standardizer.db` (SQLite) must be exported and imported.

### Fresh installation (no existing data to preserve)

```bash
python manage.py migrate        # creates all tables in PostgreSQL
python manage.py migrate        # also runs seed data migrations (§7)
```

### Migrating existing SQLite data to PostgreSQL

```bash
# Step 1 — create the schema in PostgreSQL
python manage.py migrate

# Step 2 — export from SQLite using a temporary settings override
DJANGO_SETTINGS_MODULE=sentinelpress.settings_sqlite \
    python manage.py dumpdata pipeline --indent 2 --natural-foreign \
    > /tmp/pipeline_dump.json
# settings_sqlite.py is a copy of settings.py with the SQLite DATABASES config
# and all JSONField columns reverted to TextField for the dump step

# Step 3 — import into PostgreSQL
python manage.py loaddata /tmp/pipeline_dump.json
```

**JSONField note:** The SQLite DB stores JSON columns as plain text strings. The dump/load cycle handles this correctly: `dumpdata` reads strings; `loaddata` with `JSONField` models parses them on insert. If a column value is malformed JSON, the load will fail — inspect the dump file first.

**Alternative — pgloader:** For large datasets, `pgloader` can migrate directly from SQLite to PostgreSQL without a dump file:
```bash
pgloader sqlite:///path/to/standardizer.db postgresql://user:pass@localhost/newsletter
```
This copies raw data; run `python manage.py migrate --fake-initial` first to create the schema without re-running the initial migration against empty tables.

Verify row counts after migration:
```bash
python manage.py shell -c "
from pipeline.models import HoarderOutput, Document, SectionizerOutput
print(HoarderOutput.objects.count(), Document.objects.count(), SectionizerOutput.objects.count())
"
```

---

## 7. Seed Data Migrations

Replace `_seed_sectionizer_categories()` and `_seed_homepage_items()` with Django data migrations.

```python
# pipeline/migrations/0002_seed_categories.py
from django.db import migrations
from django.utils import timezone
import json
from pathlib import Path

def seed_categories(apps, schema_editor):
    SectionizerCategory = apps.get_model("pipeline", "SectionizerCategory")
    if SectionizerCategory.objects.exists():
        return  # already seeded

    config_path = Path(__file__).resolve().parents[3] / "newsletter_adk" / "agents" / "sectionizer" / "config.json"
    if not config_path.exists():
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    sections = raw.get("sections") if isinstance(raw, dict) else []
    now = timezone.now().isoformat()
    for section in sections:
        if not isinstance(section, dict) or not section.get("name"):
            continue
        SectionizerCategory.objects.create(
            name=section["name"],
            objective=section.get("objective") or "",
            min_score=float(section.get("min_score", 0.0)),
            rules=json.dumps(section.get("rules") or []),
            created_at=now,
            updated_at=now,
        )

class Migration(migrations.Migration):
    dependencies = [("pipeline", "0001_initial")]
    operations = [migrations.RunPython(seed_categories, migrations.RunPython.noop)]
```

---

## 8. Django Admin Configuration

### Read-only pipeline tables (production safety)

Agents append to pipeline tables; admin should never silently overwrite them.

```python
# pipeline/admin.py
from django.contrib import admin
from .models import (
    HoarderOutput, ScreenedFile, Document,
    SectionizerOutput, NewsletterRun, SectionizerCategory,
)

@admin.register(SectionizerCategory)
class SectionizerCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "min_score", "objective", "created_at"]
    search_fields = ["name", "objective"]
    list_editable = ["min_score"]
    # Full edit allowed — categories are managed here

@admin.register(SectionizerOutput)
class SectionizerOutputAdmin(admin.ModelAdmin):
    list_display = ["sectionizer_output_id", "doc_id", "category", "match_count", "run_timestamp"]
    list_filter = ["category"]
    search_fields = ["source_path"]
    readonly_fields = [f.name for f in SectionizerOutput._meta.get_fields()]
    # Pipeline writes only — no edits from admin

    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["doc_id", "source_path", "extraction_status", "persisted_at"]
    readonly_fields = [f.name for f in Document._meta.get_fields()]
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
```

### Editable UI tables

```python
# chat/admin.py
from django.contrib import admin
from .models import UiPrompt, UiChatSession, UiChatMessage

@admin.register(UiPrompt)
class UiPromptAdmin(admin.ModelAdmin):
    list_display = ["prompt_id", "prompt_type", "provider", "model", "target_type", "target_id", "input_tokens", "latency_ms", "created_at"]
    list_filter = ["prompt_type", "provider", "model"]
    search_fields = ["user_prompt", "response_text"]
    readonly_fields = ["prompt_id", "created_at"]

# memory/admin.py
from django.contrib import admin
from .models import UiMemory, UiSetting

@admin.register(UiMemory)
class UiMemoryAdmin(admin.ModelAdmin):
    list_display = ["memory_id", "memory_type", "key", "source", "confidence", "is_active", "created_at"]
    list_filter = ["memory_type", "source", "is_active"]
    list_editable = ["is_active", "confidence"]
    search_fields = ["key", "value"]
```

---

## 9. Requirements Changes

```
# Remove from requirements.txt
sqlalchemy
alembic           # if present

# Add to requirements.txt
django>=5.0
psycopg[binary]>=3.1    # PostgreSQL driver (psycopg3); or use psycopg2-binary>=2.9
channels>=4.0
channels-redis>=4.0     # or daphne for ASGI
django-htmx>=1.17
django-tables2>=2.7
django-filter>=23.0
django-tailwind>=3.6    # or django-bootstrap5>=23.0
celery>=5.3             # for background agent runs
redis>=5.0              # for Celery broker and Channels layer
uvicorn[standard]>=0.27 # ASGI server
whitenoise>=6.6         # static files
```
