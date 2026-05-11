# SQLAlchemy DB Layer

This project now uses SQLAlchemy 2.x as the ORM for the shared pipeline database at `data/standardizer.db`.

## Where The Shared DB Layer Lives

The shared ORM module is:

- [db/standardizer_db.py](/d:/ai/newsletter_adk/db/standardizer_db.py)

That file contains:
- the SQLite engine
- the SQLAlchemy session factory
- the shared schema bootstrap helper
- ORM models for every table in `standardizer.db`

## Main Pattern

Code that reads or writes the shared pipeline DB should import from `db.standardizer_db` and use:

- `session_scope()`
- mapped ORM classes: `HoarderOutput`, `HoarderSourceRun`, `HoarderSourceArtifact`, `ScreenedFile`, `Document`, `DocumentScreenedFile`, `MediaAsset`, `HoarderOutputMediaAsset`, `SectionizerCategory`, `SectionizerOutput`, `SectionizerOutputDocument`, `NewsletterRun`, `NewsletterRunSectionizerOutput`, `NewsletterRunConfig`, `HomepageItem`
- `ensure_standardizer_schema()` when explicit bootstrap is needed

Typical pattern:

```python
from sqlalchemy import select

from db.standardizer_db import NewsletterRun, session_scope

with session_scope() as session:
    rows = session.execute(
        select(NewsletterRun).order_by(NewsletterRun.newsletter_run_id.desc())
    ).scalars().all()
```

## What Uses The ORM Now

The shared `standardizer.db` access is now ORM-backed in:

- [agents/hoarder/storage.py](/d:/ai/newsletter_adk/agents/hoarder/storage.py)
- [agents/screener/storage.py](/d:/ai/newsletter_adk/agents/screener/storage.py)
- [agents/standardizer/agent.py](/d:/ai/newsletter_adk/agents/standardizer/agent.py)
- [agents/sectionizer/agent.py](/d:/ai/newsletter_adk/agents/sectionizer/agent.py)
- [agents/newsletter_generator/agent.py](/d:/ai/newsletter_adk/agents/newsletter_generator/agent.py)
- [run_observability_dashboard.py](/d:/ai/newsletter_adk/run_observability_dashboard.py)

## What Still Uses Raw SQLite

The Crawl4AI-backed hoarder sources still use Python `sqlite3`, because they read a different database owned by the sibling `crawl4ai` project:

- [agents/hoarder/sources/websource/agent.py](/d:/ai/newsletter_adk/agents/hoarder/sources/websource/agent.py)
- [agents/hoarder/sources/twitter/agent.py](/d:/ai/newsletter_adk/agents/hoarder/sources/twitter/agent.py)

Those do not read `data/standardizer.db`; they read `D:/ai/crawl4ai/data/crawler.sqlite3`.

## Why This Split Exists

- `standardizer.db` is owned by this project, so we keep one shared ORM model layer for it.
- `crawler.sqlite3` is owned by Crawl4AI, so those readers stay lightweight and isolated.

## Schema Bootstrap Pattern

All table creation, column migrations, trigger creation, and seed data insertion flow through the single function `ensure_standardizer_schema()` in `db/standardizer_db.py`. Individual agent modules contain thin `_ensure_*()` wrapper functions (e.g., `_ensure_sectionizer_schema()` in the sectionizer agent) — these wrappers simply call `ensure_standardizer_schema()` and do not create tables independently.

`session_scope()` calls `ensure_standardizer_schema()` on every invocation, so the schema is always up to date before any DB access.

Seed operations inside `ensure_standardizer_schema()`:
- `_seed_sectionizer_categories()` — populates `sectionizer_categories` from `agents/sectionizer/config.json` if the table is empty.
- `_seed_homepage_items()` — populates `homepage_items` from article frontmatter in `newsletter_website/src/content/articles/` if the table is empty.

## Guidance For Future Changes

- If a new table belongs in `standardizer.db`, add its ORM model to [db/standardizer_db.py](/d:/ai/newsletter_adk/db/standardizer_db.py) and call `_ensure_column()` or add a seed step inside `ensure_standardizer_schema()`.
- Prefer reusing existing mapped models instead of writing raw SQL in agents or the dashboard.
- Use raw SQL only when the query is genuinely easier as SQL, such as a complex CTE or window-function query. Even then, run it through the shared SQLAlchemy `session_scope()`.
- Do not add new schema creation code in individual agent modules — it belongs in `ensure_standardizer_schema()`.
