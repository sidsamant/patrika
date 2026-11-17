**Status**: Draft

# newsletter_adk (pipeline agents): AI Instructions

@../.agents/AGENTS.md

Coding patterns: `../architecture/sentinelpress/coding_guidelines.md` §3. Agent design docs: `../architecture/newsletter_adk/`. Setup and run steps: `README.md` (note: `README.md` is gitignored in this repo).

## Project rules
- Use the venv at `.venv/` (`.venv/Scripts/python`).
- Agents live in `agents/<name>/agent.py` and expose `root_agent`. Prompts stay in `prompt_template.md` / `*.j2` next to the agent.
- **Agents: no direct DB access.** Code under `agents/` reads and writes through `agents/pipeline_client.py` (backend `/api/pipeline/`). If an endpoint is missing, add it in `backend/pipeline/api/` and commit in both repos. `db/standardizer_db_deprecated.py` is reference only.
- **CLI launchers use Django.** `run_*.py` put `../backend` on `sys.path` and call `django.setup()` with `sentinelpress.settings`. Keep them working when backend settings or models change.
- **Chat UI** is shared with the backend: `agents/api`, `agents/bulk_newsgenerator` and `agents/curator_greeting` serve `backend/chat` (React island and consumer).
- The backend talks to these agents only via the ADK api_server (`/run`, `/run_sse`, sessions). Keep agent names and session contracts stable, or update `backend/chat` and `backend/sectionizer` in the same task.
- `telemetry.setup_tracing()` must run before any `google.adk` import.
- Ignore `* copy*` prompt and config backups (e.g. `agents/sectionizer/prompt_template copy.md`) unless asked.
