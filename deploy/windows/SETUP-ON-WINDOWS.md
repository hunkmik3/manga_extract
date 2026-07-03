# Deploy GiantFlow on a fresh Windows machine (server + public link)

**Goal:** turn a blank Windows PC into an always-on server that runs the GiantFlow
agent locally and exposes it publicly at **`https://giantflow.realmind.co`** through a
Cloudflare named tunnel — mirroring the Mac Mini setup (which runs the same agent from
source under launchd + cloudflared).

```
  user browser → https://giantflow.realmind.co        (Cloudflare edge, TLS)
        │
        ▼
  cloudflared (named tunnel, Windows service) → http://127.0.0.1:8101
        │
        ▼
  GiantFlow agent (FastAPI + worker + bundled SPA)  ── runs from source, NSSM service
        │
        ├─ Gemini API (direct key)            } image engines
        └─ Atrium API + Cloudflare R2 (input hosting, auto-deleted)
```

> **You are Claude Code running on the Windows machine.** Follow this top to bottom in
> **PowerShell (run as Administrator)**. Stop and **ask the human** at the points marked
> **❓ASK**. Verify each step before moving on. Everything binds to `127.0.0.1` only — the
> public entry is the Cloudflare tunnel, so no Windows firewall / port-forwarding is needed.

---

## 0. Prerequisites to confirm with the human first

**❓ASK the human for:**
1. The **`.env` secret values** (copy them from the Mac Mini's `.env` — Gemini / Atrium / R2 keys). See the template in Step 4. Do **not** invent these.
2. Confirm **`realmind.co` is a zone on their Cloudflare account** (needed so the tunnel can create the `giantflow` DNS record automatically). If it's on a *different* Cloudflare account than the one they'll log into, the DNS route step will fail.
3. Whether this PC has an **NVIDIA GPU** (run `nvidia-smi` — if it prints a table, yes). This decides the PyTorch build in Step 3.

---

## 1. Install prerequisites (winget)

```powershell
winget install --id Python.Python.3.11 -e --source winget
winget install --id OpenJS.NodeJS.LTS -e --source winget
winget install --id Git.Git -e --source winget
winget install --id Cloudflare.cloudflared -e --source winget
winget install --id Gyan.FFmpeg -e --source winget          # optional but recommended
```

Then **close and reopen PowerShell** (so PATH picks up python/node/git/cloudflared).
Verify:

```powershell
python --version      # 3.11.x
node --version; npm --version
git --version
cloudflared --version
```

**NSSM** (service wrapper for auto-start + auto-restart) — download it directly (no package manager needed):

```powershell
$nssmZip = "$env:TEMP\nssm.zip"
Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
Expand-Archive $nssmZip -DestinationPath "C:\nssm" -Force
$nssm = "C:\nssm\nssm-2.24\win64\nssm.exe"
& $nssm version    # should print NSSM 2.24
```

Keep `$nssm` handy — later steps use it.

---

## 2. Clone the repo

```powershell
$ROOT = "C:\giantflow\manga_extract"
git clone https://github.com/hunkmik3/manga_extract.git $ROOT
cd $ROOT
git checkout feat/windows-desktop-build
git pull
```

(All later paths assume `$ROOT = C:\giantflow\manga_extract`. Adjust if you cloned elsewhere.)

---

## 3. Python venv + agent dependencies

```powershell
cd $ROOT\agent
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
```

**PyTorch (needed for speech-bubble / YOLO detection).** Pick ONE based on Step 0.3:

```powershell
# NVIDIA GPU present → CUDA build (much faster detection):
.\.venv\Scripts\python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# OR no NVIDIA GPU → CPU build:
.\.venv\Scripts\python -m pip install torch torchvision
```

Then the app + the rest of the ML stack (torch is already satisfied above):

```powershell
.\.venv\Scripts\python -m pip install -e ".[ml]"
```

> If you want a **lite** install (no bubble detection, panels via heuristic only), skip the
> torch line and run `pip install -e .` instead of `.[ml]`. Not recommended — the Bubble
> Extract branch needs the ML detector.

Sanity check the import:

```powershell
.\.venv\Scripts\python -c "import flowboard.main; print('agent import OK')"
```

---

## 4. Secrets — `.env` at the repo root

Create **`C:\giantflow\manga_extract\.env`** (repo root, NOT the agent folder). It's gitignored.
Fill in the real values from the human (Step 0.1):

```
GEMINI_API_KEY=...
XAI_API_KEY=...
ATRIUM_CLIENT_ID=...
ATRIUM_CLIENT_SECRET=...
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
R2_PUBLIC_URL=https://<bucket>.r2.dev
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
```

**Why R2 is required:** the Atrium engine fetches input images by public URL. The agent
uploads them to R2 and deletes them right after each generation. Without R2 (or a tunnel),
reference/edit generations on Atrium fail. Gemini works without it.

> Write it in UTF-8 without a BOM. Quick check:
> ```powershell
> Get-Content C:\giantflow\manga_extract\.env
> ```

---

## 5. Build the web UI

```powershell
cd $ROOT\frontend
npm ci
npm run build       # → frontend\dist, served by the agent at :8101
```

- This **full build** keeps all surfaces reachable: `/` = Manga Extract, `/flow` = GiantFlow,
  `/bubble` = Bubble Extract. The public link will open `/` by default; share
  **`https://giantflow.realmind.co/flow`** to land straight in GiantFlow.
- **❓ASK** the human only if they want a *Flow-only* deploy (hide Manga/Bubble, make `/`
  open GiantFlow). If yes, build with: `$env:VITE_FLOW_ONLY="1"; npm run build`.

---

## 6. Smoke-test the agent manually (before making it a service)

```powershell
cd $ROOT\agent
.\.venv\Scripts\python -m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101
```

In another PowerShell:

```powershell
curl.exe -s http://127.0.0.1:8101/api/health      # → {"ok": true, ...}
```

Open `http://127.0.0.1:8101/flow` in a browser → GiantFlow should load. Then **Ctrl+C** to
stop the manual run (a service will take over next).

---

## 7. Run the agent as a Windows service (NSSM — auto-start + auto-restart)

```powershell
$nssm = "C:\nssm\nssm-2.24\win64\nssm.exe"
$py   = "C:\giantflow\manga_extract\agent\.venv\Scripts\python.exe"

& $nssm install giantflow-agent $py "-m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101"
& $nssm set giantflow-agent AppDirectory "C:\giantflow\manga_extract\agent"
& $nssm set giantflow-agent AppStdout   "C:\giantflow\manga_extract\agent\storage\agent.out.log"
& $nssm set giantflow-agent AppStderr   "C:\giantflow\manga_extract\agent\storage\agent.err.log"
& $nssm set giantflow-agent Start SERVICE_AUTO_START
& $nssm set giantflow-agent AppExit Default Restart      # restart on crash
& $nssm start giantflow-agent

Start-Sleep 5
curl.exe -s http://127.0.0.1:8101/api/health            # → {"ok": true, ...}
```

(`storage\` is created automatically next to the agent; the SQLite DB + cached images live
there. Make sure the drive isn't full and the folder is writable.)

---

## 8. Cloudflare named tunnel → giantflow.realmind.co

```powershell
cloudflared tunnel login
#   → opens a browser; pick the account/zone that owns realmind.co

cloudflared tunnel create giantflow
#   → prints a TUNNEL UUID and writes  C:\Users\<you>\.cloudflared\<UUID>.json

cloudflared tunnel route dns giantflow giantflow.realmind.co
#   → AUTO-creates a proxied CNAME  giantflow → <UUID>.cfargotunnel.com  in Cloudflare DNS.
#     Do NOT add this record by hand.
```

Create the config file at **`C:\Users\<you>\.cloudflared\config.yml`** (replace `<you>` and the
UUID that `create` printed):

```yaml
tunnel: <UUID>
credentials-file: C:\Users\<you>\.cloudflared\<UUID>.json
protocol: http2

ingress:
  - hostname: giantflow.realmind.co
    service: http://127.0.0.1:8101
  - service: http_status:404
```

Run the tunnel as a service under NSSM too (uniform + auto-restart; avoids the Windows
LocalSystem config-path gotcha by passing `--config` explicitly):

```powershell
$cf  = (Get-Command cloudflared).Source
$cfg = "C:\Users\$env:USERNAME\.cloudflared\config.yml"

& $nssm install giantflow-tunnel $cf "tunnel --config `"$cfg`" run giantflow"
& $nssm set giantflow-tunnel Start SERVICE_AUTO_START
& $nssm set giantflow-tunnel AppExit Default Restart
& $nssm start giantflow-tunnel
```

---

## 9. Verify end-to-end

```powershell
# services up?
Get-Service giantflow-agent, giantflow-tunnel
# local health
curl.exe -s http://127.0.0.1:8101/api/health
# tunnel state
cloudflared tunnel info giantflow
```

Then from **any** browser: open **https://giantflow.realmind.co/flow** → GiantFlow loads and
you can generate. (DNS/TLS can take 1–2 minutes the first time.)

---

## Updating to a new version later

```powershell
cd C:\giantflow\manga_extract
git pull
cd frontend; npm ci; npm run build; cd ..
Restart-Service giantflow-agent        # cloudflared keeps running
```

---

## Troubleshooting

- **`/api/health` fails / service won't start** → read `agent\storage\agent.err.log`.
  Common: missing `.env`, wrong Python path in the NSSM service, or a dependency that
  didn't install (re-run `pip install -e ".[ml]"`).
- **"failed to cache sheet" / upload 500** → the `storage` drive is full or not writable.
  Check free space and folder permissions.
- **Atrium reference/edit fails** → R2 vars in `.env` are wrong/missing. Gemini still works.
- **Tunnel 502 / site down** → the agent service is down (start `giantflow-agent`) or the
  tunnel service isn't running (`Get-Service giantflow-tunnel`; check
  `cloudflared tunnel info giantflow`).
- **DNS route step failed** → the logged-in Cloudflare account doesn't own `realmind.co`, or
  a conflicting `giantflow` record already exists in DNS (delete it, re-run `route dns`).
- **Bubble detection says unavailable (MLUnavailable)** → torch/ultralytics didn't install;
  re-do Step 3. First detection also downloads the YOLO model (needs internet).

## Notes
- **No Chrome extension needed.** GiantFlow's image generation (Flow Studio + Bubble clean)
  uses the Atrium / Gemini / Grok APIs directly. The old "Flowboard Bridge" extension was only
  for the legacy Google-Flow-session engine.
- **Keep the PC awake:** Settings → System → Power → Screen & sleep → set **Sleep = Never**
  (when plugged in) so the server stays reachable.
- The agent runs with `--host 127.0.0.1` (loopback only). The tunnel is the sole public entry;
  the link is open (no login) — share it privately, or add a Cloudflare Access policy later.
