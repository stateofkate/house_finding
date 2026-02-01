import base64
import concurrent.futures
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
LLM_TIMEOUT = 60          # seconds per LLM API call
SCORING_WORKERS = 3        # concurrent listings being scored
PER_LISTING_TIMEOUT = 120  # seconds before giving up on a single listing

# Change this to "anthropic" or "openrouter"; env LLM_PROVIDER overrides.
DEFAULT_LLM_PROVIDER = "openrouter"

# OpenRouter model ID (e.g. anthropic/claude-sonnet-4, openai/gpt-4o). Must support vision.
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"


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


def build_prompt(photo_urls: list[str]) -> str:
    """Build the pass 1 prompt: objective room scoring with no feedback influence."""
    return (
        "You are evaluating a rental listing's rooms. Analyze all photos and:\n"
        "1. Identify which photos show bedrooms and which show the living room\n"
        "2. Score each bedroom and the living room from 1-10 based on:\n"
        "   - Window presence and size\n"
        "   - Natural light visible in the photo\n"
        "   - View quality (not facing a wall, alley, or obstruction)\n"
        "\nPhotos are labeled Photo 1, Photo 2, etc. in the order shown above.\n"
        "Now evaluate this listing's photos.\n\n"
        "For each identified bedroom and living room, return a JSON array with objects containing:\n"
        '- "room": room label (living_room, bedroom_1, bedroom_2, etc.)\n'
        '- "photo_index": the 1-based number of the photo that best shows this room\n'
        '- "score": integer 1-10\n'
        '- "reasoning": one-sentence explanation\n\n'
        "If no bedrooms or living room can be identified in the photos, return an empty array [].\n\n"
        "Return ONLY the JSON array, no other text."
    )


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


def build_apartment_eval_prompt(room_scores: list[dict], feedback_examples: list[dict]) -> str:
    """Build the pass 2 prompt: evaluate apartment preference match using feedback."""
    # Format room scores as structured text
    scores_text = ""
    for rs in room_scores:
        reasoning = rs.get("reasoning", "")
        scores_text += f"- {rs['room']}: {rs['score']}/10 — {reasoning}\n"

    # Format feedback examples (text-only, no photos)
    liked = [ex for ex in feedback_examples if ex["vote"] == "yes"]
    disliked = [ex for ex in feedback_examples if ex["vote"] == "no"]

    feedback_text = ""
    if liked:
        feedback_text += "\nLIKED:\n"
        for ex in liked:
            addr = ex.get("address", "Unknown")
            ex_scores = _format_feedback_scores(ex)
            detail = ""
            categories = ex.get("categories") or "[]"
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except json.JSONDecodeError:
                    categories = []
            reason = ex.get("reason") or ""
            cats_str = ", ".join(categories) if categories else ""
            parts = [p for p in [cats_str, reason] if p]
            if parts:
                detail = f": {'; '.join(parts)}"
            feedback_text += f"- {addr} ({ex_scores}){detail}\n"

    if disliked:
        feedback_text += "\nDISLIKED:\n"
        for ex in disliked:
            addr = ex.get("address", "Unknown")
            ex_scores = _format_feedback_scores(ex)
            detail = ""
            categories = ex.get("categories") or "[]"
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except json.JSONDecodeError:
                    categories = []
            reason = ex.get("reason") or ""
            cats_str = ", ".join(categories) if categories else ""
            parts = [p for p in [cats_str, reason] if p]
            if parts:
                detail = f": {'; '.join(parts)}"
            feedback_text += f"- {addr} ({ex_scores}){detail}\n"

    return (
        "You are evaluating whether a rental apartment matches the user's preferences.\n\n"
        "Here are the room scores from an objective evaluation:\n"
        f"{scores_text}\n"
        "The photos of each scored room are shown above (labeled by room name).\n\n"
        "Here are examples of apartments the user has evaluated:\n"
        f"{feedback_text}\n"
        "Based on the user's preferences shown above, would they like this apartment?\n"
        'Return JSON: {"pass": true/false, "reasoning": "one-sentence explanation"}\n\n'
        "Return ONLY the JSON object, no other text."
    )


def _format_feedback_scores(example: dict) -> str:
    """Format room scores from a feedback example as compact text."""
    raw = example.get("room_scores")
    if not raw:
        return "no scores"
    if isinstance(raw, str):
        try:
            scores = json.loads(raw)
        except json.JSONDecodeError:
            return "no scores"
    else:
        scores = raw
    if not isinstance(scores, list):
        return "no scores"
    parts = [f"{rs['room']}: {rs['score']}" for rs in scores if "room" in rs and "score" in rs]
    return ", ".join(parts) if parts else "no scores"


def _parse_eval_response(text: str) -> dict | None:
    """Parse the pass 2 apartment evaluation response: {"pass": bool, "reasoning": str}."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict) and "pass" in result:
            return result
    except json.JSONDecodeError:
        pass
    return None


def call_apartment_eval(room_photos: list[str], prompt_text: str) -> dict | None:
    """Call the configured LLM for pass 2 apartment evaluation.
    Returns {"pass": bool, "reasoning": str} or None on failure."""
    provider = get_provider()
    if provider == "openai":
        return _call_eval_openai(room_photos, prompt_text)
    if provider == "anthropic":
        return _call_eval_anthropic(room_photos, prompt_text)
    if provider == "openrouter":
        return _call_eval_openrouter(room_photos, prompt_text)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def _call_eval_openai(photo_urls: list[str], prompt_text: str) -> dict | None:
    client = get_client()
    content = []
    for i, url in enumerate(photo_urls, 1):
        content.append({"type": "text", "text": f"{_room_label_for_index(i)}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=1024,
                timeout=LLM_TIMEOUT,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_eval_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse apartment eval response: {text[:200]}")
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenAI eval API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenAI apartment eval call")
    return None


def _call_eval_openrouter(photo_urls: list[str], prompt_text: str) -> dict | None:
    client = get_client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    content = []
    photo_count = 0
    for url in photo_urls:
        data_url = _fetch_image_as_data_url(url)
        if data_url is None:
            continue
        photo_count += 1
        content.append({"type": "text", "text": f"Room photo {photo_count}:"})
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    if not content:
        logger.warning("No room images could be fetched for apartment eval")
        return None
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=1024,
                timeout=LLM_TIMEOUT,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_eval_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse OpenRouter eval response: {text[:200]}")
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenRouter eval API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenRouter apartment eval call")
    return None


def _call_eval_anthropic(photo_urls: list[str], prompt_text: str) -> dict | None:
    client = get_client()
    content = []
    for i, url in enumerate(photo_urls, 1):
        content.append({"type": "text", "text": f"Room photo {i}:"})
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": prompt_text})
    messages = [{"role": "user", "content": content}]

    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5-20250514",
                max_tokens=1024,
                timeout=LLM_TIMEOUT,
                messages=messages,
            )
            text = response.content[0].text
            result = _parse_eval_response(text)
            if result is not None:
                return result
            logger.warning(f"Failed to parse Anthropic eval response: {text[:200]}")
            return None
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(f"Anthropic eval API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
        except anthropic.APIConnectionError as e:
            wait = 2 ** (attempt + 1)
            logger.warning(f"Anthropic eval connection error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
            time.sleep(wait)
            continue
    logger.error("Exhausted retries for Anthropic apartment eval call")
    return None


def _room_label_for_index(i: int) -> str:
    """Simple label for eval photos — just numbered since room names are in the prompt text."""
    return f"Room photo {i}"


def _call_openai(photo_urls: list[str], prompt_text: str) -> tuple[list[dict] | None, list[str]]:
    client = get_client()
    content = []
    for i, url in enumerate(photo_urls, 1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                timeout=LLM_TIMEOUT,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_response(text)
            if result is not None:
                return result, photo_urls
            logger.warning(f"Failed to parse OpenAI response: {text[:200]}")
            return None, photo_urls
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenAI API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenAI API call")
    return None, photo_urls


SUPPORTED_IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _is_supported_image_url(url: str) -> bool:
    """Check if a URL points to a supported image type (not SVG, not data: URIs, not HTML pages)."""
    if url.startswith("data:"):
        return url.startswith("data:image/jpeg") or url.startswith("data:image/png") or url.startswith("data:image/gif") or url.startswith("data:image/webp")
    lower = url.split("?")[0].lower()
    if lower.endswith(".svg"):
        return False
    # Reject URLs that look like HTML pages (listing pages mixed into photo arrays)
    if not any(lower.endswith(ext) for ext in SUPPORTED_IMAGE_TYPES):
        # Allow CDN URLs that don't have extensions (e.g. zillowstatic, rdcpix)
        known_image_hosts = ("zillowstatic.com", "rdcpix.com", "googleapis.com")
        if not any(host in lower for host in known_image_hosts):
            return False
    return True


def _fetch_image_as_data_url(url: str) -> str | None:
    """Fetch image from URL and return as data URL (base64). Uses a browser User-Agent to avoid robots.txt blocks that affect API providers."""
    if not _is_supported_image_url(url):
        logger.debug(f"Skipping unsupported image URL: {url[:80]}")
        return None
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


def _call_openrouter(photo_urls: list[str], prompt_text: str) -> tuple[list[dict] | None, list[str]]:
    """Call OpenRouter (OpenAI-compatible API) with a vision-capable model. Images are fetched and sent as base64 to avoid providers hitting listing-site URLs (robots.txt)."""
    client = get_client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    sent_urls = []
    content = []
    for url in photo_urls:
        data_url = _fetch_image_as_data_url(url)
        if data_url is None:
            continue
        sent_urls.append(url)
        content.append({"type": "text", "text": f"Photo {len(sent_urls)}:"})
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    if not content:
        logger.warning("No images could be fetched for OpenRouter call")
        return None, []
    content.append({"type": "text", "text": prompt_text})

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=4096,
                timeout=LLM_TIMEOUT,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = _parse_response(text)
            if result is not None:
                return result, sent_urls
            logger.warning(f"Failed to parse OpenRouter response: {text[:200]}")
            return None, sent_urls
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str or "500" in err_str or "503" in err_str:
                wait = 2 ** (attempt + 1)
                logger.warning(f"OpenRouter API error (attempt {attempt + 1}): {e}. Retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    logger.error("Exhausted retries for OpenRouter API call")
    return None, sent_urls


def _call_anthropic(photo_urls: list[str], prompt_text: str) -> tuple[list[dict] | None, list[str]]:
    client = get_client()
    content = []
    for i, url in enumerate(photo_urls, 1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": prompt_text})
    messages = [{"role": "user", "content": content}]

    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5-20250514",
                max_tokens=4096,
                timeout=LLM_TIMEOUT,
                messages=messages,
            )
            text = response.content[0].text
            result = _parse_response(text)
            if result is not None:
                return result, photo_urls
            logger.warning(f"Failed to parse Claude response: {text[:200]}")
            return None, photo_urls
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
    return None, photo_urls


def call_llm(photo_urls: list[str], prompt_text: str) -> tuple[list[dict] | None, list[str]]:
    """Call the configured LLM (OpenAI, Anthropic, or OpenRouter) to score room photos.
    Returns (room_scores, sent_photo_urls) where sent_photo_urls is the ordered list
    of photo URLs actually sent to the LLM (may differ from input for OpenRouter)."""
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


def _score_one(listing: dict, cold_start: bool, feedback_examples: list[dict]) -> dict | None:
    """Score a single listing (pass 1 + pass 2). Returns the listing dict if it passed, or None."""
    photos_raw = listing.get("photos", "[]")
    try:
        photos = json.loads(photos_raw) if isinstance(photos_raw, str) else photos_raw
    except json.JSONDecodeError:
        photos = []

    if not photos:
        logger.info(f"Listing {listing['id']}: no photos, skipping")
        return None

    # --- Pass 1: Objective room scoring (no feedback) ---
    prompt_text = build_prompt(photos)

    try:
        room_scores, sent_urls = call_llm(photos, prompt_text)
    except Exception as e:
        logger.error(f"Listing {listing['id']}: LLM call failed, skipping: {e}")
        return None

    if not room_scores:
        logger.info(f"Listing {listing['id']}: no rooms identified, skipping")
        return None

    for rs in room_scores:
        idx = rs.get("photo_index")
        if idx and isinstance(idx, int) and 1 <= idx <= len(sent_urls):
            rs["photo_url"] = sent_urls[idx - 1]

    # --- Hard criteria pre-filter ---
    listing_pass, avg_score, reasoning = evaluate_listing(room_scores)

    if not listing_pass and not cold_start:
        # Failed hard criteria — skip pass 2
        logger.info(f"Listing {listing['id']}: failed hard criteria ({reasoning}), skipping pass 2")
        update_listing_scores(
            listing_id=listing["id"],
            room_scores=json.dumps(room_scores),
            avg_score=avg_score,
            listing_pass=False,
            llm_reasoning=reasoning,
        )
        return None

    # --- Pass 2: Apartment preference evaluation (only when feedback exists) ---
    if feedback_examples and listing_pass:
        room_photos = [rs["photo_url"] for rs in room_scores if rs.get("photo_url")]
        if room_photos:
            eval_prompt = build_apartment_eval_prompt(room_scores, feedback_examples)
            print('eval_prompt:', eval_prompt)
            try:
                eval_result = call_apartment_eval(room_photos, eval_prompt)
                print('eval_result:', eval_result)
            except Exception as e:
                logger.error(f"Listing {listing['id']}: apartment eval failed, keeping pass 1 result: {e}")
                eval_result = None

            if eval_result and not eval_result.get("pass"):
                listing_pass = False
                reasoning = f"Preference filter: {eval_result.get('reasoning', '')}"
                logger.info(f"Listing {listing['id']}: failed preference filter ({reasoning})")

    # Cold start: pass everything through
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
        return listing

    return None


def score_listings(listings: list[dict], force_feedback: bool = False) -> list[dict]:
    feedback_count = get_feedback_count()
    cold_start = feedback_count < COLD_START_THRESHOLD and not force_feedback
    logger.info(f"Feedback count: {feedback_count}, cold_start: {cold_start}, force_feedback: {force_feedback}")

    # Feedback only used in pass 2
    feedback_examples = []
    # if not cold_start:
    feedback_examples = get_recent_feedback(limit=10)
    logger.info(f"Loaded {len(feedback_examples)} feedback examples for pass 2")

    passed = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=SCORING_WORKERS) as executor:
        future_to_listing = {
            executor.submit(_score_one, listing, cold_start, feedback_examples): listing
            for listing in listings
        }
        for future in concurrent.futures.as_completed(future_to_listing):
            listing = future_to_listing[future]
            try:
                result = future.result(timeout=PER_LISTING_TIMEOUT)
                if result is not None:
                    passed.append(result)
            except concurrent.futures.TimeoutError:
                logger.warning(f"Listing {listing['id']}: timed out after {PER_LISTING_TIMEOUT}s, skipping")
            except Exception as e:
                logger.error(f"Listing {listing['id']}: unexpected error, skipping: {e}")

    logger.info(f"Scored {len(listings)} listings, {len(passed)} passed")
    return passed
