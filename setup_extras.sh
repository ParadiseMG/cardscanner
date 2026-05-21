#!/usr/bin/env bash
# CardScanner — one-shot installer for the bits not in the base requirements:
#   1. Playwright (Python package)
#   2. Chromium browser binary (~150 MB download)
#
# Run this once after `pip install -r requirements.txt` to enable the headless-
# browser comp scraper. Without it, comp lookup falls back to httpx which eBay
# blocks with HTTP 403.
#
# Usage:
#   bash setup_extras.sh
#
# Idempotent: safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "ERROR: .venv not found. Run 'python3 -m venv .venv' first." >&2
  exit 1
fi

echo "→ Installing Playwright Python package…"
.venv/bin/pip install -q playwright

echo "→ Downloading Chromium browser (~150 MB, one-time)…"
.venv/bin/python3 -m playwright install chromium

echo
echo "✓ Done. Restart uvicorn (Ctrl-C then 'cs' or your saved alias) and the"
echo "  comp scraper will use a real browser to bypass eBay's bot detection."
echo
echo "  Verify with this URL in your dashboard browser:"
echo "    http://127.0.0.1:8765/api/health"
echo "  → look for 'comp_source' showing 'playwright' available."
