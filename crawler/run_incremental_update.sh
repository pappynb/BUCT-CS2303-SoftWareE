#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

MUSEUMS="${MUSEUMS:-all}"
LIMIT="${CRAWL_LIMIT:-0}"
DELAY="${CRAWL_DELAY:-1.5}"
IMG_DELAY="${CRAWL_IMG_DELAY:-3.5}"
PAGE_SIZE="${CRAWL_PAGE_SIZE:-100}"
AUTO_SYNC_NEO4J="${AUTO_SYNC_NEO4J:-true}"
HAM_SOURCE_INCREMENTAL="${HAM_SOURCE_INCREMENTAL:-true}"
HAM_INCREMENTAL_SINCE="${HAM_INCREMENTAL_SINCE:-}"

args=(
  museum_spider.py
  --museums "$MUSEUMS"
  --limit "$LIMIT"
  --delay "$DELAY"
  --img-delay "$IMG_DELAY"
  --page-size "$PAGE_SIZE"
)

if [[ "$AUTO_SYNC_NEO4J" == "true" || "$AUTO_SYNC_NEO4J" == "1" ]]; then
  args+=(--auto-sync-neo4j)
fi

if [[ "$HAM_SOURCE_INCREMENTAL" == "false" || "$HAM_SOURCE_INCREMENTAL" == "0" ]]; then
  args+=(--no-ham-source-incremental)
fi

if [[ -n "$HAM_INCREMENTAL_SINCE" ]]; then
  args+=(--ham-incremental-since "$HAM_INCREMENTAL_SINCE")
fi

python "${args[@]}"
