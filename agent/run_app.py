"""Packaged single-process entry point.

Runs the Flowboard agent (FastAPI + extension WS + worker) as one local
process and opens the UI in the default browser. This is the PyInstaller
entry point, and also a handy no-reload launcher in dev:

    python agent/run_app.py

Unlike `uvicorn --reload`, this never restarts on file changes, so the
Chrome extension's WebSocket/token connection stays stable.
"""
from __future__ import annotations

import multiprocessing
import socket
import threading
import time
import webbrowser


def _open_browser(host: str, port: int, url: str) -> None:
    # Wait until the server is actually accepting connections before opening the
    # tab — the full (ML) bundle can take 15-25s to import torch & friends on a
    # cold start, and a fixed delay would pop a "can't connect" page.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    import uvicorn

    from flowboard.config import HTTP_PORT
    from flowboard.main import app

    host = "127.0.0.1"
    url = f"http://{host}:{HTTP_PORT}"
    threading.Thread(target=_open_browser, args=(host, HTTP_PORT, url), daemon=True).start()
    print(f"\n  Flowboard is running → {url}")
    print("  (load the Chrome 'Flowboard Bridge' extension + sign in to Flow")
    print("   to enable generation. Close this window to stop.)\n")
    # Pass the app object (not an import string) so no reloader/spawn is needed
    # under a frozen build.
    uvicorn.run(app, host=host, port=HTTP_PORT, reload=False, log_level="info")


if __name__ == "__main__":
    # PyInstaller: stop bundled child processes from re-executing main().
    multiprocessing.freeze_support()
    main()
