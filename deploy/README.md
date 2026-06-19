# Deploy — Flow Studio (Flow-only) on the Mac Mini M4

A single Python process serves both the API **and** the compiled web UI on
`127.0.0.1:8101`. The public entry point is a Cloudflare **named tunnel** gated
by **Cloudflare Access**, so the 10-person team opens one link, signs in with
their email, and each creates their own Flow project. The Mac Mini's port is
never exposed directly.

```
  teammate browser
        │  https://flow.otsulabs.com   (TLS + Cloudflare Access login)
        ▼
  Cloudflare edge ──► cloudflared (named tunnel, runs on the Mac Mini)
        │  http://127.0.0.1:8101
        ▼
  flowboard agent  ── FastAPI + worker + bundled Flow SPA (VITE_FLOW_ONLY)
        │
        ├─ Gemini API (direct key)            } image engines
        └─ Atrium API + Cloudflare R2 (input hosting, auto-deleted)
```

**Flow-only:** the frontend is built with `VITE_FLOW_ONLY=1`, which makes `/`
serve Flow Studio, hides the "↤ Manga Extract" link, and makes the Manga board
unreachable. The Manga code still ships but no one can navigate to it. (Dev
without the flag keeps both `/` = Manga and `/flow` = Flow.)

---

## One-time setup (on the Mac Mini)

### 1. Secrets — `.env` at the repo root
`/Users/mac/manga_extract/manga_extract/.env` (gitignored) must contain:

```
GEMINI_API_KEY=...
ATRIUM_CLIENT_ID=...
ATRIUM_CLIENT_SECRET=...
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
R2_PUBLIC_BASE_URL=https://<your-bucket>.r2.dev
```
The agent auto-loads this on boot. **R2 stays required** for the Atrium engine
(Atrium's servers fetch input images by public URL; they can't pass Cloudflare
Access, so they can't read `/media` through the tunnel — R2 hosts those inputs
and they're deleted right after each generation).

### 2. Build the Flow-only UI
```bash
bash deploy/build.sh          # = cd frontend && VITE_FLOW_ONLY=1 npm run build
```
Produces `frontend/dist/`, which the agent serves on `:8101`.

### 3. Run the agent as a launchd service (auto-start + crash-restart)
```bash
cp deploy/com.otsu.flowstudio.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.otsu.flowstudio.plist
launchctl enable   gui/$(id -u)/com.otsu.flowstudio
curl -s http://127.0.0.1:8101/api/health    # → {"ok": true, ...}
```
> Keep the Mac Mini awake: System Settings → Energy → "Prevent automatic
> sleeping when the display is off" (or `sudo pmset -c sleep 0`).

### 4. Cloudflare named tunnel
Needs a domain on your Cloudflare account (e.g. `otsulabs.com`).
```bash
cloudflared tunnel login
cloudflared tunnel create flow-studio
cloudflared tunnel route dns flow-studio flow.otsulabs.com
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
#   edit it: paste the tunnel UUID + credentials path printed above
sudo cloudflared service install        # runs at boot, restarts on crash
```

### 5. Cloudflare Access (the login gate)
Cloudflare **Zero Trust** dashboard → Access → Applications → **Add a
self-hosted application**:
- Application domain: `flow.otsulabs.com`
- Policy: **Allow** → Include → *Emails ending in* `@otsulabs.com`
  (or list the 10 specific emails)
- Identity: One-time PIN (email) or Google — whichever the team uses.

Done. Share `https://flow.otsulabs.com`; everyone signs in and lands on Flow.

---

## Updating to a new version
```bash
cd /Users/mac/manga_extract/manga_extract
git pull
bash deploy/build.sh
launchctl kickstart -k gui/$(id -u)/com.otsu.flowstudio   # restart agent
```
(cloudflared keeps running; only the agent needs the restart.)

## Logs / troubleshooting
- Agent: `agent/storage/agent.out.log`, `agent/storage/agent.err.log`
- Tunnel: `cloudflared tunnel info flow-studio`, `sudo launchctl print system/com.cloudflare.cloudflared`
- Health: `curl http://127.0.0.1:8101/api/health`

## Known limit (current)
The worker processes **one request at a time** (each request still runs its
variants in parallel). With many people generating at once, requests queue —
fine for a 10-person team to start. Parallel-across-requests is the next
scaling step if the queue gets long.
