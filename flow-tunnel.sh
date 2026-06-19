#!/usr/bin/env bash
#
# flow-tunnel.sh — expose the local agent publicly so the Atrium engine can
# fetch reference/source images from /media. Run this in your OWN terminal
# (it needs real network + must stay open to keep the tunnel alive).
#
#   bash flow-tunnel.sh            # tunnels http://127.0.0.1:8101
#   bash flow-tunnel.sh 8101       # explicit port
#
# It writes PUBLIC_MEDIA_BASE_URL into .env automatically; then RESTART the
# agent so it picks up the new URL, and pick Engine = Atrium in Flow Studio.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PORT="${1:-8101}"

command -v cloudflared >/dev/null || { echo "❌ cloudflared not found (brew install cloudflared)"; exit 1; }

echo "▶ Starting cloudflared tunnel → http://127.0.0.1:$PORT (keep this terminal open)…"
LOG="$(mktemp)"
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >"$LOG" 2>&1 &
CF_PID=$!
trap 'kill $CF_PID 2>/dev/null || true' EXIT INT TERM

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  # bail early if cloudflared already died
  kill -0 "$CF_PID" 2>/dev/null || { echo "❌ cloudflared exited early:"; tail -5 "$LOG"; exit 1; }
  sleep 1
done

[ -n "$URL" ] || { echo "❌ Could not get a tunnel URL in time:"; tail -8 "$LOG"; exit 1; }
echo "✅ Tunnel URL: $URL"

if grep -q '^PUBLIC_MEDIA_BASE_URL=' "$ENV_FILE" 2>/dev/null; then
  sed -i '' "s|^PUBLIC_MEDIA_BASE_URL=.*|PUBLIC_MEDIA_BASE_URL=$URL|" "$ENV_FILE"
else
  printf '\nPUBLIC_MEDIA_BASE_URL=%s\n' "$URL" >> "$ENV_FILE"
fi
echo "✅ Wrote PUBLIC_MEDIA_BASE_URL to .env"
echo ""
echo "👉 Next: RESTART the agent (so it reads the new URL), then use Engine = Atrium."
echo "   Quick-tunnel URLs change every run — re-run this script + restart the agent each time,"
echo "   or use a named cloudflare/ngrok tunnel for a stable URL (for deploy)."
echo ""
echo "── tunnel is live; press Ctrl+C to stop ──"
wait "$CF_PID"
