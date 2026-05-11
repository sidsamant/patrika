# Documentation Audit — newsletter_adk

**Audited:** 2026-05-06  
**Scope:** README.md, docs/architecture.md, docs/newletter generator.md, docs/sqlalchemy_db_layer.md, docs/standardizer_db_tables.md, agents/sectionizer/TECHNICAL.md  
**Purpose:** Identify conflicts, discrepancies, and missing links before using these docs for AI-based code generation.

---

## 1. Missing Files (Docs Reference Non-Existent Code)

| Document | Claim | Reality |
|---|---|---|
| README.md | `python run_newsletter_generator.py` | File does not exist on disk |
| README.md | `streamlit run run_observability_dashboard.py` | File does not exist on disk |
| sectionizer/TECHNICAL.md | Reads `agents/sectionizer/config.json` at runtime | `config.json` is a one-time seed file only. Runtime source is the `sectionizer_categories` DB table. Doc has been corrected. |
| (none) | — | `agent.py` at project root exists but is not documented anywhere |

---

## 2. Architecture Doc vs. Actual Implementation

`docs/architecture.md` describes a system that does not match what was built. These are the most severe conflicts.

### 2a. Vector Database / RAG — stated but not built
The doc opens: *"The core architecture is RAG based."* Mermaid diagrams show a `@Database Vector` being written and queried. The actual pipeline is entirely relational SQLite via SQLAlchemy ORM. There is no vector store, no embedding step, and no retrieval. Every agent reads from `standardizer.db` using SQL joins.

### 2b. AWS SageMaker vs. Google ADK / Gemini
Diagrams use `@Sagemaker` components. The actual implementation uses `google.adk` with Gemini models. These are completely different platforms with different APIs and deployment models.

### 2c. Privacy Scrub stage — documented but absent
The data-preparation diagram shows `Classifier → PS ("Privacy Scrub") → Vector/Meta`. No privacy scrub agent or step exists in the actual pipeline:  
`hoarder → screener → standardizer → sectionizer → newsletter_generator`

### 2d. Stage naming — "Classifier/segmentizer" vs. "sectionizer"
The architecture calls stage 2 *"Classifier/segmentizer."* The actual agent directory and class are both named `sectionizer`. The word "sectionizer" never appears in `architecture.md`.

### 2e. Chatbot interface — stated goal, not current reality
Architecture says *"This should be a chatbot"* and references a *"User Intent"* query-rewriting component. The current system is a batch pipeline with a Streamlit observability dashboard. No chatbot, no query rewriting, no interactive interface exists.

### 2f. Human review loop — diagrammed but not implemented
The newsletter generation diagram shows a human reviewer iterating with AI inside a `while("Looks good to human")` loop. The current pipeline runs fully automated with no review gate.

---

## 3. Newsletter Generator Doc vs. Actual Implementation

`docs/newletter generator.md` (note: filename has typo — "newletter")

### 3a. File-based aggregation vs. DB-based
The doc says the Aggregator *"Scans specified directories for all sectionizer.json outputs."* The actual `agents/newsletter_generator/agent.py` reads from the `sectionizer_outputs` table in `standardizer.db` via SQLAlchemy — not from files on disk. (Files are also written to `.output/sectionizer/` as a side effect, but that is not the canonical input source.)

### 3b. PDF output channel — documented but absent
The doc lists *"PDF for WhatsApp"* as an output. The `newsletter_runs` table only has `output_markdown` and `output_html` columns. No PDF generation code exists.

### 3c. LLM usage contradiction
The doc says the Synthesis Agent *"Uses Gemini 1.5 Flash."*  
`docs/standardizer_db_tables.md` notes the newsletter generator is *"not actively calling an LLM in the current implementation."*  
These two documents directly contradict each other.

### 3d. Hard-coded space-industry segments vs. configurable sections
The doc defines static segments: `Launch & Propulsion`, `Satellite & EO`, `SSA & Defense`, `Ecosystem & Policy`. `sectionizer/TECHNICAL.md` says sections come from `config.json`, making them fully configurable — and the canonical `config.json` does not exist (see §1).

---

## 4. SQLAlchemy DB Layer Doc vs. Actual Code Pattern — RESOLVED

**Initial finding was incorrect.** The individual `_ensure_*()` functions in each agent are thin wrappers that all call `ensure_standardizer_schema()` from `db/standardizer_db.py`. The centralized bootstrap is correctly implemented. `sqlalchemy_db_layer.md` has been updated to document the pattern explicitly, including the `_seed_sectionizer_categories()` and `_seed_homepage_items()` calls inside `ensure_standardizer_schema()`.

---

## 5. Internal Contradictions in `standardizer_db_tables.md`

- **Phantom columns:** `newsletter_runs` has `llm_instruction` and `llm_content` columns, but the same doc notes the newsletter generator is *"not actively calling an LLM."* These columns exist in the schema but are unused.
- **Legacy table with no exit plan:** `screened_file_hoarder_outputs` is documented as "Legacy / no current usage" with a note that *"New code should not read from or write to this table."* There is no DROP TABLE, no migration note, and no timeline for removal.

---

## 6. Undocumented Modules and Files

None of these are referenced in any documentation:

| File | Notes |
|---|---|
| `agent.py` (project root) | Unknown purpose; may be ADK runner entry point |
| `agents/hoarder/policy1.py` | No doc; unclear if active or experimental |
| `agents/hoarder/config.py` | Hoarder configuration; not described |
| `agents/screener/util.py` | Utility helpers; purpose not described |
| `agents/screener/agent_hosted.py` | README mentions `--backend hosted` but the two screener files and their split are never explained |
| `agents/hoarder/sources/webpages/agent.py` | A fourth web source distinct from `websource` and `twitter`; not in `sqlalchemy_db_layer.md` |
| `agents/hoarder/sources/gdocs/`, `confluence/`, `sharepoint/`, `slack/`, `whatsapp/` | Source stubs; unknown if functional or placeholders |
| `.output/` directory | Written by sectionizer per TECHNICAL.md; never described in any other doc |
| Crawl4AI `crawler.sqlite3` schema | `sqlalchemy_db_layer.md` mentions the external DB path but never documents which tables/columns the hoarder reads from it |
| `sectionizer_categories` DB table | Now documented in `standardizer_db_tables.md`. Runtime source of truth for section definitions; `config.json` is seed-only. |
| `homepage_items` DB table | Now documented in `standardizer_db_tables.md`. Seeded from `newsletter_website` article frontmatter. |

---

## 7. File Hygiene — Backup Files in Repo

These files will confuse AI code generation tools which may treat any of them as canonical:

- `docs/newletter generator copy.md` (also has "newletter" typo)
- `agents/sectionizer/prompt_template copy.md`
- `agents/sectionizer/prompt_template copy 2.md`
- `agents/sectionizer/prompt_template copy 3.md`
- `agents/sectionizer/config copy.json`
- `agents/sectionizer/config1.json333`

---

## 8. Missing Documentation Critical for AI Code Generation

An AI code generator working from current docs would produce incorrect code in these areas:

| Missing | Impact |
|---|---|
| Environment variables / API keys | No `.env.example`; required keys (Gemini API key, crawl4ai DB path, etc.) are implicit |
| Per-agent data contracts | Only sectionizer has an explicit I/O schema (TECHNICAL.md). All other agents need the same |
| Module dependency graph | Which module imports which is entirely implicit; an AI must infer this |
| Screener dual-backend explanation | `--backend hosted` vs `--backend ollama` maps to different files; the split is undocumented |
| Crawl4AI table/column contract | The external DB is read but its schema is nowhere described |
| `config.json` role clarified | `config.json` is a one-time seed for `sectionizer_categories`; runtime config lives in the DB. TECHNICAL.md has been updated. |
| Error handling and retry patterns | Not documented for any agent |
| `run_hoarder_source.py` purpose | Exists and is in README but has no explanation of how it differs from `run_hoarder.py` |

---

## Recommended Changes

### Priority 1 — Correctness (blocks AI code generation)

1. **Rewrite `docs/architecture.md`** to reflect what exists: SQLite pipeline, Google ADK, Gemini, no vector DB, no RAG, no SageMaker. Replace the Mermaid diagrams with an accurate 5-stage pipeline flow.

2. **Reconcile `docs/newletter generator.md`**: change aggregation source from file scan to `sectionizer_outputs` table; remove PDF output; clarify whether the LLM is active or not; change segment list to "see sectionizer config."

3. **Fix or remove phantom columns**: either populate `llm_instruction`/`llm_content` in `newsletter_runs` or document them as reserved/unused in `standardizer_db_tables.md`.

4. ~~**Create `agents/sectionizer/config.json`**~~ — RESOLVED. `config.json` is a seed-only file; runtime source is `sectionizer_categories` table. `TECHNICAL.md` now documents the seed schema for reference.

### Priority 2 — Completeness (enables confident code generation)

5. **Create `docs/agents.md`** — one entry per agent with: purpose, input source (table + columns), output destination (table + columns), LLM model used (if any), key config files, and entry-point Python file.

6. **Create `docs/environment.md`** — list all required environment variables, the crawl4ai DB path assumption (`D:/ai/crawl4ai/data/crawler.sqlite3`), Gemini API key setup, and a `.env.example` file.

7. ~~**Document the `categories` table`**~~ — RESOLVED. `sectionizer_categories` and `homepage_items` tables are now documented in `standardizer_db_tables.md`, including their seed bootstrap mechanism.

8. ~~**Clarify schema bootstrap ownership**~~ — RESOLVED. `sqlalchemy_db_layer.md` now documents the centralized pattern: all `_ensure_*()` wrappers call `ensure_standardizer_schema()`.

9. **Explain the screener dual-backend** (`agent.py` vs `agent_hosted.py`) in either README or a new `docs/screener.md`.

10. **Document the Crawl4AI DB contract** — add a section to `sqlalchemy_db_layer.md` listing which tables and columns `websource/agent.py` and `twitter/agent.py` read from `crawler.sqlite3`.

### Priority 3 — Hygiene

11. **Delete or archive backup files**: all `* copy.*` files and `config1.json333`. Move to `/scratch` if needed for reference.

12. **Rename `docs/newletter generator.md`** to `docs/newsletter_generator.md` (fix typo, remove space).

13. **Document or remove** `agents/hoarder/sources/gdocs/`, `confluence/`, `sharepoint/`, `slack/`, `whatsapp/` — note explicitly if they are unimplemented stubs.
