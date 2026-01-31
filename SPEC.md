# House Finder Spec

## Purpose

Find rental listings using Firecrawl across major listing sites, filter by LLM-analyzed room photos (windows, natural light, view quality), learn from user feedback over time.

## Architecture

### Core Services

#### 1. Searcher (`searcher.py`)

**Input**: Search criteria (location, beds, baths, price range, available dates, property type)

**Output**: List of listing URLs + extracted metadata

**Responsibilities**:
- Use Firecrawl search API to find rental listings across all major sites (Zillow, Apartments.com, Redfin, Craigslist, etc.)
- Crawl listing pages in parallel (max 3 concurrent requests) to extract structured data (price, beds, baths, photos, description, address, available date)
- Skip and log any URLs that fail to crawl (no retry on crawl failures)
- Deduplicate listings across sites using basic address normalization (St→Street, Apt→#, case-insensitive, trim whitespace)
- Skip listings already in the database (by URL match)
- Cap at 50 listings per run (configurable via `--max-listings`)
- Save crawled data to DB immediately (partial progress is preserved even if later steps fail)
- Return structured listings with all photo URLs
- Rentals of any type: apartments, houses, condos, townhomes

#### 2. Filter (`filter.py`)

**Input**: Listings with photos

**Output**: Scored/filtered listings

**Responsibilities**:
- Send ALL photos from a listing to Claude in a single LLM call (no cap, no pre-filtering)
- LLM identifies which photos are bedrooms and living rooms, then scores each room 1-10 on windows/natural light/not facing a wall
- Photos only — listing description text is NOT included in the LLM prompt
- Pull up to 20 most recent feedback examples from the database to include in the LLM prompt
- Apply filtering logic (see Scoring section below)
- Skip listings with no identifiable bedroom or living room photos
- Retry Claude API calls up to 3 times with exponential backoff on transient errors

**Cold Start**: No filtering is applied until at least 10 feedback examples exist in the database. Before that threshold, all listings with identifiable room photos are passed through unfiltered.

#### 3. Notifier (`notifier.py`)

**Input**: Filtered listings (or all listings during cold start)

**Output**: Email via SendGrid

**Responsibilities**:
- Format all qualifying listings into a single email
- Layout: price and address prominent at top of each listing, 2-3 bedroom/living room photos inline, LLM score and per-room reasoning below
- Listings ordered by average room score (highest first)
- Include feedback links for each listing:
  - "Yes, interested" → `GET /feedback?id=123&vote=yes`
  - "No, not interested" → `GET /feedback?id=123&vote=no`
- Send a summary email even when 0 listings qualify (e.g., "15 listings found, 0 passed filtering") so the user knows the system ran
- Track `emailed_at` timestamp per listing to prevent re-sending
- Only email listings that have not been previously emailed

#### 4. Feedback Handler (`feedback.py`)

**Input**: Listing ID, vote (yes/no), optional categories + free text reason

**Output**: Saved to database

**Responsibilities**:
- Standalone FastAPI server (separate process from CLI)
- `GET /feedback?id=123&vote=yes` → records positive feedback, shows plain text confirmation ("Thanks! Feedback recorded.")
- `GET /feedback?id=123&vote=no` → shows plain text page with feedback form
- Feedback form includes predefined categories (checkboxes) plus optional free text:
  - Too dark
  - Bad view
  - Windows face wall
  - No windows
  - Too small
  - Bad layout
  - Looks dated / run down
  - Poor kitchen
  - Bad neighborhood feel
  - Overpriced
- `POST /feedback` saves vote + selected categories + free text reason
- No authentication — URLs are unauthenticated GET/POST requests
- Hosted locally with ngrok for public access during development
- Associate feedback with listing photos and LLM assessment for future prompt examples

## Scoring

### Per-Room Evaluation

The LLM evaluates each identified bedroom and living room on a 1-10 scale based on:
- **Windows**: Presence and size of windows
- **Natural light**: Visible natural light in the photo
- **Not facing a wall**: Windows should not face a brick wall, alley, or other obstruction

### Listing Pass/Fail Criteria

A listing must satisfy ALL four conditions to pass:

1. **Living room gate**: The living room must score ≥ 7 (hard requirement — if it fails, the listing is rejected regardless of bedroom scores)
2. **Per-room floor**: No individual room (bedroom or living room) can score below 4
3. **Bedroom pass rate**: ≥ 50% of bedrooms must score ≥ 7 (e.g., 3 of 5 bedrooms)
4. **Overall average**: The average score across all scored rooms (bedrooms + living room) must be ≥ 7

### Storage

Room scores are stored as a JSON array on the listing row:
```json
[
  {"room": "living_room", "score": 8, "pass": true, "reasoning": "Large windows, abundant natural light, open sky view"},
  {"room": "bedroom_1", "score": 9, "pass": true, "reasoning": "Floor-to-ceiling windows, south-facing"},
  {"room": "bedroom_2", "score": 5, "pass": false, "reasoning": "Small window facing adjacent building"}
]
```

## Data Model

### Database: SQLAlchemy

No automatic cleanup — all data is retained indefinitely. Old feedback remains useful for the LLM prompt.

### Tables

#### listings
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY | Auto-increment |
| url | TEXT UNIQUE | Listing URL |
| source | TEXT | Site name (zillow, apartments.com, etc.) |
| address | TEXT | Raw address from listing |
| address_normalized | TEXT | Normalized for dedup |
| price | INTEGER | Monthly rent |
| beds | INTEGER | Number of bedrooms |
| baths | REAL | Number of bathrooms |
| property_type | TEXT | apartment, house, condo, townhome |
| available_date | TEXT | Move-in available date (YYYY-MM-DD) |
| photos | TEXT | JSON array of all photo URLs |
| description | TEXT | Listing description |
| room_scores | TEXT | JSON array of per-room scores (see Scoring section) |
| avg_score | REAL | Average across all scored rooms |
| listing_pass | INTEGER | 1 if listing passes all four criteria, 0 otherwise |
| llm_reasoning | TEXT | Overall LLM summary |
| date_found | TEXT | ISO timestamp when first crawled |
| scored_at | TEXT | ISO timestamp when LLM scoring completed (NULL if unscored) |
| emailed_at | TEXT | ISO timestamp when included in an email (NULL if not yet emailed) |

#### feedback
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY | Auto-increment |
| listing_id | INTEGER | Foreign key → listings.id |
| vote | TEXT | 'yes' or 'no' |
| categories | TEXT | JSON array of selected category strings (nullable) |
| reason | TEXT | Free text reason (nullable) |
| created_at | TEXT | ISO timestamp |

#### runs
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY | Auto-increment |
| started_at | TEXT | ISO timestamp |
| completed_at | TEXT | ISO timestamp (NULL if failed) |
| search_criteria | TEXT | JSON of CLI args used |
| listings_found | INTEGER | Total URLs returned by Firecrawl |
| listings_crawled | INTEGER | Successfully crawled |
| listings_scored | INTEGER | Sent to LLM for scoring |
| listings_passed | INTEGER | Passed all four filter criteria |
| listings_emailed | INTEGER | Included in email |
| crawl_failures | INTEGER | URLs that failed to crawl |
| status | TEXT | 'completed', 'partial', 'failed' |
| error | TEXT | Error message if failed (nullable) |

## Entry Point (`main.py`)

### CLI Arguments

| Argument | Description | Required |
|----------|-------------|----------|
| `--location` | Search location (e.g., "San Francisco, CA") | Yes (unless `--url`) |
| `--min-beds` | Minimum bedrooms | No |
| `--max-beds` | Maximum bedrooms | No |
| `--min-baths` | Minimum bathrooms | No |
| `--min-price` | Minimum monthly rent | No |
| `--max-price` | Maximum monthly rent | No |
| `--start-date` | Available from date (YYYY-MM-DD) | No |
| `--end-date` | Available to date (YYYY-MM-DD) | No |
| `--email` | Recipient email address | Yes |
| `--url` | Manually add a single listing URL (bypasses search, runs full pipeline immediately) | No |
| `--max-listings` | Max listings to process per run (default: 50) | No |
| `--dry-run` | Crawl listings but skip LLM scoring and email sending | No |

### Execution Flow

#### Standard Search Run
1. Parse search criteria from CLI arguments
2. **Searcher**: Query Firecrawl search API → crawl pages (3 concurrent) → extract listings → dedup by address → save to DB
3. **Filter**: Load feedback examples (up to 20 most recent) → send all photos per listing to Claude → score per-room → apply four-criteria filter → save scores to DB. Skip this step if `--dry-run` or < 10 feedback examples (cold start).
4. **Notifier**: Collect passed listings not yet emailed → format email (sorted by avg score) → send via SendGrid → update `emailed_at`. Skip if `--dry-run`. Send summary email if 0 passed.
5. **Summary**: Print detailed table to CLI (address, score, pass/fail per listing) + aggregate counts

#### Manual URL Add (`--url`)
1. Crawl the single URL via Firecrawl
2. Score with LLM (same logic as standard run, respects cold start)
3. Send standalone email immediately with results
4. Print result to CLI

### Partial Failure Handling
- Crawled data is saved to DB immediately, even if scoring or emailing fails later
- Next run skips already-crawled URLs and picks up unscored/unemailed listings
- Run status is recorded as 'partial' with error details

## LLM Integration

### Provider
Claude API (Anthropic) — single provider, no abstraction layer.

### Prompt Strategy

Single-pass prompt — the LLM receives ALL photos from a listing and handles room identification + scoring in one call.

```
You are evaluating a rental listing's rooms. Analyze all photos and:
1. Identify which photos show bedrooms and which show the living room
2. Score each bedroom and the living room from 1-10 based on:
   - Window presence and size
   - Natural light visible in the photo
   - View quality (not facing a wall, alley, or obstruction)

Here are examples of what the user has liked and disliked in the past:

LIKED:
{feedback_examples_positive}

DISLIKED (with reasons):
{feedback_examples_negative}

Now evaluate this listing's photos:
{all_photo_urls}

For each identified bedroom and living room, return:
- Room label (living_room, bedroom_1, bedroom_2, etc.)
- Score (1-10)
- One-sentence reasoning

If no bedrooms or living room can be identified in the photos, say so.
```

### Cold Start Behavior
- Feedback examples section is omitted from the prompt when < 10 examples exist
- No filtering is applied — all listings with identifiable room photos pass through
- Once 10+ feedback examples exist, filtering is enabled with the full four-criteria logic

## Feedback Loop

### How Feedback Improves Scoring
- The 20 most recent feedback entries are included in the LLM prompt as liked/disliked examples
- Each example includes the listing's room photos and the user's vote + categories/reason
- Over time, the LLM calibrates to the user's preferences

### Feedback Categories (for "No" votes)
- Too dark
- Bad view
- Windows face wall
- No windows
- Too small
- Bad layout
- Looks dated / run down
- Poor kitchen
- Bad neighborhood feel
- Overpriced

## Configuration

### Environment Variables (`.env` file)
```
FIRECRAWL_API_KEY=...
ANTHROPIC_API_KEY=...
OPENROUTER_API_KEY=...   # optional; set when LLM_PROVIDER=openrouter
OPENROUTER_MODEL=...     # optional; e.g. anthropic/claude-sonnet-4, openai/gpt-4o (default: Claude Sonnet)
LLM_PROVIDER=...         # optional; one of openai, anthropic, openrouter (default: anthropic)
SENDGRID_API_KEY=...
SENDGRID_FROM_EMAIL=...
DATABASE_PATH=./house_finder.db
FEEDBACK_BASE_URL=https://your-ngrok-url.ngrok.io
```

All configuration via `.env` loaded with `python-dotenv`. No separate config file.

## Project Structure

```
house_finder/
├── venv/
├── src/
│   └── house_finder/
│       ├── __init__.py
│       ├── main.py          # CLI entry point (argparse)
│       ├── searcher.py      # Firecrawl search + crawl
│       ├── filter.py        # Claude photo analysis + scoring logic
│       ├── notifier.py      # SendGrid email formatting + sending
│       ├── feedback.py      # FastAPI feedback server (separate process)
│       ├── db.py            # Raw SQLite operations
│       └── address.py       # Address normalization for dedup
├── tests/
├── .env
├── .env.example
├── SPEC.md
├── pyproject.toml
└── requirements.txt
```

## Dependencies

- `firecrawl-py` — Firecrawl search + crawl API
- `anthropic` — Claude API for photo analysis (or use `openai` / `openrouter` via `LLM_PROVIDER`)
- `sendgrid` — Email delivery
- `fastapi` — Feedback endpoint server
- `uvicorn` — ASGI server for FastAPI
- `python-dotenv` — Environment variable loading

## Technical Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| LLM provider | OpenAI, Anthropic, or OpenRouter | Configurable via `LLM_PROVIDER`; OpenRouter allows many models with one API key |
| Photo analysis passes | Single pass | Two passes is excessive; one call handles room ID + scoring |
| Photo cap per listing | None (send all) | Prioritize accuracy over cost |
| LLM description text | Photos only | Descriptions can be misleading marketing copy |
| Database | Raw SQLite | Simple, no ORM overhead, keep forever |
| Email service | SendGrid | Reliable, good API |
| Feedback auth | None | Personal tool, low risk |
| Feedback form | Categories + free text | Structured data for LLM prompt, with flexibility |
| Feedback server | FastAPI, separate process | Clean separation, easy to deploy |
| Hosting | Local + ngrok | Free, good for development |
| Cross-site dedup | Basic address normalization | Catches most dupes without external API |
| Crawl failures | Skip and log | No retry, don't block the run |
| Crawl concurrency | 3 parallel | Conservative to avoid rate limits |
| Cold start | No filtering until 10 examples | Avoids bad early filtering with no calibration |
| Feedback in prompt | Cap at 20 most recent | Controls token usage |
| Listing dedup | Skip if URL in DB | Never re-process same URL |
| Partial failures | Save progress | Crawled data persists, next run resumes |
| Email tracking | Track emailed_at | Prevents duplicate emails |
| Run logging | DB table | Historical analysis and debugging |
| Listings per run | Cap at 50 | Cost control, configurable |
| Dry run | Crawl only | Skip scoring and email |
| CLI output | Detailed table | Per-listing address, score, pass/fail |
| LLM retries | 3x with exponential backoff | Standard resilience for API calls |
| Data cleanup | None | Old feedback is valuable |
| Python version | 3.11+ | Modern features |
| Run mode | Manual CLI now, cron-ready | Design for future automation |
| Property types | All rentals | Apartments, houses, condos, townhomes |
| Date filtering | Move-in / available date | Not listing posted date |
| Email sort | By average score (high first) | Best listings at the top |
| Empty run emails | Send summary | User knows the system ran |
| Room score storage | JSON on listing row | Simple schema, avoids extra table |
| Confirmation page | Plain text | Minimal UI needed |
