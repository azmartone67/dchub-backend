#!/bin/bash
# publish_partners_post.sh — Phase r54 (2026-05-25).
#
# One-shot helper to publish the LinkedIn partners post.
# Reads the draft from PATCHES/LINKEDIN_PARTNERS_POST.md,
# extracts the "Recommended hook + body" section,
# fires it via /api/v1/linkedin/post.
#
# Usage:
#   export DCHUB_ADMIN_KEY=<your-key>
#   bash scripts/publish_partners_post.sh           # uses long variant
#   bash scripts/publish_partners_post.sh --short   # uses short variant
#   bash scripts/publish_partners_post.sh --dry-run # print, don't post
#
# After firing, verify on https://www.linkedin.com/company/dchub
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRAFT_FILE="${REPO_ROOT}/PATCHES/LINKEDIN_PARTNERS_POST.md"
ENDPOINT="${ENDPOINT:-https://dchub.cloud/api/linkedin/post}"
VARIANT="long"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --short)   VARIANT="short" ;;
    --dry-run) DRY_RUN=1 ;;
    *)         echo "unknown arg: $arg"; exit 1 ;;
  esac
done

if [ ! -f "$DRAFT_FILE" ]; then
  echo "ERROR: draft file not found at $DRAFT_FILE"
  exit 1
fi

if [ -z "${DCHUB_ADMIN_KEY:-}" ] && [ "$DRY_RUN" = "0" ]; then
  echo "ERROR: DCHUB_ADMIN_KEY not set. Either export it or use --dry-run."
  exit 1
fi

# Extract the text. The draft has two variants demarcated by '---'.
# Long variant: first block after "## Recommended hook + body"
# Short variant: first block after "## Variant — shorter punchier"
if [ "$VARIANT" = "long" ]; then
  TEXT=$(awk '/^## Recommended hook \+ body/,/^---/{print}' "$DRAFT_FILE" \
          | sed -n '/^> /p' | sed 's/^> //')
else
  TEXT=$(awk '/^## Variant — shorter punchier hook/,/^---/{print}' "$DRAFT_FILE" \
          | sed -n '/^> /p' | sed 's/^> //')
fi

if [ -z "$TEXT" ]; then
  echo "ERROR: could not extract '$VARIANT' variant from draft."
  exit 1
fi

CHAR_COUNT=$(echo -n "$TEXT" | wc -c)
echo "── Extracted text (${CHAR_COUNT} chars) ────────────────────────"
echo "$TEXT"
echo "────────────────────────────────────────────────────────────────"

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY RUN — would POST to $ENDPOINT"
  exit 0
fi

# Fire
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'text': sys.argv[1],
    'link_url': 'https://dchub.cloud/partners',
    'link_title': 'DC Hub Partners',
    'link_desc': 'Operators, investors, brokers, infrastructure firms powering DC Hub intelligence',
}))" "$TEXT")

echo "Posting to LinkedIn..."
RESP=$(curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  --data "$PAYLOAD" \
  "$ENDPOINT")

echo "$RESP" | python3 -m json.tool || echo "$RESP"
