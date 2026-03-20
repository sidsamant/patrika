# Multi-Source Newsletter Synthesis Engine

## Executive Summary

The Synthesis Engine acts as the "Editor-in-Chief" for processed intelligence. It aggregates structured JSON data from multiple Sectionizer outputs, deduplicates overlapping news across different documents, and uses LLM-based editorial logic to produce a cohesive, professional weekly report.

A key feature of this engine is the Visual Enrichment Layer, which maps imagery to relevant news stories for a higher-impact reader experience.

## System Architecture

The engine operates on a four-stage pipeline:

`Aggregate -> Deduplicate -> Synthesize -> Render`

### Core Components

#### Aggregator

Scans specified directories for all `sectionizer.json` outputs generated during the current cycle.

#### Visual Asset Manager

A dedicated module that matches image metadata, extracted during the Sectionizer phase, to summarized stories.

#### Synthesis Agent

Uses `Gemini 1.5 Flash` for the final editorial pass to ensure:

- smooth transitions
- industry-standard segmenting
- mood mirroring

#### Multi-Channel Renderer

Converts finalized editorial output into:

- Markdown
- HTML for email
- PDF for WhatsApp

## Data Source and Segment Logic

The engine uses the `mappings` array from the Sectionizer output, focusing specifically on entries where `passes_threshold: true`.

News is categorized into professional industry desks.

| Segment | Included Topics |
| --- | --- |
| Launch & Propulsion | Rocket tests, orbital launches, engine milestones |
| Satellite & EO | Earth observation, communication payloads, constellation updates |
| SSA & Defense | Space situational awareness, debris tracking, military space policy |
| Ecosystem & Policy | Funding rounds, IN-SPACe authorizations, and corporate filings such as prospectuses |

## Visual Enrichment Strategy

This engine prioritizes visual storytelling by leveraging metadata associated with each matched section.

### Hero Image Selection

Identify the primary image URL or metadata associated with a specific `doc_id` or `source_path`.

### Dynamic Embedding

Place images immediately after the generated `newsletter_title`.

Example:

`Edelweiss NCD Issue: Key Details...`

### Fallback Logic

If a section lacks a source image, use one of the following:

- a high-quality category placeholder
- the company's official logo

Example placeholder:

- generic satellite image for SSA-related news

### Captions

Generate a one-sentence caption for every image based on the extracted summary and `summary_facts`.

## Implementation Roadmap

### Phase 1: Aggregation and Deduplication

#### Multi-JSON Collection

Build logic to ingest multiple Sectionizer JSON files simultaneously.

#### Deduplication

Implement a "seen URL/path" store to prevent the same document, such as `with_text.pdf`, from being reported multiple times across different Sectionizer runs.

#### Segment Grouping

Organize all matches by section name, for example:

- `Executive`
- `ISSUE RELATED INFORMATION`

### Phase 2: AI Editorial Pass

#### Synthesis Prompt

Design a prompt that:

- maintains the professional tone used in existing summaries
- bridges stories from different documents into a single narrative

#### Threshold Validation

Ensure only sections with a high score or `passes_threshold: true` are included in the final editorial.

### Phase 3: Layout and Rendering

#### Mobile-First Design

Develop a Jinja2 HTML template optimized for mobile email clients such as Outlook and Gmail.

#### Asset Integration

Embed extracted images alongside summarized content to increase engagement.

## Operational Metrics

### Editorial Cohesion

The LLM should bridge stories from different sources into a single industry narrative.

### Cost Efficiency

Synthesis is performed in a single call, with an estimated cost of about `$0.01` per weekly run.

### Visual Quality

`100%` of major stories should be accompanied by either:

- a source image
- a high-quality category placeholder

