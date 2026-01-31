import base64
import json
import logging
import os
import re
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import anthropic
from openai import OpenAI

from house_finder.db import (
    get_feedback_count,
    get_recent_feedback,
    update_listing_scores,
)

logger = logging.getLogger(__name__)

COLD_START_THRESHOLD = 10
MAX_RETRIES = 3

# Change this to "anthropic" or "openrouter"; env LLM_PROVIDER overrides.
DEFAULT_LLM_PROVIDER = "openrouter"

# OpenRouter model ID (e.g. anthropic/claude-sonnet-4, openai/gpt-4o). Must support vision.
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4"


def get_provider() -> str:
    return os.environ.get("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).lower()


def get_client():
    """Return the LLM client for the active provider. Swap provider via DEFAULT_LLM_PROVIDER or env LLM_PROVIDER."""
    provider = get_provider()
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set; required when LLM_PROVIDER=openai")
        return OpenAI()
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set; required when LLM_PROVIDER=anthropic")
        return anthropic.Anthropic()
    if provider == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("OPENROUTER_API_KEY is not set; required when LLM_PROVIDER=openrouter")
        return OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use 'openai', 'anthropic', or 'openrouter'.")


def build_prompt(photo_urls: list[str], feedback_examples: list[dict]) -> str:
    prompt_text = (
        "You are evaluating a rental listing's rooms. Analyze all photos and:\n"
        "1. Identify which photos show bedrooms and which show the living room\n"
        "2. Score each bedroom and the living room from 1-10 based on:\n"
        "   - Window presence and size\n"
        "   - Natural light visible in the photo\n"
        "   - View quality (not facing a wall, alley, or obstruction)\n"
    )

    if feedback_examples:
        liked = [ex for ex in feedback_examples if ex["vote"] == "yes"]
        disliked = [ex for ex in feedback_examples if ex["vote"] == "no"]

        if liked:
            prompt_text += "\nHere are examples of what the user has liked in the past:\n"
            for ex in liked:
                prompt_text += f"- {ex.get('address', 'Unknown')}\n"

        if disliked:
            prompt_text += "\nHere are examples of what the user has disliked (with reasons):\n"
            for ex in disliked:
                categories = ex.get("categories") or "[]"
                if isinstance(categories, str):
                    try:
                        categories = json.loads(categories)
                    except json.JSONDecodeError:
                        categories = []
                reason = ex.get("reason") or ""
                cats_str = ", ".join(categories) if categories else ""
                parts = [p for p in [cats_str, reason] if p]
                prompt_text += f"- {ex.get('address', 'Unknown')}: {'; '.join(parts)}\n"

    prompt_text += (
        "\nNow evaluate this listing's photos.\n\n"
        "For each identified bedroom and living room, return a JSON array with objects containing:\n"
        '- "room": room label (living_room, bedroom_1, bedroom_2, etc.)\n'
        '- "score": integer 1-10\n'
        '- "reasoning": one-sentence explanation\n\n'
        "If no bedrooms or living room can be identified in the photos, return an empty array [].\n\n"
        "Return ONLY the JSON array, no other text."
    )

    return prompt_text


def _parse_response(text: str) -> list[dict] | None:
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return None


def _call_openai(photo_urls: list[str], prompt_text: str) -> list[dict] | None:
    client = get_client()
    content = []
    for url in photo_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse OpenAI response: {text[:200]}")
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenAI API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenAI API call")
    return None


def _fetch_image_as_data_url(url: str) -> str | None:
    """Fetch image from URL and return as data URL (base64). Uses a browser User-Agent to avoid robots.txt blocks that affect API providers."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            b64 = base64.standard_b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{b64}"
    except (URLError, HTTPError, OSError) as e:
        logger.warning(f"Failed to fetch image {url[:80]!r}: {e}")
        return None


def _call_openrouter(photo_urls: list[str], prompt_text: str) -> list[dict] | None:
    """Call OpenRouter (OpenAI-compatible API) with a vision-capable model. Images are fetched and sent as base64 to avoid providers hitting listing-site URLs (robots.txt)."""
    client = get_client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    content = []
    for url in photo_urls:
        data_url = _fetch_image_as_data_url(url)
        if data_url is None:
            continue
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    if not content:
        logger.warning("No images could be fetched for OpenRouter call")
        return None
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse OpenRouter response: {text[:200]}")
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenRouter API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenRouter API call")
    return None


def _call_anthropic(photo_urls: list[str], prompt_text: str) -> list[dict] | None:
    client = get_client()
    content = []
    for url in photo_urls:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": prompt_text})
    messages = [{"role": "user", "content": content}]

    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5-20250514",
                max_tokens=4096,
                messages=messages,
            )
            text = response.content[0].text
            result = _parse_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse Claude response: {text[:200]}")
            return None
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(f"Claude API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
        except anthropic.APIConnectionError as e:
            wait = 2 ** (attempt + 1)
            logger.warning(f"Connection error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
            time.sleep(wait)
            continue
    logger.error("Exhausted retries for Claude API call")
    return None


def call_llm(photo_urls: list[str], prompt_text: str) -> list[dict] | None:
    """Call the configured LLM (OpenAI, Anthropic, or OpenRouter) to score room photos."""
    provider = get_provider()
    if provider == "openai":
        return _call_openai(photo_urls, prompt_text)
    if provider == "anthropic":
        return _call_anthropic(photo_urls, prompt_text)
    if provider == "openrouter":
        return _call_openrouter(photo_urls, prompt_text)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def evaluate_listing(room_scores: list[dict]) -> tuple[bool, float, str]:
    if not room_scores:
        return False, 0.0, "No identifiable rooms"

    living_rooms = [r for r in room_scores if r["room"] == "living_room"]
    bedrooms = [r for r in room_scores if r["room"].startswith("bedroom")]
    all_rooms = living_rooms + bedrooms

    if not all_rooms:
        return False, 0.0, "No identifiable rooms"

    avg_score = sum(r["score"] for r in all_rooms) / len(all_rooms)

    # Mark each room pass/fail for storage
    for r in room_scores:
        r["pass"] = r["score"] >= 7

    # Criterion 1: Living room >= 7
    if not living_rooms:
        return False, avg_score, "No living room identified"
    if living_rooms[0]["score"] < 7:
        return False, avg_score, f"Living room score {living_rooms[0]['score']} < 7"

    # Criterion 2: No room below 4
    for r in all_rooms:
        if r["score"] < 4:
            return False, avg_score, f"{r['room']} score {r['score']} < 4 (floor)"

    # Criterion 3: >= 50% bedrooms >= 7
    if bedrooms:
        passing_bedrooms = sum(1 for b in bedrooms if b["score"] >= 7)
        if passing_bedrooms / len(bedrooms) < 0.5:
            return (
                False,
                avg_score,
                f"Only {passing_bedrooms}/{len(bedrooms)} bedrooms >= 7 (need 50%)",
            )

    # Criterion 4: Overall average >= 7
    if avg_score < 7:
        return False, avg_score, f"Average score {avg_score:.1f} < 7"

    return True, avg_score, "Passed all criteria"


def score_listings(listings: list[dict]) -> list[dict]:
    feedback_count = get_feedback_count()
    cold_start = feedback_count < COLD_START_THRESHOLD

    feedback_examples = []
    if not cold_start:
        feedback_examples = get_recent_feedback(limit=20)

    passed = []

    for listing in listings:
        photos_raw = listing.get("photos", "[]")
        print('photos_raw:', photos_raw)
        try:
            photos = json.loads(photos_raw) if isinstance(photos_raw, str) else photos_raw
        except json.JSONDecodeError:
            photos = []

        if not photos:
            logger.info(f"Listing {listing['id']}: no photos, skipping")
            continue

        prompt_text = build_prompt(photos, feedback_examples)
        print('prompt_text:', prompt_text)
        room_scores = call_llm(photos, prompt_text)

        if not room_scores:
            logger.info(f"Listing {listing['id']}: no rooms identified, skipping")
            continue

        listing_pass, avg_score, reasoning = evaluate_listing(room_scores)

        if cold_start:
            listing_pass = True
            reasoning = "Cold start: passed without filtering"

        update_listing_scores(
            listing_id=listing["id"],
            room_scores=json.dumps(room_scores),
            avg_score=avg_score,
            listing_pass=listing_pass,
            llm_reasoning=reasoning,
        )

        if listing_pass:
            listing["room_scores"] = json.dumps(room_scores)
            listing["avg_score"] = avg_score
            listing["listing_pass"] = 1
            listing["llm_reasoning"] = reasoning
            passed.append(listing)

    logger.info(f"Scored {len(listings)} listings, {len(passed)} passed")
    return passed
