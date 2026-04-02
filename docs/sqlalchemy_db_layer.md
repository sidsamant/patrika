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
- mapped ORM classes such as `HoarderOutput`, `ScreenedFile`, `Document`, `SectionizerOutput`, `NewsletterRun`
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

## Guidance For Future Changes

- If a new table belongs in `standardizer.db`, add its model to [db/standardizer_db.py](/d:/ai/newsletter_adk/db/standardizer_db.py).
- Prefer reusing existing mapped models instead of writing raw SQL in agents or the dashboard.
- Use raw SQL only when the query is genuinely easier as SQL, such as a complex CTE or window-function query. Even then, run it through the shared SQLAlchemy session.
- Do not add new ad hoc schema creation code in individual modules if it can live in `ensure_standardizer_schema()`.
