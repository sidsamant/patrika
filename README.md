**Status**: Changed

# Newsletter ADK: SentientPress pipeline agents

Google ADK agents for the SentientPress pipeline. The backend calls them over HTTP through the ADK api_server, and they read and write data only through the backend REST API (`agents/pipeline_client.py` → `/api/pipeline/`). See [../architecture/sentinelpress/system_overview.md](../architecture/sentinelpress/system_overview.md).

## Setup (PowerShell)

```powershell
cd d:\ai\sentinelpress\newsletter_adk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

To rebuild a broken venv, first run `deactivate; Remove-Item -Recurse -Force .venv`, then repeat the steps above.

`.env` must define `PIPELINE_API_URL` (default `http://localhost:8000`) and a `PIPELINE_API_KEY` that matches the backend, plus `GOOGLE_API_KEY`.

`filesource` uses the MCP filesystem server via `npx @modelcontextprotocol/server-filesystem`, so Node.js and `npx` must be on PATH.

## Start the ADK API server (used by the backend)

```powershell
.\.venv\Scripts\Activate.ps1
adk.exe api_server agents/ --port 8001
```

Swagger docs: <http://localhost:8001/docs>

## ADK web UI (development)

From the `agents` directory:

```powershell
adk web                      # all agents
adk web bulk_newsgenerator   # a single agent
```

## Pipeline (CLI launchers)

`hoarder -> screener -> standardizer -> sectionizer -> newsletter generator`

```powershell
python run_pipeline.py                     # full pipeline
python run_pipeline.py --backend hosted    # or --backend ollama (screener backend)
python run_hoarder.py
python run_hoarder_source.py
python run_screener.py --backend hosted
python run_standardizer.py
python run_sectionizer.py
python run_sectionizer_single.py
python run_batch_sectionizer.py            # Gemini Batch API path
python run_newsletter.py
```

Observability and review now happen in the backend (Django). The old Streamlit dashboard (`newsletter_admin`) is deprecated.
