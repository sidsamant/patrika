# Sectionizer Output Data Model

Exhaustive reference for the data stored in `pipeline.sectionizer_outputs` and its `output_json` blob, derived from reading actual rows in the database (10 rows as of the first pipeline run, 2026-05-09).

---

## Context: What the Sectionizer Does

The sectionizer agent receives each standardized document (from `pipeline.standardizer_outputs`) and evaluates it against every configured newsletter category (from `pipeline.sectionizer_categories`). For each category it:

1. Applies keyword/regex rules mechanically to produce per-rule scores.
2. Calls an LLM (Gemini) to produce a `raw_llm_score` and a written `summary` + `newsletter_title`.
3. Combines the rule scores (weighted) into a single composite `score`.
4. Compares the composite score against the category's `min_score` threshold.
5. A document that passes at least one category's threshold is a **match**. The best-matching category is recorded as the winning `category_id`.

Documents that match no category still get a row in `sectionizer_outputs` — `match_count = 0`, `category = null` — because the `document_summary` and per-category evaluations are still useful for human review.

---

## DB Table: `pipeline.sectionizer_outputs`

| Column | Type | Description |
|--------|------|-------------|
| `sectionizer_output_id` | PK int | Auto-increment primary key |
| `standardizer_output_id` | FK int | References `pipeline.standardizer_outputs` (the source document) |
| `category_id` | FK int / null | The winning category (highest-scoring category that passed threshold). `null` if no match. |
| `source_path` | text | URL or file path of the original source article — duplicated from `standardizer_outputs.source_path` for quick access |
| `match_count` | int | Number of categories the document matched (score ≥ min_score). 0 = no match; 1+ = matched |
| `llm_instruction` | text | The full system prompt sent to the LLM for this evaluation (the "role + rules" block) |
| `llm_content` | text | The full user message sent to the LLM — contains the document metadata and full text |
| `output_json` | jsonb | Full structured output from the sectionizer agent (see below) |
| `output_path` | text | Reserved for a future file export path; currently an empty string |
| `homepage_slot` | text / null | `"headline"` or `"latest"` — for homepage display (currently unused) |
| `homepage_position` | int / null | Ordering within a homepage slot (currently unused) |
| `homepage_active` | int / null | 1 = show on homepage, 0/null = hidden (currently unused) |
| `run_timestamp` | text | When the sectionizer run occurred. Format: `YYYYMMDD-HHMMSS` (e.g. `20260509-235838`) |
| `created_at` | text | ISO-8601 UTC timestamp when the row was inserted |
| `agent_run_id` | FK int / null | References `pipeline.agent_runs` |
| `newsletter_run_id` | FK int / null | References `pipeline.newsletter_runs`. `null` until the document is included in a newsletter. Set to non-null when the newsletter generator runs and claims this output. |

---

## `output_json` Structure

`output_json` is a single JSON object that is the complete agent output for one document. It contains everything the agent computed.

### Top-Level Keys

| Key | Type | Description |
|-----|------|-------------|
| `doc_id` | int | The `standardizer_output_id` of the document that was evaluated |
| `rowSource` | string | Always `"postgresql.documents"` in current data — identifies where the document was loaded from |
| `runTimestamp` | string | Same as the DB column `run_timestamp` (`YYYYMMDD-HHMMSS`) |
| `source_path` | string | Source URL/path of the article (same as DB column) |
| `llm_instruction` | string | Copy of the system prompt (same as DB column `llm_instruction`) |
| `llm_content` | string | Copy of the full user message (same as DB column `llm_content`). Contains document metadata JSON + full text |
| `document_summary` | string | **LLM-generated 2–3 sentence neutral summary of what the document is about.** Generated before category evaluation. This is the readable "what is this article" description. Examples: *"SatSure has partnered with IN-SPACe, Pixxel, Dhruva Space, and PierSight to develop India's first private Earth Observation satellite constellation."* |
| `category_id` | int / null | The winning category ID (same as DB column `category_id`). `null` if `match_count = 0` |
| `category_name` | string / null | The winning category name (e.g. `"Satellite & EO"`). `null` if `match_count = 0` |
| `matches` | array | List of categories that **passed** their threshold — i.e., the document matched them. Empty array if `match_count = 0`. Contains full match objects (see below) |
| `section_evaluations` | array | List of all 5 category evaluations regardless of pass/fail. Always has one entry per category. Contains the same structure as `matches` items (see below) |
| `promptTemplatePath` | string | Path to the prompt template `.md` file used for this run. Present in newer rows only. Example: `D:\ai\newsletter_adk\agents\sectionizer\prompt_template.md` |
| `sectionConfigSource` | string | Where category config was loaded from. In current rows: `"postgresql"` — confirming the DB is the runtime source |

---

### `matches` and `section_evaluations` Items

Both arrays contain objects with the same structure. `matches` is the filtered subset of `section_evaluations` where `passes_threshold = true`.

| Key | Type | Description |
|-----|------|-------------|
| `section` | string | Category name (e.g. `"Satellite & EO"`, `"Launch & Propulsion"`) |
| `category_id` | int | Category ID from `pipeline.sectionizer_categories` |
| `objective` | string | The category's objective text as stored in the DB. Describes what kind of content belongs here. Example: *"News about artificial satellites and Earth Observation."* |
| `min_score` | float | The minimum composite score required to pass. Loaded from `sectionizer_categories.min_score`. Values in current data: 0.5 – 0.7 |
| `score` | float | **Composite score** — the weighted average of all `rule_scores`. Range 0.0–1.0. Computed as: `sum(rule.weight × rule.score) / sum(rule.weight)` |
| `raw_llm_score` | float | **LLM's self-reported confidence** that this document belongs in this category. The LLM returns a 0.0–1.0 value. Currently observed: 0.0 (no match) or 0.9 (strong match). This is a separate signal from `score` |
| `passes_threshold` | bool | `true` if `score >= min_score`. This is the gate — only rows with `passes_threshold = true` appear in `matches` |
| `matched_rule_count` | int | Number of rules that fired (i.e., `rule.score > 0`). 0 means no rules matched |
| `newsletter_title` | string | **LLM-generated headline** for the newsletter — a short punchy title for this document in this category. Empty string if no match. Example: *"SatSure to Lead India's First Private EO Constellation with IN-SPACe Partnership"* |
| `summary` | string | **LLM-generated newsletter summary paragraph** for this document in this category. A multi-sentence editorial write-up suitable for publication. Empty string if no match. This is the primary content that would appear in the newsletter |
| `summary_facts` | array | Intended for bullet-point facts extracted from the document. Empty list in all current rows — a field reserved for a future feature |
| `rule_scores` | array | Per-rule breakdown — one object per rule defined in `sectionizer_categories.rules` (see below) |

---

### `rule_scores` Items

Each rule in a category's `rules` JSON array is evaluated individually. `rule_scores` records the outcome.

| Key | Type | Description |
|-----|------|-------------|
| `index` | int | Zero-based position of this rule in the category's `rules` array |
| `rule` | string | The rule definition as a JSON string. Contains `field`, `op`, `value`, `weight`. Example: `{"field": "text", "op": "contains", "value": "constellation", "weight": 0.4}` |
| `score` | float | Binary: `1.0` if the rule fired, `0.0` if it did not. Rules are currently binary (no partial credit) |
| `fact` | string | What the LLM found in the document relevant to this rule. `"N/A"` if the rule didn't fire. If fired, it's a one-sentence statement extracted from the document. Example: *"SatSure is partnering to build India's first private EO satellite constellation."* |
| `reason` | string | Plain-English explanation of the score. Examples: *"The text contains the keyword 'constellation'."* / *"The text does not contain the keyword 'imaging'."* |

**Rule definition fields:**

| Field | Meaning |
|-------|---------|
| `field` | What to test against — always `"text"` in current rules |
| `op` | Operation — `"contains"` (substring) or `"regex"` (regex match) |
| `value` | The keyword or regex pattern to match |
| `weight` | This rule's contribution weight. All rule weights in a category sum to 1.0. Higher weight = more influence on composite score |

---

## Current Categories and Their Rules (as of first pipeline run)

### 1. Launch & Propulsion — min_score: 0.6
| Rule | Op | Value | Weight |
|------|----|-------|--------|
| 0 | contains | `orbital launch` | 0.4 |
| 1 | contains | `engine test` | 0.3 |
| 2 | regex | `(Vikram\|Agnibaan\|Spectrum\|Starship)` | 0.3 |

### 2. Satellite & EO — min_score: 0.6
| Rule | Op | Value | Weight |
|------|----|-------|--------|
| 0 | contains | `constellation` | 0.4 |
| 1 | contains | `imaging` | 0.3 |
| 2 | contains | `Earth Observation` | 0.3 |

### 3. SSA & Defense — min_score: 0.5
*(rules not yet observed in this dataset)*

### 4. Ecosystem & Policy — min_score: 0.7
*(rules not yet observed in this dataset)*

### 5. International Desk — min_score: 0.6
*(rules not yet observed in this dataset)*

---

## Observed Data Patterns (10 rows, run 20260509-235838)

- **All 10 documents** are press releases from `satsure.co/press-release/...`
- **9 of 10** have `match_count = 0` — no category passed its threshold
- **1 of 10** (id=3) matched **Satellite & EO** with composite score 0.6667 (2 of 3 rules fired: "constellation" weight 0.4 + "Earth Observation" weight 0.3)
- **Every row** has `section_evaluations` with 5 entries — all categories are always evaluated
- **Every row** has a `document_summary` — the LLM produces a summary regardless of whether any category matches
- `newsletter_title` and `summary` are only populated in `section_evaluations` entries that passed or nearly passed — entries with `score = 0.0` have empty strings
- `summary_facts` is `[]` in all current rows
- `raw_llm_score` appears to be either 0.0 (no relevance) or 0.9 (strong relevance) in current data — suggesting the LLM is decisive

---

## How the Review Page Uses This Data

The **Sectionizer Review** list page (`/review/`) shows one row per `sectionizer_output`. The columns displayed are:

- **Doc** — `sectionizer_output_id` (PK)
- **Source** — `source_path` (the article URL)
- **Category** — winning category name (from `category_id` FK), or blank if no match
- **Match count** — `match_count`
- Links: **Detail** → full evaluation breakdown, **Refine in Chat** → opens chat with a prefill

The **Detail page** shows the `section_evaluations` array as sections with score badges, rule score breakdowns, and the per-field values (`newsletter_title`, `summary`) that can be edited in-place via HTMX.

---

## Relationship to Other Tables

```
hoarder_outputs
    └── screener_outputs  (screener_output_id FK)
            └── standardizer_outputs  (standardizer_output_id FK)
                    └── sectionizer_outputs  (standardizer_output_id FK)
                                └── newsletter_runs  (newsletter_run_id FK on sectionizer_outputs)
```

- `sectionizer_outputs.standardizer_output_id` → the document that was evaluated
- `sectionizer_outputs.newsletter_run_id` → set when this output is claimed by a newsletter run; `null` until then — used by the newsletter generator to find unprocessed outputs
- `sectionizer_outputs.category_id` → the winning category from `sectionizer_categories`
