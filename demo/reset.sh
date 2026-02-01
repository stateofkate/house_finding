#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
rm -f house_finder.db
cp demo/starting_saved_listings.json saved_listings.json
echo "Reset complete. DB wiped, saved_listings.json restored."
