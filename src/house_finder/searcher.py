import asyncio
import json
import logging
import os
from urllib.parse import urlparse

from firecrawl import Firecrawl
from pydantic import BaseModel, Field

from house_finder.address import normalize_address
from house_finder.db import insert_listing, listing_exists, listing_exists_by_address

logger = logging.getLogger(__name__)

EXCLUDED_SITES = ["facebook", "yelp", "craigslist","tiktok","quora"]


class ListingSchema(BaseModel):
    address: str | None = Field(None, description="Full street address of the property")
    price: int | None = Field(None, description="Monthly rent in dollars (number only)")
    beds: int | None = Field(None, description="Number of bedrooms")
    baths: float | None = Field(None, description="Number of bathrooms")
    property_type: str | None = Field(
        None, description="Type: apartment, house, condo, or townhome"
    )
    available_date: str | None = Field(
        None, description="Move-in available date in YYYY-MM-DD format"
    )
    description: str | None = Field(None, description="Listing description text")


def _get_client() -> Firecrawl:
    return Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])


def detect_source(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    domain = domain.removeprefix("www.")
    for site in ["zillow", "apartments", "redfin", "craigslist", "trulia", "realtor", "hotpads"]:
        if site in domain:
            return site
    return domain


def build_search_query(criteria: dict) -> str:
    parts = ["rental listings"]
    if criteria.get("location"):
        parts.append(f"in {criteria['location']}")
    if criteria.get("min_beds"):
        parts.append(f"{criteria['min_beds']}+ bedrooms")
    elif criteria.get("max_beds"):
        parts.append(f"{criteria['max_beds']} bedrooms")
    if criteria.get("max_price"):
        parts.append(f"under ${criteria['max_price']}")
    elif criteria.get("min_price"):
        parts.append(f"from ${criteria['min_price']}")
    parts.append("allinurl:zillow")
    return " ".join(parts)


def extract_listing_data(scrape_result, url: str) -> dict | None:
    try:
        data = {}

        # Extract structured fields from JSON extraction
        json_data = getattr(scrape_result, "json", None)
        if json_data and isinstance(json_data, dict):
            data["address"] = json_data.get("address")
            data["price"] = json_data.get("price")
            data["beds"] = json_data.get("beds")
            data["baths"] = json_data.get("baths")
            data["property_type"] = json_data.get("property_type")
            data["available_date"] = json_data.get("available_date")
            data["description"] = json_data.get("description")

        # Extract image URLs
        images = getattr(scrape_result, "images", None) or []
        data["photos"] = json.dumps(images)

        data["url"] = url
        data["source"] = detect_source(url)

        # Normalize address for dedup
        if data.get("address"):
            data["address_normalized"] = normalize_address(data["address"])

        return data
    except Exception as e:
        logger.error(f"Failed to extract listing data from {url}: {e}")
        return None


async def _crawl_one(fc: Firecrawl, url: str, semaphore: asyncio.Semaphore) -> dict | None:
    async with semaphore:
        try:
            result = await asyncio.to_thread(
                fc.scrape,
                url,
                formats=[
                    {
                        "type": "json",
                        "schema": ListingSchema.model_json_schema(),
                        "prompt": "Extract rental listing details: address, monthly rent price, bedrooms, bathrooms, property type, available date, and description.",
                        
                    },
                    "images",
                ],
            )
            return extract_listing_data(result, url)
        except Exception as e:
            logger.warning(f"Crawl failed for {url}: {e}")
            return None


async def run_search(
    criteria: dict, run_id: int, max_urls: int = 5
) -> list[dict]:
    fc = _get_client()
    query = build_search_query(criteria)
    logger.info(f"Searching: {query}")

    try:
        search_results = await asyncio.to_thread(fc.search, query, limit=max_urls)
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []

    # Extract URLs from search results (Firecrawl returns .web = list of SearchResultWeb with .url)
    urls = []
    web_results = getattr(search_results, "web", None)
    if web_results:
        for item in web_results:
            url = item.get("url") if isinstance(item, dict) else getattr(item, "url", None)
            if url:
                urls.append(url)

    logger.info(f"Found {len(urls)} URLs from search")

    # Filter out already-known URLs
    new_urls = [u for u in urls if not listing_exists(u)]
    logger.info(f"{len(new_urls)} new URLs after dedup")
    cleaned_urls = [u for u in new_urls if not any(site in u for site in EXCLUDED_SITES)]
    logger.info(f"{len(cleaned_urls)} URLs after excluding excluded sites")

    # Crawl in parallel with semaphore; process and log each result as it completes
    semaphore = asyncio.Semaphore(3)
    tasks = [_crawl_one(fc, url, semaphore) for url in cleaned_urls[:max_urls]]
    crawled = []
    crawl_failures = 0
    completed = 0
    for coro in asyncio.as_completed(tasks):
        completed += 1
        listing_data = await coro
        if listing_data is None:
            crawl_failures += 1
            logger.warning(f"[{completed}/{len(tasks)}] Crawl failed for one URL")
            continue

        # Address dedup
        normalized = listing_data.get("address_normalized")
        # if normalized and listing_exists_by_address(normalized):
        #     logger.info(f"[{completed}/{len(tasks)}] Skipping duplicate address: {listing_data.get('address')}")
        #     continue

        listing_id = insert_listing(listing_data)
        listing_data["id"] = listing_id
        crawled.append(listing_data)
        addr = listing_data.get("address") or listing_data.get("url", "")[:50]
        logger.info(f"[{completed}/{len(tasks)}] Crawled: {addr}")

    logger.info(f"Crawled {len(crawled)} listings, {crawl_failures} failures")
    return crawled


async def crawl_single_url(url: str, run_id: int) -> dict | None:
    fc = _get_client()
    semaphore = asyncio.Semaphore(1)
    listing_data = await _crawl_one(fc, url, semaphore)
    if listing_data is None:
        return None

    listing_id = insert_listing(listing_data)
    listing_data["id"] = listing_id
    return listing_data
