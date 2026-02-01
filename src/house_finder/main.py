import argparse
import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from house_finder.db import complete_run, create_run, get_listing_ids_with_feedback, init_db, insert_listing, update_run
from house_finder.filter import score_listings
from house_finder.notifier import send_notification
from house_finder.searcher import crawl_single_url, run_search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="House Finder: Search, score, and notify for rental listings."
    )
    parser.add_argument(
        "--location", type=str, help="Search location (e.g., 'San Francisco, CA')"
    )
    parser.add_argument("--min-beds", type=int, default=None)
    parser.add_argument("--max-beds", type=int, default=None)
    parser.add_argument("--min-baths", type=int, default=None)
    parser.add_argument("--min-price", type=int, default=None)
    parser.add_argument("--max-price", type=int, default=None)
    parser.add_argument(
        "--start-date", type=str, default=None, help="Available from date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date", type=str, default=None, help="Available to date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        help="Recipient email address (if omitted, opens local browser review instead)",
    )
    parser.add_argument(
        "--url", type=str, default=None, help="Manually add a single listing URL"
    )
    parser.add_argument(
        "--max-listings",
        type=int,
        default=50,
        help="Max listings to process per run (default: 50)",
    )
    parser.add_argument(
        "--max-score",
        type=int,
        default=5,
        help="Max listings to send to LLM for scoring (default: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl only, skip scoring and email",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Score listings but skip sending email; output results to terminal",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        nargs="?",
        const="saved_listings.json",
        metavar="PATH",
        help="Load listings from JSON file instead of crawling (no Firecrawl). Default: saved_listings.json",
    )
    parser.add_argument(
        "--save-listings",
        type=str,
        metavar="PATH",
        help="After crawling, save listing data to this JSON file for later --from-file runs",
    )
    parser.add_argument(
        "--no-cold-start",
        action="store_true",
        help="Use feedback for scoring even with fewer than 10 feedback entries",
    )
    parser.add_argument(
        "--append-listings",
        type=str,
        nargs="?",
        const="demo/starting_saved_listings.json",
        metavar="PATH",
        help="Append new listings to an existing JSON file (deduped by URL). Default: demo/starting_saved_listings.json",
    )

    args = parser.parse_args()

    if not args.url and not args.location and not args.from_file:
        parser.error("Either --location, --url, or --from-file is required.")

    return args


def load_listings_from_file(path: str) -> list[dict]:
    """Load listing dicts from JSON file and ensure each has a DB id (insert if needed)."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raw = [raw]
    listings = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        listing_id = insert_listing(item)
        item["id"] = listing_id
        listings.append(item)
    return listings


def _normalize_listing(listing: dict) -> dict:
    """Extract only the fields needed for saved listing files."""
    keys = (
        "url", "source", "address", "address_normalized", "price", "beds", "baths",
        "property_type", "available_date", "photos", "description",
    )
    return {k: listing.get(k) for k in keys if k in listing}


def save_listings_to_file(listings: list[dict], path: str) -> None:
    """Save listing dicts to JSON file for later --from-file runs."""
    out = [_normalize_listing(L) for L in listings]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    logger.info("Saved %d listings to %s", len(out), path)


def append_listings_to_file(listings: list[dict], path: str) -> None:
    """Append new listings to an existing JSON file, skipping duplicates by URL."""
    existing = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = [existing]

    existing_urls = {item.get("url") for item in existing}
    new = [_normalize_listing(L) for L in listings if L.get("url") not in existing_urls]

    if not new:
        logger.info("No new listings to append to %s (all %d already present)", path, len(listings))
        return

    existing.extend(new)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    logger.info("Appended %d new listings to %s (%d total)", len(new), path, len(existing))


def filter_by_criteria(listings: list[dict], criteria: dict) -> list[dict]:
    """Drop listings whose crawled beds/baths/price don't match the search criteria."""
    filtered = []
    for listing in listings:
        beds = listing.get("beds")
        baths = listing.get("baths")
        price = listing.get("price")

        if criteria.get("min_beds") and (beds is None or beds < criteria["min_beds"]):
            logger.info(
                "Filtered out %s: %s beds (need %d+)",
                listing.get("address") or listing.get("url", "")[:50],
                beds,
                criteria["min_beds"],
            )
            continue
        if criteria.get("max_beds") and beds is not None and beds > criteria["max_beds"]:
            logger.info(
                "Filtered out %s: %d beds (max %d)",
                listing.get("address") or listing.get("url", "")[:50],
                beds,
                criteria["max_beds"],
            )
            continue
        if criteria.get("min_baths") and (baths is None or baths < criteria["min_baths"]):
            logger.info(
                "Filtered out %s: %s baths (need %d+)",
                listing.get("address") or listing.get("url", "")[:50],
                baths,
                criteria["min_baths"],
            )
            continue
        if criteria.get("max_price") and price is not None and price > criteria["max_price"]:
            logger.info(
                "Filtered out %s: $%d (max $%d)",
                listing.get("address") or listing.get("url", "")[:50],
                price,
                criteria["max_price"],
            )
            continue
        if criteria.get("min_price") and (price is None or price < criteria["min_price"]):
            logger.info(
                "Filtered out %s: $%s (min $%d)",
                listing.get("address") or listing.get("url", "")[:50],
                price,
                criteria["min_price"],
            )
            continue
        filtered.append(listing)

    if len(filtered) < len(listings):
        logger.info(
            "Criteria filter: %d -> %d listings", len(listings), len(filtered)
        )
    return filtered


def print_summary(listings: list[dict], run_stats: dict):
    print("\n" + "=" * 80)
    print("HOUSE FINDER RESULTS")
    print("=" * 80)

    print(f"  Listings found:   {run_stats.get('listings_found', 0)}")
    print(f"  Listings crawled: {run_stats.get('listings_crawled', 0)}")
    print(f"  Listings scored:  {run_stats.get('listings_scored', 0)}")
    print(f"  Listings passed:  {run_stats.get('listings_passed', 0)}")
    print(f"  Crawl failures:   {run_stats.get('crawl_failures', 0)}")
    print("-" * 80)

    if not listings:
        print("  No listings to display.")
        print("=" * 80)
        return

    print(f"  {'Address':<35} {'Score':>6} {'Pass':>5} {'Price':>10}")
    print("-" * 80)
    for listing in listings:
        addr = (listing.get("address") or "Unknown")[:34]
        score = f"{listing.get('avg_score', 0):.1f}" if listing.get("avg_score") else "N/A"
        passed = "YES" if listing.get("listing_pass") else "NO"
        price = f"${listing.get('price', 0):,}" if listing.get("price") else "N/A"
        print(f"  {addr:<35} {score:>6} {passed:>5} {price:>10}")

    print("=" * 80)


async def run_standard(args) -> int:
    criteria = {
        "location": args.location or "",
        "min_beds": args.min_beds,
        "max_beds": args.max_beds,
        "min_baths": args.min_baths,
        "min_price": args.min_price,
        "max_price": args.max_price,
        "start_date": args.start_date,
        "end_date": args.end_date,
    }

    run_id = create_run(json.dumps(criteria))
    run_stats = {
        "listings_found": 0,
        "listings_crawled": 0,
        "listings_scored": 0,
        "listings_passed": 0,
        "crawl_failures": 0,
    }

    try:
        # Step 1: Get listings (from file or search + crawl)
        if args.from_file:
            if not os.path.isfile(args.from_file):
                logger.error("File not found: %s", args.from_file)
                complete_run(run_id, status="failed", error=f"File not found: {args.from_file}")
                return 1
            logger.info("Step 1: Loading listings from %s (no crawl)...", args.from_file)
            listings = load_listings_from_file(args.from_file)
        else:
            logger.info("Step 1: Searching and crawling listings...")
            listings = await run_search(criteria, run_id, max_urls=args.max_listings)

        run_stats["listings_found"] = len(listings)
        run_stats["listings_crawled"] = len(listings)
        update_run(run_id, listings_found=len(listings), listings_crawled=len(listings))

        # Step 1.5: Filter by criteria before scoring and saving
        listings = filter_by_criteria(listings, criteria)

        # Step 1.6: Skip listings that already have feedback
        feedback_ids = get_listing_ids_with_feedback()
        before = len(listings)
        listings = [l for l in listings if l.get("id") not in feedback_ids]
        if len(listings) < before:
            logger.info(
                "Skipped %d listings with existing feedback (%d remaining)",
                before - len(listings), len(listings),
            )

        if not args.from_file:
            if args.save_listings:
                save_listings_to_file(listings, args.save_listings)
            if args.append_listings:
                append_listings_to_file(listings, args.append_listings)

        if args.dry_run:
            logger.info("Dry run: skipping scoring and email.")
            complete_run(run_id, status="completed")
            print_summary(listings, run_stats)
            return 0

        # Step 2: Score with LLM (loop until we have enough passed or exhaust pool)
        target = args.max_score
        passed = []
        offset = 0
        total_scored = 0
        logger.info("Step 2: Scoring listings with Claude (target: %d passed)...", target)

        while len(passed) < target and offset < len(listings):
            needed = target - len(passed)
            batch = listings[offset : offset + needed]
            offset += len(batch)
            if not batch:
                break
            logger.info(
                "Scoring batch of %d listings (%d/%d passed so far, %d remaining in pool)...",
                len(batch), len(passed), target, len(listings) - offset,
            )
            newly_passed = score_listings(batch, force_feedback=args.no_cold_start)
            passed.extend(newly_passed)
            total_scored += len(batch)

        passed = passed[:target]
        run_stats["listings_scored"] = total_scored
        run_stats["listings_passed"] = len(passed)
        update_run(run_id, listings_scored=total_scored, listings_passed=len(passed))

        # Step 3: Review / Notify
        if args.email and not args.no_email:
            logger.info("Step 3: Sending notification email...")
            send_notification(
                to_email=args.email,
                listings=passed,
                summary_stats=run_stats,
                run_id=run_id,
            )
            run_stats["listings_emailed"] = len(passed)
            update_run(run_id, listings_emailed=len(passed))
        elif not args.no_email:
            logger.info("Step 3: Opening local review...")
            from house_finder.reviewer import run_review

            review_result = run_review(passed, run_stats)
            run_stats["listings_reviewed"] = review_result["reviewed"]

        complete_run(run_id, status="completed")
        print_summary(passed, run_stats)
        return 0

    except Exception as e:
        logger.error(f"Run failed: {e}", exc_info=True)
        status = "partial" if run_stats["listings_crawled"] > 0 else "failed"
        complete_run(run_id, status=status, error=str(e))
        print_summary([], run_stats)
        return 1


async def run_single_url(args) -> int:
    run_id = create_run(json.dumps({"url": args.url}))

    try:
        # Step 1: Crawl single URL
        logger.info(f"Crawling single URL: {args.url}")
        listing = await crawl_single_url(args.url, run_id)
        if not listing:
            complete_run(run_id, status="failed", error="Crawl failed")
            print("ERROR: Failed to crawl URL.")
            return 1

        if args.save_listings:
            save_listings_to_file([listing], args.save_listings)
        if args.append_listings:
            append_listings_to_file([listing], args.append_listings)

        # Step 2: Score
        logger.info("Scoring listing with Claude...")
        passed = score_listings([listing], force_feedback=args.no_cold_start)

        # Step 3: Review / Notify
        listings_to_show = passed if passed else [listing]
        single_stats = {
            "listings_found": 1,
            "listings_scored": 1,
            "listings_passed": len(passed),
        }
        if args.email and not args.no_email:
            send_notification(
                to_email=args.email,
                listings=listings_to_show,
                summary_stats=single_stats,
                run_id=run_id,
            )
        elif not args.no_email:
            from house_finder.reviewer import run_review

            run_review(listings_to_show, single_stats)

        complete_run(run_id, status="completed")
        print_summary(
            listings_to_show,
            {
                "listings_found": 1,
                "listings_crawled": 1,
                "listings_scored": 1,
                "listings_passed": len(passed),
                "crawl_failures": 0,
            },
        )
        return 0

    except Exception as e:
        logger.error(f"Single URL run failed: {e}", exc_info=True)
        complete_run(run_id, status="failed", error=str(e))
        return 1


def main():
    args = parse_args()
    init_db()

    if args.url:
        exit_code = asyncio.run(run_single_url(args))
    else:
        exit_code = asyncio.run(run_standard(args))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
