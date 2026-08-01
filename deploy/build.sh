#!/usr/bin/env bash
#
# build.sh — compile the Flow-only frontend the agent will serve on :8101.
#
# VITE_FLOW_ONLY=1 makes the whole app Flow Studio: "/" serves Flow, the Manga
# board is unreachable, and the "↤ Manga Extract" link is hidden. Run this once
# per release (after `git pull`), then restart the agent service.
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT/frontend"
echo "▶ Building Flow-only frontend (VITE_FLOW_ONLY=1)…"
VITE_FLOW_ONLY=1 npm run build

echo "✓ Built → $ROOT/frontend/dist"
echo "  The agent serves this on http://127.0.0.1:8101 — restart it to pick up the new build:"
echo "    launchctl kickstart -k gui/\$(id -u)/com.otsu.flowstudio"
