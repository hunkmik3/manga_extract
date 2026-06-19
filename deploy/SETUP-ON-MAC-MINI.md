# Setup task for Claude Code — deploy "Flow Studio" on this Mac Mini

You (Claude Code) are running on a **Mac Mini (M4, macOS)** that will host an
internal image-generation web app called **Flow Studio** for a ~10-person team.
Your job: get it running and reachable at a private HTTPS link, then hand the
link back to the user. Work step by step, run the verification after each step,
and **stop to ask the user** wherever this doc says ASK USER.

> This is a **defensive/internal deployment** on hardware the user owns. The
> secrets below are the user's own API keys for their own accounts.

---

## What you're deploying (architecture)

One Python process serves **both** the JSON API and the compiled web UI on
`127.0.0.1:8101`. That port is **never exposed** — the public entry is a
Cloudflare **named tunnel** gated by **Cloudflare Access** (email login).

```
 teammate browser ──https──► Cloudflare edge ──► cloudflared (this Mac)
                                                      │ http://127.0.0.1:8101
                                                      ▼
                                          flowboard agent (FastAPI + worker + SPA)
                                                      ├─ Gemini API (direct key)
                                                      └─ Atrium API + Cloudflare R2
```

Key facts about this app:
- The agent is `flowboard.main:app`; it **must** be launched with the working
  directory = `<repo>/agent` so `flowboard.*` imports resolve.
- Secrets live in a **`.env` at the repo root** and are auto-loaded on boot by
  `agent/flowboard/__init__.py`. No `export` needed.
- The UI is built with **`VITE_FLOW_ONLY=1`** so the app is Flow-only: `/`
  opens Flow Studio, the old "Manga" board is unreachable. The agent serves the
  built `frontend/dist/` automatically.
- **Cloudflare R2 is required** for the Atrium engine (Atrium's servers fetch
  input images by public URL and cannot pass Cloudflare Access, so inputs are
  hosted on R2 and deleted right after each generation). The tunnel is only for
  serving the app to humans.
- Bind the agent to `127.0.0.1` only. There is also an internal WS server on
  `127.0.0.1:9223` (a browser-extension bridge that Flow-only doesn't use); it
  boots harmlessly — leave it.

---

## Step 0 — Collect inputs (ASK USER first)

Ask the user for, and write down:
1. **Repo location** on this machine (full path), or how to obtain it (git
   clone URL / transfer). Everything below uses `$ROOT` = that path.
2. **Public hostname** to use, e.g. `flow.theircompany.com`. They must have
   that **domain on their Cloudflare account** (named tunnels require it).
3. **Who can log in** — an email domain (e.g. `@otsulabs.com`) or an explicit
   list of teammate emails — and the login method (**One-time PIN by email** is
   simplest; or Google).
4. The **`.env` secret values** (see Step 4). Have them paste the values; never
   print them back in full and never commit them.

Then set, in your shell for the rest of the session:
```bash
ROOT="<the path the user gave>"        # e.g. /Users/<user>/flow-studio
cd "$ROOT"
test -f "$ROOT/agent/flowboard/main.py" && echo "repo OK" || echo "WRONG PATH — re-ask user"
```

---

## Step 1 — Prerequisites

```bash
# Homebrew
which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# Tools
brew install python@3.12 node cloudflared
python3 --version    # expect 3.11+
node --version       # expect 18+
cloudflared --version
```

---

## Step 2 — Code + Python deps

```bash
cd "$ROOT"
# (if not cloned yet, clone/transfer first, then cd in)
python3 -m venv agent/.venv
agent/.venv/bin/python3 -m pip install --upgrade pip
agent/.venv/bin/python3 -m pip install -r agent/requirements.txt
# sanity: the web server is present
agent/.venv/bin/python3 -c "import uvicorn, fastapi; print('deps OK', uvicorn.__version__)"
```
> The install may pull large ML libraries and take a few minutes — that's
> expected; let it finish.

---

## Step 3 — Frontend deps

```bash
cd "$ROOT/frontend"
npm install
```

---

## Step 4 — Secrets (`.env` at repo root)

Create `$ROOT/.env` with the values the user pastes (ASK USER). Required:
```
GEMINI_API_KEY=...
ATRIUM_CLIENT_ID=...
ATRIUM_CLIENT_SECRET=...
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
R2_PUBLIC_BASE_URL=https://<bucket>.r2.dev
```
After writing it:
```bash
chmod 600 "$ROOT/.env"
grep -c '=' "$ROOT/.env"     # quick non-secret confirmation it has lines
```
Do **not** echo the file contents back. `.env` is gitignored — never commit it.

---

## Step 5 — Build the Flow-only UI

```bash
cd "$ROOT/frontend"
VITE_FLOW_ONLY=1 npm run build      # → frontend/dist/ (the agent serves this)
test -f "$ROOT/frontend/dist/index.html" && echo "build OK"
```

---

## Step 6 — Run the agent as a launchd service (auto-start + crash-restart)

Generate the plist with **this machine's real paths** (don't hand-type paths):
```bash
PYBIN="$ROOT/agent/.venv/bin/python3"
PLIST="$HOME/Library/LaunchAgents/com.otsu.flowstudio.plist"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.otsu.flowstudio</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYBIN</string>
    <string>-m</string><string>uvicorn</string>
    <string>flowboard.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8101</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT/agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/agent/storage/agent.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/agent/storage/agent.err.log</string>
</dict>
</plist>
PLIST_EOF

mkdir -p "$ROOT/agent/storage"
launchctl bootout  gui/$(id -u)/com.otsu.flowstudio 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl enable    gui/$(id -u)/com.otsu.flowstudio
sleep 8
curl -s http://127.0.0.1:8101/api/health        # → {"ok": true, ...}
curl -s -o /dev/null -w "root=%{http_code}\n" http://127.0.0.1:8101/   # → 200
```
If health fails, read `$ROOT/agent/storage/agent.err.log`.

**Keep the Mac awake** so the service stays up:
```bash
sudo pmset -c sleep 0          # never sleep on AC power
```

---

## Step 7 — Cloudflare named tunnel

The `cloudflared tunnel login` step opens a browser and needs the user to pick
their domain — **ASK USER to complete the browser login**, then continue.
```bash
HOST="<flow.theircompany.com from Step 0>"
cloudflared tunnel login
cloudflared tunnel create flow-studio          # prints a UUID + writes ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns flow-studio "$HOST"

# Find the UUID/credentials file just created:
UUID=$(ls -t ~/.cloudflared/*.json | head -1 | xargs basename | sed 's/.json//')
cat > ~/.cloudflared/config.yml <<CF_EOF
tunnel: $UUID
credentials-file: $HOME/.cloudflared/$UUID.json
ingress:
  - hostname: $HOST
    service: http://127.0.0.1:8101
  - service: http_status:404
CF_EOF

sudo cloudflared service install               # runs at boot, restarts on crash
sleep 5
cloudflared tunnel info flow-studio
```

---

## Step 8 — Cloudflare Access (login gate)

This is a dashboard step — **guide the USER through it** (you can't click it):
1. Go to Cloudflare **Zero Trust** → **Access** → **Applications** → **Add an
   application** → **Self-hosted**.
2. Application domain = the hostname from Step 0.
3. Add a policy: **Action: Allow** → Include → either *Emails ending in*
   `@theircompany.com` or the explicit teammate email list.
4. Identity / login method: **One-time PIN** (email) unless the user chose
   Google.
5. Save.

---

## Step 9 — Final verification

- Local: `curl -s http://127.0.0.1:8101/api/health` → `{"ok": true, ...}`.
- Public: open `https://$HOST` in a browser → you should hit the Cloudflare
  Access login, sign in, then land on **Flow Studio** (no "Manga" anywhere).
- Generate one test image to confirm the keys work end to end.

Report back to the user: the public link, the login method, and confirm a test
generation succeeded.

---

## Updating to a new version (later)

```bash
cd "$ROOT" && git pull
cd "$ROOT/frontend" && VITE_FLOW_ONLY=1 npm run build
launchctl kickstart -k gui/$(id -u)/com.otsu.flowstudio    # restart agent only
```
(cloudflared keeps running.)

## Troubleshooting
- Agent logs: `$ROOT/agent/storage/agent.{out,err}.log`
- Restart agent: `launchctl kickstart -k gui/$(id -u)/com.otsu.flowstudio`
- Tunnel status: `cloudflared tunnel info flow-studio`
- Health: `curl http://127.0.0.1:8101/api/health`
- Port already in use: `lsof -ti :8101 | xargs kill`

## Known limit
The worker processes **one request at a time** (each request still runs its
variants in parallel). With many simultaneous users, requests queue — acceptable
for ~10 people at launch.
