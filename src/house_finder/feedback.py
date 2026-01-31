import json
import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from house_finder.db import get_listing_by_id, init_db, insert_feedback

logger = logging.getLogger(__name__)

app = FastAPI(title="House Finder Feedback")

CATEGORIES = [
    "Too dark",
    "Bad view",
    "Windows face wall",
    "No windows",
    "Too small",
    "Bad layout",
    "Looks dated / run down",
    "Poor kitchen",
    "Bad neighborhood feel",
    "Overpriced",
]


@app.on_event("startup")
def startup():
    init_db()


@app.get("/feedback")
def feedback_get(id: int = Query(...), vote: str = Query(...)):
    listing = get_listing_by_id(id)
    if not listing:
        return PlainTextResponse("Listing not found.", status_code=404)

    if vote == "yes":
        insert_feedback(listing_id=id, vote="yes")
        return PlainTextResponse(
            "Thanks! Feedback recorded. You voted YES for this listing."
        )

    elif vote == "no":
        checkboxes = ""
        for cat in CATEGORIES:
            safe_cat = cat.replace('"', "&quot;")
            checkboxes += (
                f'<label><input type="checkbox" name="categories" '
                f'value="{safe_cat}"> {cat}</label><br>\n'
            )

        address = listing.get("address", "Unknown")
        form_html = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;max-width:500px;margin:40px auto;">
            <h2>Feedback: {address}</h2>
            <p>You voted <strong>No</strong>. Please tell us why:</p>
            <form method="POST" action="/feedback">
                <input type="hidden" name="listing_id" value="{id}" />
                <input type="hidden" name="vote" value="no" />
                <h3>Categories (select all that apply):</h3>
                {checkboxes}
                <h3>Additional comments (optional):</h3>
                <textarea name="reason" rows="4" cols="40" placeholder="Optional free text..."></textarea>
                <br><br>
                <button type="submit" style="background:#4CAF50;color:white;padding:10px 20px;border:none;border-radius:4px;cursor:pointer;">Submit Feedback</button>
            </form>
        </body>
        </html>
        """
        return HTMLResponse(form_html)

    else:
        return PlainTextResponse("Invalid vote. Use 'yes' or 'no'.", status_code=400)


@app.post("/feedback")
def feedback_post(
    listing_id: int = Form(...),
    vote: str = Form(...),
    categories: list[str] = Form(default=[]),
    reason: str = Form(default=""),
):
    listing = get_listing_by_id(listing_id)
    if not listing:
        return PlainTextResponse("Listing not found.", status_code=404)

    categories_json = json.dumps(categories) if categories else None
    reason_text = reason.strip() if reason else None

    insert_feedback(
        listing_id=listing_id,
        vote=vote,
        categories=categories_json,
        reason=reason_text,
    )

    return PlainTextResponse("Thanks! Your feedback has been recorded.")
