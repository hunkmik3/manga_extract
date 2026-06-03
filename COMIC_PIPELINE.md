# Comic Pipeline — manhwa → cleaned panels / 2×2 anime storyboards

Built on top of Flowboard (FastAPI agent + React canvas + a Chrome extension
"Flow bridge"). It turns a folder of comic/manhwa pages into split panels,
removes text, extends to 9:16, combines into 2×2 storyboards, and re-renders to
anime — driving **Google Flow (Nano Banana Pro)** through your own browser
session (uses your Flow subscription, no API billing).

This is a clone-and-run guide. (See `README.md` for the original Flowboard docs.)

---

## Prerequisites
- **Python 3.11+** (3.14 works — torch ships cp314 wheels), **Node 18+**, **Chrome**.
- A Google account with **Google Flow** access (https://labs.google/fx/tools/flow).
- `uv` recommended (the Makefile falls back to stdlib `venv` + `pip`).

## 1. Install deps
```bash
make install            # agent venv (agent/.venv) + frontend npm install
# (or `make install-dev` to also get pytest/ruff for running tests)

# OPTIONAL — ML extras (YOLO panel detector + CCIP character tracking). Heavy (torch ~hundreds of MB):
cd agent && .venv/bin/pip install -e ".[ml]" && cd ..
```
Without `[ml]`, panel detection still works (the pure-OpenCV heuristic); the
"YOLO"/"Auto" detector and the Character DB need `[ml]`.

## 2. Secrets — create `.env`
The Flow public API key is **not** committed. Copy the template and fill it in:
```bash
cp .env.example .env
# edit .env → set FLOWBOARD_FLOW_API_KEY=<your labs.google Flow public key>
```
Reuse the same value you have on your other machine. (It's the public `key=AIza…`
that labs.google Flow puts on its `aisandbox-pa.googleapis.com` requests — visible
in the browser Network tab on the Flow site.) The agent auto-loads `.env`.

## 3. Load the Flow-bridge extension
1. Chrome → `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick the `extension/` folder.
2. Open https://labs.google/fx/tools/flow and **sign in**.
   The extension sniffs your session token and connects to the agent on `ws://127.0.0.1:9223`.

## 4. Run
```bash
make agent       # FastAPI :8101  (+ extension WS :9223)
make frontend    # Vite dev server :5173
```
Open **http://localhost:5173**.

> Tip: `make agent` runs uvicorn with `--reload` (dev). For day-to-day *use*, run
> it **without** reload so the extension/token connection stays stable:
> `cd agent && .venv/bin/uvicorn flowboard.main:app --port 8101`
> After any agent restart, click **Refresh token** in the extension popup.

## 5. The comic workflow (nodes)
From the canvas "Add node" palette:

1. **Comic upload** — *Upload folder* a folder of pages (`.webp/.png/.jpg/.jpeg/.bmp`), or paste a server-side path.
2. **① Create page nodes** — one node per page, with auto-detected panel boxes.
   Detector = *Heuristic* (no deps) / *YOLO* / *Auto*. Edit boxes by hand:
   drag empty area = new box, drag = move, corner = resize, **right-click = Add box**, ✕ = delete.
3. Pick a downstream:
   - **② Create all panel nodes** — one node per panel (live CSS crop; edits to a box update its panel).
   - **③ Combine 2×2 (groups of 4)** — flattens panels in reading order, groups by 4, and each *Combine* node cleans the 4 panels + stitches a **2×2 9:16** image. Use the **↻** on a single cell to re-generate just that one.
   - **Comic clean** (remove text, optional *Extend to 9:16*) → **Comic enhance** (re-render to anime) for per-panel work.
   - **Character DB** — *Build* from the upload to detect + cluster characters; enhance auto-feeds the matched character's reference images for consistency (needs `[ml]`).

Notes:
- Each board gets its own **Flow project**; generated images live there.
- Bridge calls (clean/enhance/combine) require a logged-in Flow tab and spend Flow generations.
- The ML detectors download their model weights from HuggingFace on first use.

## Tests
```bash
cd agent && .venv/bin/python -m pytest
```

## What's NOT in the repo (gitignored)
- `.env` (your key), `agent/.venv`, `frontend/node_modules`, `storage/` (the
  local DB + cached media + uploaded pages). All are recreated on the new
  machine by the steps above; `storage/` is created automatically on first run.
