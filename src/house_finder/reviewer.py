import json
import logging
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
from pydantic import BaseModel

from house_finder.db import insert_feedback, mark_listing_reviewed
from house_finder.feedback import CATEGORIES

logger = logging.getLogger(__name__)

REVIEW_PORT = 8111
TEMPLATE_PATH = Path(__file__).parent / "templates" / "review.html"


class FeedbackPayload(BaseModel):
    listing_id: int
    vote: str
    categories: list[str] = []
    reason: str = ""


def _prepare_listings(listings: list[dict]) -> list[dict]:
    """Parse JSON string fields and add display helpers."""
    prepared = []
    for listing in sorted(listings, key=lambda x: x.get("avg_score", 0), reverse=True):
        entry = dict(listing)

        # Parse photos JSON
        photos = entry.get("photos", "[]")
        if isinstance(photos, str):
            try:
                photos = json.loads(photos)
            except (json.JSONDecodeError, TypeError):
                photos = []
        entry["photos"] = photos if isinstance(photos, list) else []

        # Parse room_scores JSON
        scores = entry.get("room_scores", "[]")
        if isinstance(scores, str):
            try:
                scores = json.loads(scores)
            except (json.JSONDecodeError, TypeError):
                scores = []
        entry["room_scores"] = scores if isinstance(scores, list) else []

        # Price display string
        price = entry.get("price")
        entry["price_str"] = f"${price:,}/mo" if price else "Price N/A"

        prepared.append(entry)
    return prepared


def _render_page(listings: list[dict], run_stats: dict) -> str:
    """Render the review HTML template."""
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    template = Template(template_text)
    return template.render(
        listings=listings,
        stats=run_stats,
        categories=CATEGORIES,
    )


def create_review_app(
    listings: list[dict],
    run_stats: dict,
    feedback_state: dict[int, str],
) -> FastAPI:
    """Create a FastAPI app pre-loaded with listings to review.

    feedback_state is a shared dict (listing_id -> vote) that the caller
    can read after the server shuts down to get the final review count.
    """
    app = FastAPI(title="House Finder Review")
    prepared = _prepare_listings(listings)
    page_html = _render_page(prepared, run_stats)

    @app.get("/", response_class=HTMLResponse)
    def review_page():
        return page_html

    @app.post("/api/feedback")
    def submit_feedback(payload: FeedbackPayload):
        categories_json = json.dumps(payload.categories) if payload.categories else None
        reason_text = payload.reason.strip() if payload.reason else None
        insert_feedback(
            listing_id=payload.listing_id,
            vote=payload.vote,
            categories=categories_json,
            reason=reason_text,
        )
        mark_listing_reviewed(payload.listing_id)
        feedback_state[payload.listing_id] = payload.vote
        return JSONResponse({"status": "ok", "listing_id": payload.listing_id, "vote": payload.vote})

    @app.get("/api/progress")
    def get_progress():
        return JSONResponse({"total": len(prepared), "reviewed": len(feedback_state)})

    @app.post("/api/done")
    def done_reviewing():
        app.state.shutdown_event.set()
        return JSONResponse({"status": "done"})

    return app


def run_review(listings: list[dict], run_stats: dict) -> dict:
    """Launch local review server, open browser, wait for user to finish."""
    if not listings:
        print("\n  No listings passed scoring. Nothing to review.\n")
        return {"reviewed": 0, "total": 0}

    feedback_state: dict[int, str] = {}
    app = create_review_app(listings, run_stats, feedback_state)
    shutdown_event = threading.Event()
    app.state.shutdown_event = shutdown_event

    config = uvicorn.Config(app, host="127.0.0.1", port=REVIEW_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    time.sleep(0.5)
    url = f"http://127.0.0.1:{REVIEW_PORT}"
    webbrowser.open(url)

    print(f"\n  Review page opened in browser: {url}")
    print(f"  Review {len(listings)} listing(s), then click 'Finish Review'.")
    print(f"  Press Ctrl+C to skip review.\n")

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("Review interrupted by user.")
        print("\n  Review interrupted.\n")

    server.should_exit = True
    server_thread.join(timeout=3)

    return {"reviewed": len(feedback_state), "total": len(listings)}
