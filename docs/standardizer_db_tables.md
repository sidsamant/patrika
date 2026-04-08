# `standardizer.db` Table Reference

This document describes the tables currently present in `data/standardizer.db`, what each table is for, and where each table is used in the `newsletter_adk` codebase.

Even though the file is named `standardizer.db`, it is now the shared pipeline database for hoarder, screener, standardizer, sectionizer, newsletter generation, and dashboard configuration/observability.

## Pipeline Lineage

The main content lineage through the shared DB is:

`hoarder_outputs -> screened_files -> documents -> sectionizer_outputs -> newsletter_runs`

The relation tables between those stages are:

- `document_screened_files`
  Maps standardized documents back to screener rows.
- `sectionizer_output_documents`
  Maps sectionizer outputs back to standardized documents.
- `newsletter_run_sectionizer_outputs`
  Maps newsletter runs back to the sectionizer outputs used to generate them.

Media and editorial side tables attached to that flow are:

- `media_assets`
  Stores extracted or referenced media for standardized documents.
- `hoarder_output_media_assets`
  Maps hoarder source rows to media assets.
- `newsletter_run_configs`
  Stores dashboard/editorial settings for a newsletter run.
- `hoarder_source_runs`
  Stores per-source hoarder execution history.
- `hoarder_source_artifacts`
  Stores per-source incremental-ingestion audit records.

## Hoarder

### `hoarder_outputs`
- Purpose:
  Stores the append-only output rows produced by hoarder source agents. These are the raw source records handed to screener.
- Key columns:
  - `hoarder_output_id`: primary key
  - `source_id`: hoarder source id such as `filesystem`, `websource`, `twitter`
  - `source_path`: source path or URL
  - `name`: source item title/name
  - `source_payload_json`: original hoarder payload
  - `created_at`: when hoarder persisted the row
  - `screened_at`: when screener last read the row
- Used in code:
  - `agents/hoarder/storage.py`
    - schema creation in `ensure_hoarder_outputs_schema()`
    - inserts in `persist_hoarder_payload()`
    - reads in `load_hoarder_rows_for_screening()`
    - updates in `mark_hoarder_rows_screened()`
  - `agents/hoarder/agent.py`
    - persists hoarder results through `persist_hoarder_payload()`
  - `agents/screener/storage.py`
    - linked directly from `screened_files.hoarder_output_id`
  - `agents/standardizer/agent.py`
    - used indirectly by `hoarder_output_media_assets`
  - `run_observability_dashboard.py`
    - pending screener view
    - newsletter lineage joins

### `hoarder_source_runs`
- Purpose:
  Stores one row per explicit hoarder source-agent run for dashboard observability.
- Key columns:
  - `source_id`
  - `source_path`
  - `status`
  - `item_count`
  - `error_text`
  - `created_at`
- Used in code:
  - `agents/hoarder/storage.py`
    - schema creation in `ensure_hoarder_outputs_schema()`
    - inserts in `record_hoarder_source_run()`
  - `run_hoarder_source.py`
    - records success/error outcomes for single-source runs
  - `run_observability_dashboard.py`
    - hoarder page shows last run time/status/count

### `hoarder_source_artifacts`
- Purpose:
  Hoarder audit table for incremental source ingestion. Tracks which upstream artifacts were already processed.
- Current usage:
  - `websource` uses keys like `scraped_item:<id>` for Crawl4AI rows
  - `twitter` uses the same pattern for X/Twitter rows
- Key columns:
  - `source_id`
  - `artifact_path`
  - `status`
  - `error_text`
  - `processed_at`
- Used in code:
  - `agents/hoarder/storage.py`
    - schema creation in `ensure_hoarder_outputs_schema()`
    - reads in `load_processed_hoarder_artifact_paths()`
    - inserts in `record_hoarder_source_artifact()`
  - `agents/hoarder/sources/websource/agent.py`
    - filters unread Crawl4AI rows
    - records processed row ids
  - `agents/hoarder/sources/twitter/agent.py`
    - filters unread Crawl4AI X/Twitter rows
    - records processed row ids

## Screener

### `screened_files`
- Purpose:
  Stores screener decisions as append-only rows. Standardizer reads selected rows from here.
- Key columns:
  - `screened_file_id`
  - `hoarder_output_id`
  - `source_path`
  - `name`
  - `is_selected`
  - `rejection_reason`
  - `source_payload_json`
  - `created_at`
  - `processed_at`
- Used in code:
  - `agents/screener/storage.py`
    - schema creation in `ensure_screened_files_schema()`
    - inserts in `persist_screened_payload()`
  - `agents/standardizer/agent.py`
    - reads in `_load_screened_rows_from_db()`
    - dedupes selected rows in `_dedupe_selected_screened_rows()`
    - updates `processed_at` in `_mark_screened_row_processed()`
  - `run_observability_dashboard.py`
    - pending standardizer view
    - newsletter lineage joins

### `screened_file_hoarder_outputs`
- Status:
  Legacy table that is no longer required by the current pipeline.
- Why it is redundant:
  `screened_files.hoarder_output_id` already links each screener row back to its hoarder row.
- Current code usage:
  None. New code should not read from or write to this table.

## Standardizer

### `documents`
- Purpose:
  Stores standardized text, metadata, and extraction status for selected items.
- Key columns:
  - `doc_id`
  - `source_path`
  - `author`
  - `text_content`
  - `metadata_json`
  - `extraction_status`
  - `extraction_error`
  - `content_sha256`
  - `modified_at`
  - `created_at`
  - `persisted_at`
- Used in code:
  - `agents/standardizer/agent.py`
    - schema creation in `_ensure_schema()`
    - inserts in `DocumentStandardizerAgent._run_async_impl()`
  - `agents/sectionizer/agent.py`
    - reads pending standardized rows in `_load_rows_from_db()`
  - `run_observability_dashboard.py`
    - pending sectionizer view
    - newsletter lineage joins

### `document_screened_files`
- Purpose:
  Relation table linking each standardized document to the screener row it came from.
- Key columns:
  - `doc_id`
  - `screened_file_id`
  - `created_at`
- Used in code:
  - `agents/standardizer/agent.py`
    - schema creation in `_ensure_schema()`
    - inserts in `DocumentStandardizerAgent._run_async_impl()`
  - `run_observability_dashboard.py`
    - lineage joins from documents back to screened files

### `media_assets`
- Purpose:
  Stores image/media artifacts extracted or associated during standardization.
- Key columns:
  - `media_id`
  - `doc_id`
  - `artifact_path`
  - `mime_type`
  - `created_at`
- Used in code:
  - `agents/standardizer/agent.py`
    - schema creation in `_ensure_schema()`
    - inserts in `DocumentStandardizerAgent._run_async_impl()`
  - `agents/newsletter_generator/agent.py`
    - reads in `_load_media_assets()`
    - picks hero/primary images in `_pick_primary_image()`
  - `run_observability_dashboard.py`
    - shows images in lineage/story views

### `hoarder_output_media_assets`
- Purpose:
  Relation table linking hoarder source rows to extracted media assets.
- Key columns:
  - `hoarder_output_id`
  - `media_id`
  - `created_at`
- Used in code:
  - `agents/standardizer/agent.py`
    - schema creation in `_ensure_schema()`
    - inserts in `DocumentStandardizerAgent._run_async_impl()`
  - `run_observability_dashboard.py`
    - lineage joins to show source-associated images

## Sectionizer

### `sectionizer_outputs`
- Purpose:
  Append-only sectionizer results for each processed document, including persisted LLM input/output traces.
- Key columns:
  - `sectionizer_output_id`
  - `doc_id`
  - `source_path`
  - `llm_instruction`
  - `llm_content`
  - `output_json`
  - `match_count`
  - `run_timestamp`
  - `created_at`
- Used in code:
  - `agents/sectionizer/agent.py`
    - schema creation in `_ensure_sectionizer_schema()`
    - inserts in `_persist_row_output()`
    - dedupe/unprocessed check in `_load_rows_from_db()`
  - `agents/newsletter_generator/agent.py`
    - loads eligible rows in `_load_sectionizer_outputs()`
  - `run_observability_dashboard.py`
    - newsletter detail and lineage
    - sectionizer prompt inspection

### `sectionizer_output_documents`
- Purpose:
  Relation table linking sectionizer outputs to standardized documents.
- Key columns:
  - `sectionizer_output_id`
  - `doc_id`
  - `created_at`
- Used in code:
  - `agents/sectionizer/agent.py`
    - schema creation in `_ensure_sectionizer_schema()`
    - inserts in `_persist_row_output()`
  - `run_observability_dashboard.py`
    - lineage joins from newsletter runs to documents and upstream records

## Newsletter Generator

### `newsletter_runs`
- Purpose:
  Stores one row per newsletter generation run.
- Key columns:
  - `newsletter_run_id`
  - `run_timestamp`
  - `llm_instruction`
  - `llm_content`
  - `output_markdown`
  - `output_html`
  - `output_json`
  - `created_at`
- Used in code:
  - `agents/newsletter_generator/agent.py`
    - schema creation in `_ensure_newsletter_schema()`
    - inserts in `_persist_newsletter_run()`
  - `run_observability_dashboard.py`
    - run list
    - run detail
    - export HTML generation

### `newsletter_run_sectionizer_outputs`
- Purpose:
  Relation table linking one newsletter run to the sectionizer rows used to build it.
- Key columns:
  - `newsletter_run_id`
  - `sectionizer_output_id`
  - `created_at`
- Used in code:
  - `agents/newsletter_generator/agent.py`
    - schema creation in `_ensure_newsletter_schema()`
    - inserts in `_persist_newsletter_run()`
    - exclusion logic in `_load_sectionizer_outputs()`
  - `run_observability_dashboard.py`
    - loads newsletter run detail and lineage

## Dashboard / Editorial Configuration

### `newsletter_run_configs`
- Purpose:
  Stores per-newsletter-run editor settings used by the dashboard, currently including the newsletter date.
- Key columns:
  - `newsletter_run_id`
  - `newsletter_date`
  - `config_json`
  - `created_at`
  - `updated_at`
- Used in code:
  - `run_observability_dashboard.py`
    - schema creation in `_ensure_dashboard_schema()`
    - reads in `_load_run_config()`
    - inserts/updates in `save_run_config()`
    - used when generating downloadable HTML exports

## Notes

- Some tables are created in agent modules and also re-created defensively in `run_observability_dashboard.py` so the dashboard can work against older DB files.
- `newsletter_runs` currently includes `llm_instruction` and `llm_content` columns even though the newsletter generator is not actively calling an LLM in the current implementation.
- The shared DB now functions as the main lineage store across the whole pipeline, not only as a standardizer output database.
