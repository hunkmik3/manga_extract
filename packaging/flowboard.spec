# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Flowboard single-process desktop build.

Builds the FastAPI agent + extension WS + worker + the compiled frontend into
one launchable folder (onedir). Entry point: agent/run_app.py, which starts
uvicorn (no reload) and opens the UI in the default browser.

Prerequisites (run from the repo root):
    cd frontend && npm ci && npm run build && cd ..          # produces frontend/dist
    pip install pyinstaller                                  # + the agent deps

Build:
    pyinstaller packaging/flowboard.spec --noconfirm
    # → dist/flowboard/flowboard(.exe)

Variants (env var, read at build time):
    FLOWBOARD_BUNDLE_ML=1   include the heavy ML stack (torch / ultralytics /
                            onnxruntime / dghs-imgutils / scikit-learn) so the
                            YOLO/Auto panel detector + Character DB work.
    (unset / 0)             lite build — heuristic panel detection only; the
                            ML imports are excluded and degrade gracefully at
                            runtime (MLUnavailable).

The Chrome 'Flowboard Bridge' extension is NOT bundled (it lives in the
browser); generation still requires it loaded + a signed-in Flow session.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # packaging/ → repo root
BUNDLE_ML = os.environ.get("FLOWBOARD_BUNDLE_ML", "0") == "1"

datas = []
binaries = []
hiddenimports = []

# ── compiled frontend (served same-origin by the agent) ────────────────────
dist = ROOT / "frontend" / "dist"
if not (dist / "index.html").is_file():
    raise SystemExit(
        f"frontend build missing at {dist}\n"
        "  run: cd frontend && npm ci && npm run build"
    )
datas += [(str(dist), "frontend_dist")]

# ── app + server packages (dynamic imports PyInstaller can't see statically) ─
hiddenimports += collect_submodules("flowboard")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += ["multipart", "anyio", "sqlalchemy.dialects.sqlite"]

# opencv is always present (pure-OpenCV heuristic panel detector).
for pkg in ("cv2",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ── optional heavy ML stack ────────────────────────────────────────────────
_ML_PKGS = ("torch", "ultralytics", "onnxruntime", "imgutils", "huggingface_hub", "sklearn", "scipy")
if BUNDLE_ML:
    for pkg in _ML_PKGS:
        try:
            d, b, h = collect_all(pkg)
        except Exception:
            # A package may be absent in a partial install — skip rather than fail.
            continue
        datas += d
        binaries += b
        hiddenimports += h
    excludes = []
else:
    # Keep the lite bundle small: don't drag torch & friends in transitively.
    excludes = list(_ML_PKGS)

block_cipher = None

a = Analysis(
    [str(ROOT / "agent" / "run_app.py")],
    pathex=[str(ROOT / "agent")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: binaries live in the COLLECT folder
    name="flowboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep the console: it shows server logs + is the stop button
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="flowboard",
)
