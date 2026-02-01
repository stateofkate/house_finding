#!/usr/bin/env bash
cd "$(dirname "$0")/.."
DB="${DATABASE_PATH:-./house_finder.db}"
sqlite3 -header -column "$DB" "
  SELECT f.id, substr(l.address, 1, 40) as address, f.vote, f.categories,
         substr(f.reason, 1, 50) as reason, f.created_at
  FROM feedback f JOIN listings l ON f.listing_id = l.id
  ORDER BY f.created_at;
"
