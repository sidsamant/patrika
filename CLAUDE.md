**Status**: Draft

# newsletter_adk (pipeline agents): AI Instructions

@../.agents/AGENTS.md

Coding patterns: `../architecture/sentinelpress/coding_guidelines.md` §3. Agent design docs: `../architecture/newsletter_adk/`. Setup and run steps: `README.md` (note: `README.md` is gitignored in this repo).

## Project rules
- Use the venv at `.venv/` (`.venv/Scripts/python`).
- Agents live in `agents/<name>/agent.py` and expose `root_agent`. Prompts stay in `prompt_template.md` / `*.j2` next to the agent.
- **No direct DB access.** All reads and writes go through `agents/pipeline_client.py` (backend `/api/pipeline/`). If an endpoint is missing, add it in `backend/pipeline/api/` and commit in both repos. `db/standardizer_db_deprecated.py` is reference only.
- The backend talks to these agents only via the ADK api_server (`/run`, `/run_sse`, sessions). Keep agent names and session contracts stable, or update `backend/chat` and `backend/sectionizer` in the same task.
- `telemetry.setup_tracing()` must run before any `google.adk` import.
- Ignore `* copy*` prompt and config backups (e.g. `agents/sectionizer/prompt_template copy.md`) unless asked.
