# Update task for Claude Code — pull the latest Flow Studio on this Mac Mini

Flow Studio is **already deployed** on this machine (repo cloned, the
`com.otsu.flowstudio` launchd service running, the Cloudflare tunnel up). This
task just pulls the newest code, rebuilds the UI, and restarts the agent. The
public link and tunnel stay the same.

> Safe by design: `.env`, `storage/` (the generated images + DB), `dist/`,
> `.venv/`, and `node_modules/` are **gitignored**, so `git pull` never touches
> secrets or user data.

## What's new in this update
- **Concurrent generations** — no more one-at-a-time; fire several at once. The
  agent runs up to `FLOWBOARD_WORKER_CONCURRENCY` requests in parallel
  (default 3; raise it on this box if the M4 has headroom).
- **Paste an image** (Cmd/Ctrl+V) to upload + attach it as a reference.
- **Atrium-only** engine (the direct-Gemini option was removed); variant counts
  back to **1×–4×**.
- Viewer streams a light **~2048px view image** (fast over the tunnel), upgrades
  to the original on deep zoom; **fixed the black-screen on zoom+pan**.
- Generation no longer **auto-opens** the detail popup (results land in the grid).
- **Reuse prompt** now re-attaches the referenced images; **upload limit 20 MB**;
  **Nano Banana 2** (`gemini-3.1-flash-image`, up to 4K) in the model picker;
  viewer **filmstrip** with ← / → navigation.

> The 20 MB upload, the 2048 view image, and the concurrency change are
> **backend** — they only take effect after the agent restart in step 5.

---

## Steps

```bash
# 1. Point at the existing install (same folder used at setup)
ROOT="$HOME/flow-studio"        # adjust if it was installed elsewhere
cd "$ROOT" && test -f agent/flowboard/main.py && echo "repo OK" || echo "WRONG PATH"

# 2. Pull the latest code on the deploy branch
git fetch origin
git checkout feat/windows-desktop-build
git pull --ff-only origin feat/windows-desktop-build
```

If `git pull` complains about **local changes** (there shouldn't be any on a
deploy box — source files aren't edited here): this box has no intentional edits,
so it's safe to take the remote version exactly —
```bash
git reset --hard origin/feat/windows-desktop-build   # only resets tracked source; .env + storage are gitignored
```

```bash
# 3. (only if the build or agent fails below) refresh dependencies
#    Skip otherwise — pulls rarely change deps.
# agent/.venv/bin/python3 -m pip install -r agent/requirements.txt
# (cd "$ROOT/frontend" && npm install)

# 4. Rebuild the Flow-only UI
cd "$ROOT/frontend"
VITE_FLOW_ONLY=1 npm run build
test -f "$ROOT/frontend/dist/index.html" && echo "build OK"

# 5. Restart the agent (cloudflared keeps running — don't touch it)
launchctl kickstart -k gui/$(id -u)/com.otsu.flowstudio
sleep 6

# 6. Verify
curl -s http://127.0.0.1:8101/api/health        # → {"ok": true, ...}
```

Then open the public link in a browser and confirm the new viewer (filmstrip +
←/→) and that **Nano Banana 2** appears in the model list. Report back to the
user: updated OK + a one-line confirmation.

## Troubleshooting
- Build fails on a missing import → uncomment step 3 (`npm install`) and rebuild.
- Agent won't start → read `"$ROOT/agent/storage/agent.err.log"`; if a Python
  import is missing, uncomment step 3's `pip install` line.
- Port busy after restart → `lsof -ti :8101 | xargs kill`, then redo step 5.
