# Webpages Hoarder Technical Notes

## Overview

The webpages hoarder is a deterministic Scrapy-based source. It does not use an LLM for extraction and it does not use ADK session state to drive scraping or parsing decisions.

The source does three things:

1. Downloads configured pages with Scrapy.
2. Extracts article or page text using selectors defined in `config.json`.
3. Computes `content_sha256` hashes and stores them in `.output/webpages_state.json` for later duplicate checks.

## Config Schema

Each page entry in `config.json` supports:

```json
{
  "company": "Digantara",
  "url": "https://www.example.com/newsroom",
  "page_name": "Newsroom",
  "selector_type": "css",
  "article_selector": "article",
  "title_selector": "h2::text",
  "link_selector": "a::attr(href)",
  "text_selector": "p::text",
  "published_at_selector": "time::text, [datetime]::attr(datetime)",
  "page_content_selector": "main",
  "meta": {
    "sector": "space"
  }
}
```

Field behavior:

- `selector_type`: `css` or `xpath`. Applies to all configured selectors for that page.
- `article_selector`: Selects each news/article container. If omitted or no matches are found, the agent falls back to a page-level record.
- `title_selector`: Extracts article title text.
- `link_selector`: Extracts the article URL. Relative URLs are resolved against the page URL.
- `text_selector`: Extracts article body/summary text.
- `published_at_selector`: Extracts article date text.
- `page_content_selector`: Extracts meaningful page text when no article elements are found.

## Output Records

Each extracted page/article becomes one output item with fields including:

- `path`
- `name`
- `company`
- `pageUrl`
- `pageTitle`
- `pageSummary`
- `contentText`
- `publishedAt`
- `sourceType`
- `scrapeStatus`
- `scrapeError`
- `content_sha256`
- `isDuplicate`
- `meta`

`content_sha256` is computed from normalized extracted text. For article records it is derived from title, published date, and extracted text.

## Duplicate Tracking

The agent persists crawl state to `.output/webpages_state.json`.

Stored per configured page:

- `page_hash`: hash of the fetched page HTML text
- `known_hashes`: all previously seen `content_sha256` values
- `last_crawled_at`: timestamp of the latest successful crawl

During later runs:

- if a newly extracted record hash already exists in `known_hashes`, the record is marked with `isDuplicate: true`
- otherwise it is treated as newly observed content and its hash is added to the state file

## Pipeline Contract

This source no longer uses ADK state for crawl inputs or parsing logic.

For compatibility with the rest of the current pipeline, the final extracted results are still appended into the shared `file_list` handoff consumed by the screener. If you want the hoarder pipeline to become fully state-free end-to-end, the screener and standardizer will also need to stop reading from `ctx.session.state`.
