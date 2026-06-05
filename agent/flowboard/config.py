from pathlib import Path
from typing import Optional
import os
import sys

ROOT = Path(__file__).resolve().parent.parent.parent

# When packaged with PyInstaller (`sys.frozen`), `__file__` lives inside the
# one-shot extraction dir, so persistent data must sit next to the .exe instead.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = ROOT

STORAGE_DIR = Path(os.getenv("FLOWBOARD_STORAGE", APP_DIR / "storage"))
DB_PATH = Path(os.getenv("FLOWBOARD_DB", STORAGE_DIR / "flowboard.db"))


def frontend_dist_dir() -> Optional[Path]:
    """Locate the compiled frontend (Vite ``dist/``) to serve same-origin.

    Returns ``None`` when there's no build to serve — the normal dev case,
    where Vite serves the UI on :5173 and the agent is API-only. In a packaged
    build the SPA is bundled (PyInstaller datas → ``frontend_dist``) and served
    from the agent port so the whole app is a single process.
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "frontend_dist")
    candidates.append(ROOT / "frontend" / "dist")
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return None

HTTP_PORT = int(os.getenv("FLOWBOARD_HTTP_PORT", "8101"))
WS_HOST = os.getenv("FLOWBOARD_WS_HOST", "127.0.0.1")
EXTENSION_WS_PORT = int(os.getenv("FLOWBOARD_EXT_WS_PORT", "9223"))

PLANNER_MODEL = os.getenv("FLOWBOARD_PLANNER_MODEL", "claude-sonnet-4-6")
# "cli" → always use claude CLI; "mock" → always mock; "auto" → CLI if available,
# otherwise mock. Default auto.
PLANNER_BACKEND = os.getenv("FLOWBOARD_PLANNER_BACKEND", "auto")

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
