# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

House Finder is a rental listing search and filtering system. It scrapes listings via Firecrawl, analyzes room photos using vision LLMs (scoring windows/natural light/view quality 1-10), learns from user feedback, and sends results via email.

## Commands

### Install dependencies
```
pip install -e .
```

### Run a search
```
house-finder --location "San Francisco, CA" --max-price 3500 --email user@example.com
```

### Add a single listing manually
```
house-finder --url https://listing-url.com --email user@example.com
```

### Run feedback server (separate process)
```
uvicorn house_finder.feedback:app --reload
```

### CLI flags
- `--dry-run`: Crawl only, skip LLM scoring and email
- `--no-email`: Score but print to terminal instead of emailing
- `--max-listings N`: Cap listings per run (default: 50)
- `--save-listings PATH`: Save crawled listings to JSON
- `--from-file PATH`: Load listings from JSON instead of searching

## Architecture

**Pipeline**: `main.py` orchestrates a 4-step pipeline: Search → Score → Email → Feedback

| Module | Responsibility |
|--------|---------------|
| `main.py` | CLI entry point, orchestrates pipeline |
| `searcher.py` | Firecrawl search API + parallel crawling (3 concurrent) |
| `filter.py` | Sends all listing photos to vision LLM, scores rooms 1-10 |
| `notifier.py` | Formats HTML email with scores/photos, sends via SendGrid |
| `feedback.py` | Standalone FastAPI server for collecting user votes |
| `db.py` | SQLAlchemy ORM — listings, feedback, runs tables |
| `address.py` | Address normalization for cross-site deduplication |

### LLM Providers

Configurable via `LLM_PROVIDER` env var: `anthropic` (default), `openai`, or `openrouter`. OpenRouter allows using multiple vision models with a single API key. Model selection via `OPENROUTER_MODEL`.

### Scoring criteria (all must pass)

1. Living room score >= 7
2. No room below 4
3. >= 50% of bedrooms >= 7
4. Overall average >= 7

### Cold start

No filtering until 10+ feedback examples exist. Before that, all listings with identifiable room photos pass through.

### Feedback loop

Up to 20 most recent feedback examples are injected into the LLM prompt as liked/disliked examples to calibrate scoring over time.

## Configuration

All config via `.env` (see `.env.example`). Key variables:
- `FIRECRAWL_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` — API keys
- `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL` — email delivery
- `DATABASE_PATH` — SQLite path (default: `./house_finder.db`)
- `FEEDBACK_BASE_URL` — public URL for feedback links (ngrok during dev)
- `LLM_PROVIDER`, `OPENROUTER_MODEL` — LLM provider selection

## Key Design Decisions

- Photos only sent to LLM (no listing description text — avoids misleading marketing copy)
- All photos sent per listing (no cap) — accuracy over cost
- Crawled data saved to DB immediately — partial failure recovery on next run
- No authentication on feedback endpoints (personal tool)
- SQLite with no cleanup — old feedback remains useful
