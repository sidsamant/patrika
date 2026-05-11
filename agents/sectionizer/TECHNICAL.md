# Sectionizer Technical Notes

## Overview

`agents/sectionizer/agent.py` uses an internal Google ADK `LlmAgent` backed by Gemini to score each standardized document against the configured newsletter sections. The Python code handles data loading, prompt rendering, response normalization, score thresholding, and persistence, while the section/rule evaluation itself is delegated to the ADK LLM flow. Prompt instructions live in `prompt_template.md` instead of being embedded in code.

## Runtime Flow

1. The ADK runner invokes `SectionizerAgent._run_async_impl`.
2. The agent logs the incoming ADK user message from `ctx.user_content` for debugging.
3. `_load_categories_from_db()` queries the `sectionizer_categories` table in `data/standardizer.db` for all active section definitions.
4. `_load_prompt_template()` reads `agents/sectionizer/prompt_template.md`.
5. `_load_standardized_rows()` resolves input rows from `data/standardizer.db`, table `documents`. If the database is missing or empty, the agent falls back to an empty list.
6. For each document row, `_render_prompt()` injects:
   - the runner prompt
   - the full standardized document JSON
   - the raw document text
   - the configured section definitions, including plain-text rules
7. `sectionizer_instruction_provider()` renders the full prompt template for the current document.
8. `sectionizer_before_model_callback()` logs the rendered prompt before the ADK model call.
9. Before each document after the first, the agent pauses for `SECTIONIZER_LLM_DELAY_SECONDS` seconds (default `2.0`) to reduce rate-limit pressure.
10. The internal `LlmAgent` calls Gemini with `response_mime_type="application/json"`.
11. Gemini returns per-section output including:
   - per-rule scores
   - a newsletter title
   - a factual summary
   - summary facts grounded in the document
12. `_normalize_rule_scores()` and `_normalize_section_result()` merge the Gemini output back with the configured plain-text rules and compute the section score as the average of returned per-rule scores.
13. Sections whose computed score is at least `min_score` are copied into `matches`.
14. The agent persists one JSON file per standardized document row to:
    - `.output/sectionizer/doc_<doc_id>_<run_timestamp>.json`
    - the `sectionizer_outputs` table in `data/standardizer.db`
    - `ctx.session.state["section_mappings"]`
15. The same payload is emitted as the ADK event response.

## Input Model

Each standardized row is expected to look roughly like this:

```json
{
  "doc_id": 1,
  "text": "document body",
  "metadata": {
    "filesystem": {
      "path": "..."
    },
    "source": {
      "path": "...",
      "name": "..."
    },
    "extracted": {
      "title": "..."
    }
  }
}
```

Rows are loaded from SQLite only.

## Section Configuration

Section definitions are stored in the `sectionizer_categories` table in `data/standardizer.db`, not in a JSON file at runtime. The agent calls `_load_categories_from_db()` which queries `sectionizer_categories` via SQLAlchemy ORM (`SectionizerCategory` model in `db/standardizer_db.py`).

Each row in `sectionizer_categories` provides:

| Column | Type | Description |
|---|---|---|
| `sectionizer_category_id` | INTEGER | Primary key; passed through as `category_id` in outputs |
| `name` | TEXT | Section name shown in newsletter and output JSON |
| `objective` | TEXT (nullable) | Optional free-text description sent to the LLM |
| `min_score` | FLOAT | Threshold (0.0–1.0); document must score at or above this to match |
| `rules` | TEXT | JSON-serialized list of plain-text rule strings |
| `created_at`, `updated_at` | TEXT | ISO 8601 timestamps |

Rules are plain-text instructions for the LLM. The LLM receives each rule exactly as stored and is expected to score every rule on a `0.0` to `1.0` scale.

### Seed Bootstrap (one-time only)

`agents/sectionizer/config.json` is a **seed file**, not a runtime config. On the first call to `ensure_standardizer_schema()`, if the `sectionizer_categories` table is empty and `config.json` exists, `_seed_sectionizer_categories()` reads the file's `sections` array and inserts the rows. Once the table has any rows, `config.json` is ignored entirely.

To add or edit categories after initial setup, update the `sectionizer_categories` table directly (via the admin panel or SQL). Changes take effect on the next sectionizer run without any file changes.

The expected shape of `config.json` (if used for seeding) is:

```json
{
  "sections": [
    {
      "name": "Engineering",
      "objective": "Documents about engineering work",
      "min_score": 0.25,
      "rules": [
        "Prioritize documents about API changes, incidents, release notes, or architecture work.",
        "Prefer documents whose source clearly belongs to engineering."
      ]
    }
  ]
}
```

## Prompt Template

`prompt_template.md` is the source of truth for LLM instructions. It tells Gemini to:

- evaluate every configured section
- interpret the configured rules as plain-text instructions
- return a score for every rule
- generate a newsletter title per section
- generate a short factual summary per section
- provide `summary_facts` grounded strictly in the document
- return JSON only

This keeps prompt text out of Python code and makes prompt iteration easier.

## Path Resolution

`_path_get()` is still used for output convenience, mainly to recover a stable `source_path` from standardized metadata.

Examples:

- `text`
- `metadata.extracted.title`
- `metadata.source.path`
- `documents.0.title`

The resolver supports dictionaries and list indices.

## Output Shape

The emitted payload contains:

```json
{
  "rowSource": "...",
  "sectionConfigPath": "...",
  "promptTemplatePath": "...",
  "runTimestamp": "20260320-120000",
  "totalRows": 10,
  "matchedRows": 4,
  "outputDirectory": "D:\\ai\\newsletter_adk\\.output\\sectionizer",
  "outputs": [
    {
      "sectionizer_output_id": 123,
      "doc_id": 1,
      "output_path": "D:\\ai\\newsletter_adk\\.output\\sectionizer\\doc_1_20260320-120000.json",
      "run_timestamp": "20260320-120000",
      "created_at": "2026-03-20T12:00:00Z",
      "match_count": 1,
      "output": {
        "rowSource": "...",
        "sectionConfigPath": "...",
        "promptTemplatePath": "...",
        "runTimestamp": "20260320-120000",
        "source_path": "...",
        "document_summary": "...",
        "section_evaluations": [
        {
          "section": "Engineering",
          "score": 0.6,
          "newsletter_title": "...",
          "summary": "...",
          "summary_facts": ["..."],
          "rule_scores": [
            {
              "index": 0,
              "rule": "Prioritize documents about API changes, incidents, release notes, or architecture work.",
              "score": 0.7,
              "reason": "...",
              "fact": "..."
            }
          ]
        }
      ],
      "matches": [
        {
          "section": "Engineering",
          "score": 0.6,
          "min_score": 0.25,
          "matched_rule_count": 1,
          "newsletter_title": "...",
          "summary": "...",
          "summary_facts": ["..."],
          "rule_scores": []
        }
      ]
      }
    }
  ]
}
```

## Debug Logging

The module now enables debug logging and records:

- the incoming runner prompt from `ctx.user_content`
- the loaded section configuration
- the prompt template path
- the full rendered Gemini prompt
- the raw Gemini response
- the resolved row source and row count
- each document's passing section count
- the final persisted output payload

This gives you a full trace of the LLM inputs and outputs used to classify documents into newsletter sections.
