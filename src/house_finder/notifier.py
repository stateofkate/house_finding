import json
import logging
import os

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Content, Mail, MimeType

from house_finder.db import mark_listing_emailed

logger = logging.getLogger(__name__)


def _get_client() -> SendGridAPIClient:
    return SendGridAPIClient(os.environ["SENDGRID_API_KEY"])


def _get_from_email() -> str:
    return os.environ["SENDGRID_FROM_EMAIL"]


def _get_feedback_base_url() -> str:
    return os.environ.get("FEEDBACK_BASE_URL", "http://localhost:8000")


def format_listing_html(listing: dict) -> str:
    address = listing.get("address") or "Unknown address"
    price = listing.get("price")
    price_str = f"${price:,}/mo" if price else "Price N/A"
    avg_score = listing.get("avg_score", 0)
    listing_id = listing["id"]

    room_scores = listing.get("room_scores", "[]")
    if isinstance(room_scores, str):
        try:
            room_scores = json.loads(room_scores)
        except json.JSONDecodeError:
            room_scores = []

    # Show first 3 photos
    photos_raw = listing.get("photos", "[]")
    if isinstance(photos_raw, str):
        try:
            all_photos = json.loads(photos_raw)
        except json.JSONDecodeError:
            all_photos = []
    else:
        all_photos = photos_raw
    display_photos = all_photos[:3]

    photos_html = ""
    for photo_url in display_photos:
        photos_html += f'<img src="{photo_url}" style="max-width:300px;margin:5px;" />\n'

    # Room scores table
    scores_html = ""
    for rs in room_scores:
        icon = "&#9989;" if rs.get("pass") else "&#10060;"
        scores_html += (
            f"<tr><td>{rs.get('room', '')}</td>"
            f"<td>{rs.get('score', '')}/10 {icon}</td>"
            f"<td>{rs.get('reasoning', '')}</td></tr>\n"
        )

    feedback_base = _get_feedback_base_url()
    yes_url = f"{feedback_base}/feedback?id={listing_id}&vote=yes"
    no_url = f"{feedback_base}/feedback?id={listing_id}&vote=no"

    return f"""
    <div style="border:1px solid #ccc;padding:15px;margin:15px 0;border-radius:8px;">
        <h2 style="margin:0;">{price_str} &mdash; {address}</h2>
        <p style="color:#666;">Avg score: {avg_score:.1f}/10 | {listing.get('beds', '?')} bed / {listing.get('baths', '?')} bath</p>
        <div>{photos_html}</div>
        <table style="border-collapse:collapse;margin:10px 0;">
            <tr><th>Room</th><th>Score</th><th>Reasoning</th></tr>
            {scores_html}
        </table>
        <p>{listing.get('llm_reasoning', '')}</p>
        <p>
            <a href="{yes_url}" style="background:#4CAF50;color:white;padding:8px 16px;text-decoration:none;border-radius:4px;margin-right:10px;">Yes, interested</a>
            <a href="{no_url}" style="background:#f44336;color:white;padding:8px 16px;text-decoration:none;border-radius:4px;">No, not interested</a>
        </p>
        <p style="font-size:0.8em;color:#999;">
            <a href="{listing.get('url', '#')}">{listing.get('url', '')}</a>
        </p>
    </div>
    """


def build_email_html(listings: list[dict], summary_stats: dict) -> str:
    found = summary_stats.get("listings_found", 0)
    passed = len(listings)

    header = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
        <h1>House Finder Results</h1>
        <p>{found} listings found, {passed} passed filtering.</p>
    """

    if not listings:
        header += "<p>No listings met the filtering criteria this run.</p>"
        return header + "</div>"

    body = ""
    for listing in listings:
        body += format_listing_html(listing)

    return header + body + "</div>"


def send_notification(
    to_email: str,
    listings: list[dict],
    summary_stats: dict,
    run_id: int,
):
    listings_sorted = sorted(
        listings, key=lambda x: x.get("avg_score", 0), reverse=True
    )

    html_body = build_email_html(listings_sorted, summary_stats)

    subject = f"House Finder: {len(listings_sorted)} listings"
    if not listings_sorted:
        subject = "House Finder: No qualifying listings this run"

    message = Mail(
        from_email=_get_from_email(),
        to_emails=to_email,
        subject=subject,
        html_content=Content(MimeType.html, html_body),
    )

    try:
        sg = _get_client()
        response = sg.send(message)
        logger.info(f"Email sent: status {response.status_code}")

        for listing in listings_sorted:
            mark_listing_emailed(listing["id"])

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise
