import argparse
import asyncio
import json
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from house_finder.db import complete_run, create_run, init_db, update_run
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
    parser.add_argument("--email", type=str, required=True, help="Recipient email address")
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
        "--dry-run",
        action="store_true",
        help="Crawl only, skip scoring and email",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Score listings but skip sending email; output results to terminal",
    )

    args = parser.parse_args()

    if not args.url and not args.location:
        parser.error("Either --location or --url is required.")

    return args


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
        "location": args.location,
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
        # Step 1: Search and crawl
        logger.info("Step 1: Searching and crawling listings...")
        listings = await run_search(criteria, run_id, max_urls=args.max_listings)
        print('len(listings):', len(listings))
        
        run_stats["listings_found"] = len(listings)
        run_stats["listings_crawled"] = len(listings)
        update_run(run_id, listings_found=len(listings), listings_crawled=len(listings))

        if args.dry_run:
            logger.info("Dry run: skipping scoring and email.")
            complete_run(run_id, status="completed")
            print_summary(listings, run_stats)
            return 0

        # Step 2: Score with LLM
        logger.info("Step 2: Scoring listings with Claude...")
        passed = score_listings(listings)
        print('passed:', passed)
        run_stats["listings_scored"] = len(listings)
        run_stats["listings_passed"] = len(passed)
        update_run(run_id, listings_scored=len(listings), listings_passed=len(passed))

        # Step 3: Send email (unless --no-email)
        if args.no_email:
            logger.info("Skipping email (--no-email); outputting results to terminal.")
        else:
            logger.info("Step 3: Sending notification email...")
            send_notification(
                to_email=args.email,
                listings=passed,
                summary_stats=run_stats,
                run_id=run_id,
            )
            run_stats["listings_emailed"] = len(passed)
            update_run(run_id, listings_emailed=len(passed))

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

        # Step 2: Score
        logger.info("Scoring listing with Claude...")
        passed = score_listings([listing])

        # Step 3: Email (unless --no-email)
        listings_to_show = passed if passed else [listing]
        if not args.no_email:
            send_notification(
                to_email=args.email,
                listings=listings_to_show,
                summary_stats={
                    "listings_found": 1,
                    "listings_scored": 1,
                    "listings_passed": len(passed),
                },
                run_id=run_id,
            )
        else:
            logger.info("Skipping email (--no-email); outputting results to terminal.")

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
