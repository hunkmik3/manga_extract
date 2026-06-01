#!/usr/bin/env python3
"""Phase-2 acceptance smoke test for the comic image-edit bridge.

Sends a local image to a *running* Flowboard agent's ``/api/comic/edit-image``
endpoint, which routes it through the Flow extension bridge (Nano Banana Pro),
and saves the edited result. This is the literal Phase-2 acceptance:

    edit_image(test_image, "make it grayscale") via bridge -> valid image

Prerequisites (the bridge needs a live Flow session — same as any generation):
  1. `make agent` (or uvicorn) running on :8101
  2. The Chrome extension loaded and a logged-in Google Flow tab open, so the
     extension has captured a Bearer token and a paygate tier.
  3. A board with a bound Flow project. Pass --board-id (the script will call
     POST /api/projects/{id}/project to ensure one) or pass --project-id.

Usage:
    python scripts/comic_edit_smoke.py INPUT.png --board-id 1
    python scripts/comic_edit_smoke.py INPUT.png --project-id <flow_project_id> \
        --prompt "make it grayscale" --out out.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

DEFAULT_AGENT = "http://127.0.0.1:8101"
DEFAULT_PROMPT = "make it grayscale"


def resolve_project_id(agent: str, args) -> str:
    if args.project_id:
        return args.project_id
    if args.board_id is None:
        sys.exit("error: pass --project-id or --board-id")
    r = httpx.post(f"{agent}/api/projects/{args.board_id}/project", timeout=120)
    if r.status_code != 200:
        sys.exit(f"ensure project failed [{r.status_code}]: {r.text}")
    pid = r.json().get("flow_project_id")
    if not pid:
        sys.exit(f"no flow_project_id in response: {r.text}")
    print(f"resolved project_id={pid}")
    return pid


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path, help="path to the source image")
    p.add_argument("--agent", default=DEFAULT_AGENT)
    p.add_argument("--board-id", type=int, default=None)
    p.add_argument("--project-id", default=None)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--aspect-ratio", default="IMAGE_ASPECT_RATIO_LANDSCAPE")
    p.add_argument("--image-model", default=None, help="e.g. NANO_BANANA_PRO")
    p.add_argument("--out", type=Path, default=Path("comic_edit_out.png"))
    args = p.parse_args()

    if not args.input.is_file():
        sys.exit(f"input not found: {args.input}")

    project_id = resolve_project_id(args.agent, args)

    data = {"project_id": project_id, "prompt": args.prompt, "aspect_ratio": args.aspect_ratio}
    if args.image_model:
        data["image_model"] = args.image_model
    files = {"file": (args.input.name, args.input.read_bytes(), "application/octet-stream")}

    print(f"POST {args.agent}/api/comic/edit-image  prompt={args.prompt!r}")
    r = httpx.post(f"{args.agent}/api/comic/edit-image", data=data, files=files, timeout=300)
    if r.status_code != 200:
        sys.exit(f"FAILED [{r.status_code}]: {r.text}")

    out_bytes = r.content
    if len(out_bytes) < 100:
        sys.exit(f"FAILED: suspiciously small result ({len(out_bytes)} bytes)")
    args.out.write_bytes(out_bytes)
    print(f"OK: {len(out_bytes)} bytes  mime={r.headers.get('content-type')}  -> {args.out}")


if __name__ == "__main__":
    main()
