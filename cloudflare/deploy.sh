#!/usr/bin/env bash
# Build the static page and deploy it, plus the /api/snapshot function, to Cloudflare Pages.
set -euo pipefail
cd "$(dirname "$0")"

python3 build.py
npx wrangler pages deploy
