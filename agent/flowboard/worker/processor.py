"""In-process worker that drains queued generation requests.

Scope for Run 3 (Phase 2 bridge): a single handler type `"proxy"` that
forwards `params = {url, method?, headers?, body?}` through the extension
via ``flow_client.api_request``. Further types (gen_image, gen_video,
upload_image, etc.) land in later runs once the full Flow protocol + captcha
round-trip is ported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import get_flow_sdk

logger = logging.getLogger(__name__)


# type → coroutine(params) → (result_dict, error_or_None)
Handler = Callable[[dict], Awaitable[tuple[dict, Optional[str]]]]


_ALLOWED_URL_PREFIXES: tuple[str, ...] = (
    "https://aisandbox-pa.googleapis.com/",
)


async def _handle_proxy(params: dict) -> tuple[dict, Optional[str]]:
    url = params.get("url")
    method = params.get("method", "POST")
    if not isinstance(url, str) or not url:
        return {}, "missing_url"
    # Defense-in-depth: refuse to proxy URLs outside the expected allowlist
    # even if the extension's own check was somehow bypassed.
    if not any(url.startswith(p) for p in _ALLOWED_URL_PREFIXES):
        return {}, "url_not_allowed"
    resp = await flow_client.api_request(
        url=url,
        method=method,
        headers=params.get("headers") or {},
        body=params.get("body"),
    )
    if not isinstance(resp, dict):
        return {"value": resp}, None
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    status = resp.get("status")
    if isinstance(status, int) and status >= 400:
        return resp, f"API_{status}"
    return resp, None


async def _handle_create_project(params: dict) -> tuple[dict, Optional[str]]:
    name = params.get("name") or params.get("title") or "Untitled"
    if not isinstance(name, str) or not name.strip():
        return {}, "missing_name"
    tool = params.get("tool", "PINHOLE")
    resp = await get_flow_sdk().create_project(name.strip(), tool)
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    return resp, None


async def _handle_gen_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution: caller-stamped value first (set at dispatch time),
    # then the live value from `flow_client` (resolved authoritatively
    # via /v1/credits on token capture). NO silent default — if both
    # are absent we fail loud with `paygate_tier_unknown`. The old
    # behaviour (default `PAYGATE_TIER_ONE`) silently downgraded Ultra
    # users to Pro and stamped the wrong tier into request.params, which
    # then fed back through `_last_observed_paygate_tier_from_db()` and
    # corrupted /api/auth/me responses for the rest of the session.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    # `ref_media_ids` is the broader name (any upstream image / character /
    # visual_asset feeds in as IMAGE_INPUT_TYPE_REFERENCE). Older callers used
    # `character_media_ids` — accept both.
    raw_ref_ids = params.get("ref_media_ids")
    if not isinstance(raw_ref_ids, list):
        raw_ref_ids = params.get("character_media_ids")
    ref_media_ids: Optional[list[str]] = None
    if isinstance(raw_ref_ids, list):
        cleaned = [m for m in raw_ref_ids if isinstance(m, str) and m]
        ref_media_ids = cleaned or None
    raw_count = params.get("variant_count")
    variant_count = 1
    if isinstance(raw_count, int) and raw_count > 0:
        variant_count = raw_count
    # Per-variant prompts (optional). When provided, each variant gets its
    # own text — used by auto-prompt batch mode so variants don't collapse
    # to the same stance.
    raw_prompts = params.get("prompts")
    per_variant_prompts: Optional[list[str]] = None
    if isinstance(raw_prompts, list):
        cleaned = [p for p in raw_prompts if isinstance(p, str) and p.strip()]
        per_variant_prompts = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None
    resp = await get_flow_sdk().gen_image(
        prompt=prompt.strip(),
        project_id=project_id,
        aspect_ratio=aspect,
        paygate_tier=tier,
        ref_media_ids=ref_media_ids,
        variant_count=variant_count,
        prompts=per_variant_prompts,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    # Flow returns signed fifeUrls directly in the response — persist them
    # immediately so `/media/:id` can serve bytes without any extra round-trip.
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_image response failed")
    return resp, None


# Video polling knobs — overridable in tests. 5-minute hard deadline
# (30 cycles × 10s). When the budget runs out without all ops finishing
# the handler returns the ``timeout_waiting_video`` sentinel and the
# worker stamps the row as ``status='timeout'`` (distinct from
# ``failed``) so the UI can render it as a soft auto-cancel rather than
# a generation error.
VIDEO_POLL_INTERVAL_S = 10.0
VIDEO_POLL_MAX_CYCLES = 30


def _is_request_canceled(rid: Optional[int]) -> bool:
    """Return True iff the cancel endpoint flipped this row to canceled.

    Long-running handlers call this between polls so a user-initiated
    cancel takes effect mid-flight (we can't abort the Flow HTTP calls
    themselves, but we can stop polling and let _process_one keep the
    canceled status intact).
    """
    if not isinstance(rid, int):
        return False
    with get_session() as s:
        req = s.get(Request, rid)
        if req is None:
            return True
        return req.status == "canceled"


async def _handle_gen_video(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    start_media_id = params.get("start_media_id") or params.get("startMediaId")
    raw_starts = params.get("start_media_ids")
    start_media_ids: Optional[list[str]] = None
    if isinstance(raw_starts, list):
        cleaned = [m for m in raw_starts if isinstance(m, str) and m.strip()]
        start_media_ids = [m.strip() for m in cleaned] or None

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    # Either a single start_media_id OR a non-empty start_media_ids list.
    if start_media_ids is None and (
        not isinstance(start_media_id, str) or not start_media_id.strip()
    ):
        return {}, "missing_start_media_id"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see the matching block in _handle_gen_image for
    # the rationale. No silent default; missing tier is a hard error so
    # we never dispatch an Ultra user's video at the Pro checkpoint.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    video_quality = params.get("video_quality")
    if not isinstance(video_quality, str) or not video_quality.strip():
        video_quality = None

    sdk = get_flow_sdk()
    dispatch = await sdk.gen_video(
        prompt=prompt.strip(),
        project_id=project_id,
        start_media_id=start_media_id.strip()
        if isinstance(start_media_id, str) and start_media_id.strip()
        else None,
        start_media_ids=start_media_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        video_quality=video_quality,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"
    # NEW low-priority models return workflows (`{name, primary_media_id}`)
    # instead of operations; the SDK surfaces them on `dispatch["workflows"]`
    # so we can route the poll to /v1/media/<id> instead of batchCheckAsync.
    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    # Per-op resolution: each operation in the batch resolves
    # independently (success, content-filter rejection, or timeout). We
    # used to break the whole loop on the first per-op error, which
    # collapsed a 4-variant gen into a hard failure even when 3/4 clips
    # had already rendered. Now we let every op terminate on its own
    # and aggregate the outcome at the end so partial batches still
    # surface the variants that did succeed.
    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            # User canceled mid-poll. Bail with the special error code
            # so _process_one knows to leave the row's canceled status
            # intact (the cancel endpoint already stamped finished_at +
            # error='canceled'). Any partial state we collected is
            # preserved on `result` for the detail viewer.
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            # Per-op terminal failure (e.g. content filter
            # PUBLIC_ERROR_UNSAFE_GENERATION / PUBLIC_ERROR_AUDIO_FILTERED).
            # Mark this op resolved-with-error and keep polling the rest.
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                # Each op is expected to yield exactly one media entry
                # on success; capture the first valid one.
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    # Slots still unresolved after the max cycles — record as timeout
    # so the partial summary names them alongside any filter failures.
    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    # Build positional outcome aligned to dispatch order. Slot i in
    # `media_ids` corresponds to slot i in the original
    # `start_media_ids` array, so the frontend can keep upstream-image
    # variant ↔ video-variant alignment even when middle slots fail.
    # `slot_errors` mirrors the same indexing — `None` for succeeded
    # slots, error code for blocked ones — so the detail viewer can
    # render the exact filter reason on the blocked tile without
    # having to know the internal Flow op-name keys.
    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    success_count = sum(1 for x in positional_ids if x)
    total = len(op_names)

    if success_count == 0:
        # No op produced a clip — surface the first error verbatim.
        # When all errors are "timeout_waiting_video" this matches the
        # legacy single-op timeout contract; tests rely on it.
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    # ≥1 op succeeded — ingest only the bytes we actually have.
    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_video response failed")
    # Workflow-mode (Low Priority) deliveries arrive inline as base64 MP4
    # bytes on the `/v1/media/<id>` poll — there is no GCS URL to chase.
    # Plant the bytes in the local cache directly so the `/media/<id>` route
    # serves them like any URL-backed asset.
    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64
            media_service.ingest_inline_bytes(
                mid, _b64.b64decode(encoded, validate=False),
                kind="video", mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception("inline ingest from workflow-mode poll failed for %s", mid)

    partial_error: Optional[str] = None
    if op_errors:
        # De-dup distinct error codes for a compact one-line summary
        # (e.g. "1/4 variants blocked: PUBLIC_ERROR_UNSAFE_GENERATION").
        unique_errs = sorted({err for err in op_errors.values()})
        partial_error = (
            f"{len(op_errors)}/{total} variants blocked: {', '.join(unique_errs)}"
        )

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "partial_error": partial_error,
        },
        None,
    )


async def _handle_edit_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    source_media_id = params.get("source_media_id") or params.get("sourceMediaId")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not isinstance(source_media_id, str) or not source_media_id.strip():
        return {}, "missing_source_media_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see _handle_gen_image for rationale. Fail loud,
    # no silent fallback to Pro.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    raw_refs = params.get("ref_media_ids")
    ref_ids: Optional[list[str]] = None
    if isinstance(raw_refs, list):
        cleaned = [m for m in raw_refs if isinstance(m, str) and m]
        ref_ids = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None

    resp = await get_flow_sdk().edit_image(
        prompt=prompt.strip(),
        project_id=project_id,
        source_media_id=source_media_id.strip(),
        ref_media_ids=ref_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from edit_image response failed")
    return resp, None



# ── Omni Flash r2v ────────────────────────────────────────────────────────
# Variable-duration video model with a distinct endpoint + body shape from
# Veo i2v. See agent/flowboard/services/flow_sdk.py::gen_video_omni for the
# request assembly. Single operation per request (no multi-source batching
# like Veo's start_media_ids), so the polling logic collapses to a single
# op + first-error-wins, simpler than _handle_gen_video.

async def _handle_gen_video_omni(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id
    from flowboard.services.media_project_sync import (
        MediaSyncError,
        ensure_media_ids_in_project,
    )

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    raw_refs = params.get("ref_media_ids")
    if not isinstance(raw_refs, list):
        # Also accept the legacy single-source field for symmetry with
        # Veo's start_media_id, so the same upstream-walk on the frontend
        # works without a special-case.
        raw_refs = (
            [params.get("start_media_id")]
            if isinstance(params.get("start_media_id"), str)
            else []
        )
    ref_media_ids = [m for m in raw_refs if isinstance(m, str) and m.strip()]
    duration_s = params.get("duration_s")

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not ref_media_ids:
        return {}, "missing_ref_media_ids"
    if not isinstance(duration_s, int) or duration_s not in (4, 6, 8, 10):
        return {}, "invalid_duration_s"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_PORTRAIT"
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    # ── Cross-project ref sync ────────────────────────────────────────
    # Flow scopes mediaIds to the project they were uploaded in. When
    # the user references media generated under another board's project
    # (the cross-board Reference library case), Flow returns 404 because
    # the asset is unknown in this project. Re-upload bytes from the
    # local cache and substitute the project-local id before dispatch.
    # First sync hits the Flow upload endpoint per ref; subsequent
    # syncs use the MediaProjectMapping cache and are free.
    try:
        synced_refs, sync_failures = await ensure_media_ids_in_project(
            ref_media_ids, project_id
        )
    except MediaSyncError as exc:
        return {}, f"sync_failed: {exc}"[:200]
    if not synced_refs:
        # Every ref failed to sync — surface the first reason.
        first = sync_failures[0][1] if sync_failures else "no_refs_synced"
        return (
            {"sync_failures": sync_failures},
            f"sync_failed: {first}"[:200],
        )
    if sync_failures:
        # Partial sync — log; proceed with the refs that worked.
        logger.warning(
            "gen_video_omni: %d ref(s) failed to sync, proceeding with %d",
            len(sync_failures), len(synced_refs),
        )

    sdk = get_flow_sdk()
    dispatch = await sdk.gen_video_omni(
        prompt=prompt.strip(),
        project_id=project_id,
        ref_media_ids=synced_refs,
        duration_s=duration_s,
        aspect_ratio=aspect,
        paygate_tier=tier,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"
    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    if not any(positional_ids):
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_video_omni response failed")
    # Omni Flash uses workflow-mode polling: Flow delivers the rendered MP4
    # inline as base64 on `/v1/media/<id>` with no signed GCS URL. Plant the
    # bytes in the local cache so `/media/<id>` can serve them.
    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64
            media_service.ingest_inline_bytes(
                mid, _b64.b64decode(encoded, validate=False),
                kind="video", mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception("inline ingest from omni workflow poll failed for %s", mid)

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "duration_s": duration_s,
        },
        None,
    )


# ── Comic pipeline — Node 1: Import & Extract Panels ──────────────────────────
# Pure-local CPU work (OpenCV XY-cut) — does NOT touch the Flow bridge. Each
# detected panel crop is planted in the media cache as a local asset so the
# downstream comic nodes (clean / enhance) can read its bytes and send them
# through the bridge wrapper. The node result is shaped for the frontend to
# patch into Node.data.panels (same contract as gen_image: handler returns,
# frontend persists).

async def _handle_extract_panels(params: dict) -> tuple[dict, Optional[str]]:
    import uuid

    from flowboard.services.comic import panels as panel_svc

    folder = params.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        return {}, "missing_folder"
    # Strip surrounding whitespace + quotes — pasting a path from Finder /
    # a shell often wraps it in single/double quotes, which would otherwise
    # make it "not a directory".
    folder = folder.strip().strip("'\"").strip()
    debug = bool(params.get("debug"))
    detector = params.get("detector") or "heuristic"
    if detector not in ("heuristic", "ml", "webtoon", "auto"):
        return {}, f"invalid_detector:{detector}"

    try:
        # extract_folder is CPU-bound (and ML detection is even heavier); keep
        # the single-consumer worker loop responsive by running it off-thread.
        page_results = await asyncio.to_thread(
            panel_svc.extract_folder, folder, debug=debug, detector=detector
        )
    except (NotADirectoryError, FileNotFoundError) as exc:
        return {}, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        # MLUnavailable (detector="ml" without the optional deps) lands here —
        # surface a clear, actionable message instead of a generic failure.
        from flowboard.services.comic.panel_ml import MLUnavailable
        if isinstance(exc, MLUnavailable):
            return {}, f"ml_unavailable: {str(exc)[:160]}"
        logger.exception("extract_panels failed for %s", folder)
        return {}, f"extract_failed: {str(exc)[:160]}"

    node_id = params.get("__node_id")
    panels_out: list[dict] = []
    pages_out: list[dict] = []
    global_idx = 0
    for pr in page_results:
        debug_media_id: Optional[str] = None
        if pr.overview_png:
            debug_media_id = str(uuid.uuid4())
            media_service.ingest_inline_bytes(
                debug_media_id, pr.overview_png, kind="image", mime="image/png"
            )
        for crop in pr.panels:
            media_id = str(uuid.uuid4())
            ok = media_service.ingest_inline_bytes(
                media_id, crop.png_bytes, kind="image", mime="image/png"
            )
            if not ok:
                logger.warning("extract_panels: failed to cache crop %s/%d", crop.page_name, crop.panel_index)
                continue
            x, y, w, h = crop.box
            panels_out.append({
                "idx": global_idx,
                "pageIndex": crop.page_index,
                "pageName": crop.page_name,
                "panelIndex": crop.panel_index,
                "box": {"x": x, "y": y, "w": w, "h": h},
                "mediaId": media_id,
                "status": "extracted",
            })
            global_idx += 1
        pages_out.append({
            "pageIndex": pr.page_index,
            "pageName": pr.page_name,
            "panelCount": pr.panel_count,
            "debugMediaId": debug_media_id,
            "error": pr.error,
        })

    result = {
        "panels": panels_out,
        "pages": pages_out,
        "page_count": len(page_results),
        "panel_count": len(panels_out),
        "detector": detector,
        "node_id": node_id,
    }
    logger.info(
        "extract_panels: %d page(s) → %d panel(s) from %s",
        result["page_count"], result["panel_count"], folder,
    )
    return result, None


# ── Comic pipeline — 3-node split (Upload → Detect → Panels) ──────────────────
# Node 1 (import_pages): folder → ingest each FULL page as a media asset (no
#   splitting). Node 2 (detect_page_panels): page media → panel boxes per page
#   (auto; the frontend lets the user hand-edit the boxes after). Node 3
#   (crop_panels): page media + (possibly hand-edited) boxes → individual panel
#   crops. All pure-local; no Flow bridge.

_EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".bmp": "image/bmp"}


def _read_page_bgr(media_id: str):
    """Decode a cached page image (ingested by import_pages) to BGR, or None."""
    from flowboard.services.comic import panels as panel_svc
    path = media_service.cached_path(media_id)
    if path is None:
        return None
    try:
        return panel_svc.decode_bgr(path.read_bytes())
    except Exception:  # noqa: BLE001
        return None


async def _handle_import_pages(params: dict) -> tuple[dict, Optional[str]]:
    """Node 1 — ingest every page in a folder as a full-page media asset."""
    import uuid

    from flowboard.services.comic import panels as panel_svc

    folder = params.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        return {}, "missing_folder"
    folder = folder.strip().strip("'\"").strip()

    try:
        paths = await asyncio.to_thread(panel_svc.iter_page_paths, folder)
    except NotADirectoryError as exc:
        return {}, str(exc)[:200]
    if not paths:
        return {}, f"no page images in {folder}"

    pages_out: list[dict] = []
    for i, p in enumerate(paths):
        raw = p.read_bytes()
        mime = _EXT_MIME.get(p.suffix.lower(), "image/png")
        media_id = str(uuid.uuid4())
        if not media_service.ingest_inline_bytes(media_id, raw, kind="image", mime=mime):
            logger.warning("import_pages: failed to cache %s", p.name)
            continue
        h = w = 0
        try:
            img = panel_svc.decode_bgr(raw)
            h, w = int(img.shape[0]), int(img.shape[1])
        except Exception:  # noqa: BLE001
            pass
        pages_out.append({"idx": i, "name": p.name, "mediaId": media_id, "w": w, "h": h})

    result = {"pages": pages_out, "page_count": len(pages_out), "node_id": params.get("__node_id")}
    logger.info("import_pages: %d page(s) from %s", len(pages_out), folder)
    return result, None


async def _handle_detect_page_panels(params: dict) -> tuple[dict, Optional[str]]:
    """Node 2 — auto-detect panel boxes for each upstream page."""
    import uuid

    from flowboard.services.comic import panels as panel_svc

    pages_in = params.get("pages")
    if not isinstance(pages_in, list) or not pages_in:
        return {}, "missing_pages"
    detector = params.get("detector") or "heuristic"
    if detector not in ("heuristic", "ml", "webtoon", "auto"):
        return {}, f"invalid_detector:{detector}"

    def _run():
        out = []
        total = 0
        for pg in pages_in:
            if not isinstance(pg, dict):
                continue
            mid = pg.get("mediaId")
            bgr = _read_page_bgr(mid) if isinstance(mid, str) else None
            boxes = []
            err = None
            if bgr is None:
                err = "page_unreadable"
            else:
                for (x, y, w, h) in panel_svc.detect_boxes(bgr, detector):
                    boxes.append({"id": str(uuid.uuid4()), "x": x, "y": y, "w": w, "h": h})
            total += len(boxes)
            out.append({
                "idx": pg.get("idx"), "name": pg.get("name"), "mediaId": mid,
                "w": pg.get("w") or (bgr.shape[1] if bgr is not None else 0),
                "h": pg.get("h") or (bgr.shape[0] if bgr is not None else 0),
                "boxes": boxes, "error": err,
            })
        return out, total

    try:
        pages_out, total = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        from flowboard.services.comic.panel_ml import MLUnavailable
        if isinstance(exc, MLUnavailable):
            return {}, f"ml_unavailable: {str(exc)[:160]}"
        logger.exception("detect_page_panels failed")
        return {}, f"detect_failed: {str(exc)[:160]}"

    result = {
        "pages": pages_out, "page_count": len(pages_out), "box_count": total,
        "detector": detector, "node_id": params.get("__node_id"),
    }
    logger.info("detect_page_panels: %d page(s) → %d box(es) [%s]", len(pages_out), total, detector)
    return result, None


async def _handle_crop_panels(params: dict) -> tuple[dict, Optional[str]]:
    """Node 3 — crop each (possibly hand-edited) box into a panel image."""
    import uuid

    from flowboard.services.comic import panels as panel_svc

    pages_in = params.get("pages")
    if not isinstance(pages_in, list) or not pages_in:
        return {}, "missing_pages"

    def _run():
        panels_out = []
        gidx = 0
        for pg in pages_in:
            if not isinstance(pg, dict):
                continue
            mid = pg.get("mediaId")
            boxes = pg.get("boxes") or []
            if not isinstance(mid, str) or not boxes:
                continue
            bgr = _read_page_bgr(mid)
            if bgr is None:
                continue
            H, W = bgr.shape[0], bgr.shape[1]
            for j, b in enumerate(boxes):
                x = max(0, int(b.get("x", 0)))
                y = max(0, int(b.get("y", 0)))
                w = int(b.get("w", 0))
                h = int(b.get("h", 0))
                x2 = min(W, x + max(1, w))
                y2 = min(H, y + max(1, h))
                if x2 <= x or y2 <= y:
                    continue
                crop = bgr[y:y2, x:x2]
                cid = str(uuid.uuid4())
                if not media_service.ingest_inline_bytes(
                    cid, panel_svc.encode_png(crop), kind="image", mime="image/png"
                ):
                    continue
                panels_out.append({
                    "idx": gidx, "pageIndex": pg.get("idx"), "pageName": pg.get("name"),
                    "panelIndex": j, "box": {"x": x, "y": y, "w": x2 - x, "h": y2 - y},
                    "mediaId": cid, "status": "extracted",
                })
                gidx += 1
        return panels_out

    try:
        panels_out = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("crop_panels failed")
        return {}, f"crop_failed: {str(exc)[:160]}"

    result = {"panels": panels_out, "panel_count": len(panels_out), "node_id": params.get("__node_id")}
    logger.info("crop_panels: %d panel(s)", len(panels_out))
    return result, None


# ── Comic pipeline — Phase 4/5: Clean (9:16) + Enhance anime via Flow bridge ──
# These call services.comic.bridge.edit_image (upload → Nano Banana Pro edit →
# fetch) directly — NOT through this queue — so running inside a worker handler
# is fine. Source bytes come from either an upstream image media_id, or a
# (page_media_id + box) pair cropped on the fly (comic_panel is reference-only).

def _source_image_bytes(params: dict) -> Optional[bytes]:
    from flowboard.services.comic import panels as panel_svc

    src = params.get("source_media_id")
    if isinstance(src, str) and src:
        p = media_service.cached_path(src)
        if p is not None:
            try:
                return p.read_bytes()
            except OSError:
                return None
    page = params.get("page_media_id")
    box = params.get("box")
    if isinstance(page, str) and isinstance(box, dict):
        pp = media_service.cached_path(page)
        if pp is None:
            return None
        try:
            bgr = panel_svc.decode_bgr(pp.read_bytes())
        except Exception:  # noqa: BLE001
            return None
        H, W = bgr.shape[0], bgr.shape[1]
        x = max(0, int(box.get("x", 0)))
        y = max(0, int(box.get("y", 0)))
        x2 = min(W, x + int(box.get("w", 0)))
        y2 = min(H, y + int(box.get("h", 0)))
        if x2 > x and y2 > y:
            return panel_svc.encode_png(bgr[y:y2, x:x2])
    return None


def _orientation_aspect(data: bytes) -> str:
    from flowboard.services.comic import panels as panel_svc
    try:
        img = panel_svc.decode_bgr(data)
        h, w = img.shape[0], img.shape[1]
        return "IMAGE_ASPECT_RATIO_PORTRAIT" if h >= w else "IMAGE_ASPECT_RATIO_LANDSCAPE"
    except Exception:  # noqa: BLE001
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"


def _page_ref_bytes(params: dict) -> Optional[bytes]:
    """The full source page (the panel was cropped from) — passed as a reference
    image so the model knows the surrounding scene, character, and costume when
    cleaning / extending / enhancing."""
    page = params.get("page_media_id")
    if not isinstance(page, str) or not page:
        return None
    p = media_service.cached_path(page)
    if p is None:
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


async def _bridge_edit(
    raw: bytes, prompt: str, *, project_id: str, aspect: str, image_model: Optional[str],
    references: Optional[list[bytes]], pad_916: bool, node_id, variant_count: int = 1,
) -> tuple[dict, Optional[str]]:
    """Run bridge.edit_image(raw + refs, prompt) → (optional 9:16 letterbox) →
    cache → return media_id + dims. With ``variant_count`` > 1, return every
    candidate in ``mediaIds`` (the "x4" on Flow) — ``mediaId`` is the first."""
    from flowboard.services.comic import bridge, panels as panel_svc
    import uuid

    n = max(1, min(int(variant_count or 1), 4))
    try:
        if n == 1:
            outs = [await bridge.edit_image(
                raw, prompt, reference_images=references,
                project_id=project_id, aspect_ratio=aspect, image_model=image_model,
            )]
        else:
            outs = await bridge.edit_image_variants(
                raw, prompt, reference_images=references,
                project_id=project_id, aspect_ratio=aspect, image_model=image_model,
                variant_count=n,
            )
    except bridge.BridgeEditError as exc:
        return {}, f"bridge_failed: {exc.reason}"[:200]

    media_ids: list[str] = []
    w = h = 0
    for out in outs:
        if pad_916:
            try:
                out = panel_svc.encode_png(panel_svc.pad_to_aspect(panel_svc.decode_bgr(out), 9, 16))
            except Exception:  # noqa: BLE001
                pass
        mid = str(uuid.uuid4())
        media_service.ingest_inline_bytes(mid, out, kind="image", mime="image/png")
        media_ids.append(mid)
        if w == 0:
            try:
                img = panel_svc.decode_bgr(out)
                h, w = int(img.shape[0]), int(img.shape[1])
            except Exception:  # noqa: BLE001
                pass
    return {"mediaId": media_ids[0], "mediaIds": media_ids, "width": w, "height": h, "node_id": node_id}, None


async def _handle_clean_panel(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.comic import panels as panel_svc, prompts

    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    raw = await asyncio.to_thread(_source_image_bytes, params)
    if raw is None:
        return {}, "no_source_image"
    page_ref = await asyncio.to_thread(_page_ref_bytes, params)
    refs = [page_ref] if page_ref else None
    image_model = params.get("image_model")
    image_model = image_model if isinstance(image_model, str) and image_model else None
    extend = bool(params.get("extend_916"))

    prompt = prompts.CLEAN_PROMPT
    if extend:
        prompt += prompts.EXTEND_9_16
    if refs:
        prompt += prompts.REFERENCE_CLAUSE

    if extend:
        # Pre-pad the cleaned panel's canvas to 9:16 (edge-replicated seed) so
        # the model has empty-but-primed regions to OUTPAINT into, instead of
        # returning flat bars. The page reference gives it the scene to extend.
        try:
            raw = panel_svc.encode_png(panel_svc.pad_to_aspect(panel_svc.decode_bgr(raw), 9, 16, mode="replicate"))
        except Exception:  # noqa: BLE001
            pass
        aspect = "IMAGE_ASPECT_RATIO_PORTRAIT"
    else:
        aspect = _orientation_aspect(raw)

    return await _bridge_edit(
        raw, prompt, project_id=project_id.strip(), aspect=aspect, image_model=image_model,
        references=refs, pad_916=extend, node_id=params.get("__node_id"),
        variant_count=params.get("variant_count") or 1,
    )


def _match_and_load_char_refs(panel_bytes: bytes, chars: list) -> list[bytes]:
    """Auto-pick the character in this panel (CCIP match against the character
    DB's samples) and return that character's reference-crop bytes."""
    from flowboard.services.comic import characters as char_svc, panels as panel_svc

    try:
        panel_bgr = panel_svc.decode_bgr(panel_bytes)
    except Exception:  # noqa: BLE001
        return []
    samples: list[tuple[str, object]] = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        sid = c.get("sampleMediaId")
        if not isinstance(sid, str):
            continue
        p = media_service.cached_path(sid)
        if p is None:
            continue
        try:
            samples.append((str(c.get("id")), panel_svc.decode_bgr(p.read_bytes())))
        except Exception:  # noqa: BLE001
            continue
    if not samples:
        return []
    try:
        matched = char_svc.match_character(panel_bgr, samples)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — MLUnavailable / inference errors → skip refs
        return []
    if matched is None:
        return []
    char = next((c for c in chars if isinstance(c, dict) and str(c.get("id")) == matched), None)
    refs: list[bytes] = []
    for mid in (char or {}).get("refMediaIds", [])[:3]:
        if not isinstance(mid, str):
            continue
        p = media_service.cached_path(mid)
        if p is None:
            continue
        try:
            refs.append(p.read_bytes())
        except OSError:
            continue
    return refs


def _panel_reference_bytes(
    panel_bytes: bytes, panel_params: dict, chars: object, *, include_page: bool = True
) -> list[bytes]:
    """References for a comic panel edit: the full source page for environment
    continuity plus matched Character DB crops for identity/costume continuity."""
    refs: list[bytes] = []
    if include_page:
        page_ref = _page_ref_bytes(panel_params)
        if page_ref:
            refs.append(page_ref)
    if isinstance(chars, list) and chars:
        refs.extend(_match_and_load_char_refs(panel_bytes, chars))
    return refs[:5]


async def _handle_enhance_panel(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.comic import prompts

    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    raw = await asyncio.to_thread(_source_image_bytes, params)
    if raw is None:
        return {}, "no_source_image"
    # Auto-match the panel's character → add its reference crops for consistency.
    refs = await asyncio.to_thread(_panel_reference_bytes, raw, params, params.get("characters"))

    image_model = params.get("image_model")
    image_model = image_model if isinstance(image_model, str) and image_model else None
    prompt = prompts.ENHANCE_PROMPT + (prompts.REFERENCE_CLAUSE if refs else "")
    aspect = _orientation_aspect(raw)
    return await _bridge_edit(
        raw, prompt, project_id=project_id.strip(), aspect=aspect, image_model=image_model,
        references=refs or None, pad_916=False, node_id=params.get("__node_id"),
        variant_count=params.get("variant_count") or 1,
    )


# ── Comic pipeline — character tracking (build per-character reference sets) ──
# Detect character bodies across all pages (manga109) + cluster by identity
# (CCIP), then ingest the top crops per character as reference media. Those
# refs get fed to enhance so a character looks consistent across the comic.

async def _handle_build_character_db(params: dict) -> tuple[dict, Optional[str]]:
    import uuid

    from flowboard.services.comic import characters as char_svc
    from flowboard.services.comic.panel_ml import MLUnavailable

    pages_in = params.get("pages")
    if not isinstance(pages_in, list) or not pages_in:
        return {}, "missing_pages"
    conf = params.get("conf")
    conf = float(conf) if isinstance(conf, (int, float)) else 0.30

    page_tuples: list[tuple[str, int, bytes]] = []
    for pg in pages_in:
        if not isinstance(pg, dict):
            continue
        mid = pg.get("mediaId")
        if not isinstance(mid, str):
            continue
        p = media_service.cached_path(mid)
        if p is None:
            continue
        try:
            page_tuples.append((str(pg.get("name") or ""), int(pg.get("idx") or 0), p.read_bytes()))
        except OSError:
            continue
    if not page_tuples:
        return {}, "no_readable_pages"

    try:
        clusters = await asyncio.to_thread(char_svc.build_clusters, page_tuples, conf)
    except MLUnavailable as exc:
        return {}, f"ml_unavailable: {str(exc)[:160]}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("build_character_db failed")
        return {}, f"characters_failed: {str(exc)[:160]}"

    characters: list[dict] = []
    for i, cl in enumerate(clusters):
        ref_ids: list[str] = []
        for png in cl["ref_png"]:
            cid = str(uuid.uuid4())
            if media_service.ingest_inline_bytes(cid, png, kind="image", mime="image/png"):
                ref_ids.append(cid)
        if not ref_ids:
            continue
        characters.append({
            "id": f"char_{i}",
            "name": f"Character {i + 1}",
            "count": cl["count"],
            "refMediaIds": ref_ids,
            "sampleMediaId": ref_ids[0],
            "pages": cl.get("pages", []),
        })

    logger.info("build_character_db: %d page(s) → %d character(s)", len(page_tuples), len(characters))
    return {"characters": characters, "character_count": len(characters), "node_id": params.get("__node_id")}, None


# ── Comic pipeline — combine 4 panels into one 2×2 9:16 storyboard image ──────
# Pre-stitch the 4 panels (reading order) into a rough 2×2 composite, then send
# that single "ground truth" image to the bridge with COMBINE_2X2_PROMPT → the
# model removes text, keeps characters faithful, extends backgrounds to 9:16.

async def _handle_combine_panels(params: dict) -> tuple[dict, Optional[str]]:
    """Clean each of the up-to-4 panels INDIVIDUALLY via the bridge (remove
    text/bubbles + reconstruct art), then code-stitch the cleaned panels into a
    clean 2×2 grid. The model never does the layout — code does — so the grid is
    exact (no re-selecting/rearranging/invented panels)."""
    from flowboard.services.comic import bridge, panels as panel_svc, prompts

    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    specs = params.get("panels")
    if not isinstance(specs, list) or not specs:
        return {}, "missing_panels"
    image_model = params.get("image_model")
    image_model = image_model if isinstance(image_model, str) and image_model else None
    chars = params.get("characters")
    pid = project_id.strip()

    raws = await asyncio.to_thread(
        lambda: [(_source_image_bytes(s) if isinstance(s, dict) else None) for s in specs[:4]]
    )
    if all(r is None for r in raws):
        return {}, "no_source_image"

    # Clean + extend every panel to the same 9:16 portrait so all four cells are
    # filled before code stitches the exact 2×2 layout. Character refs remain
    # opt-in and are used only for identity/costume consistency.
    cleaned: list = []
    for spec, raw in zip(specs[:4], raws):
        if raw is None:
            cleaned.append(None)
            continue
        refs = await asyncio.to_thread(
            lambda: _panel_reference_bytes(
                raw, spec if isinstance(spec, dict) else {}, chars, include_page=False
            )
        )
        prompt = (
            prompts.CLEAN_PROMPT
            + prompts.EXTEND_9_16
            + (prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE if refs else "")
        )
        try:
            out = await bridge.edit_image(
                raw, prompt, reference_images=refs or None, project_id=pid,
                aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT", image_model=image_model,
            )
        except bridge.BridgeEditError as exc:
            return {}, f"bridge_failed: {exc.reason}"[:200]
        try:
            bgr = panel_svc.decode_bgr(out)
            cleaned.append(panel_svc.pad_to_aspect(bgr, 9, 16))
        except Exception:  # noqa: BLE001
            cleaned.append(None)

    if all(c is None for c in cleaned):
        return {}, "all_panels_failed"

    def _enc() -> tuple[list, bytes, int, int]:
        cell_pngs = [None if c is None else panel_svc.encode_png(c) for c in cleaned]
        comp = panel_svc.stitch_2x2(cleaned)
        return cell_pngs, panel_svc.encode_png(comp), int(comp.shape[1]), int(comp.shape[0])

    cell_pngs, composite, w, h = await asyncio.to_thread(_enc)
    cells = _ingest_pngs(cell_pngs)
    media_id = _ingest_png(composite)
    return {
        "mediaId": media_id, "cells": cells, "width": w, "height": h,
        "panels_cleaned": sum(1 for c in cleaned if c is not None),
        "node_id": params.get("__node_id"),
    }, None


def _ingest_png(png: bytes) -> str:
    import uuid
    cid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(cid, png, kind="image", mime="image/png")
    return cid


def _ingest_pngs(pngs: list) -> list:
    return [None if p is None else _ingest_png(p) for p in pngs]


def _stitch_cells(cells: list) -> tuple[bytes, int, int]:
    """Code-stitch the 2×2 composite from up to 4 cell media ids (each already a
    cleaned 9:16 cell). Missing/unreadable cells become blank slots."""
    from flowboard.services.comic import panels as panel_svc
    bgrs = []
    for cid in cells[:4]:
        path = media_service.cached_path(cid) if isinstance(cid, str) else None
        try:
            bgrs.append(panel_svc.decode_bgr(path.read_bytes()) if path else None)
        except Exception:  # noqa: BLE001
            bgrs.append(None)
    comp = panel_svc.stitch_2x2(bgrs)
    return panel_svc.encode_png(comp), int(comp.shape[1]), int(comp.shape[0])


async def _handle_regen_cell(params: dict) -> tuple[dict, Optional[str]]:
    """Re-clean ONE panel of a combine group and re-stitch the 2×2, reusing the
    other (already-cleaned) cells. Lets the user fix the one cell that came out
    wrong without re-running all four. With ``variant_count`` > 1, returns
    ``candidates`` for the cell instead of committing — the user picks one."""
    from flowboard.services.comic import bridge, panels as panel_svc, prompts

    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    panel = params.get("panel")
    cells = params.get("cells")
    index = params.get("index")
    if not isinstance(panel, dict):
        return {}, "missing_panel"
    if not isinstance(cells, list) or not cells:
        return {}, "missing_cells"
    if not isinstance(index, int) or index < 0 or index >= len(cells):
        return {}, "bad_index"
    image_model = params.get("image_model")
    image_model = image_model if isinstance(image_model, str) and image_model else None
    chars = params.get("characters")

    raw = await asyncio.to_thread(_source_image_bytes, panel)
    if raw is None:
        return {}, "no_source_image"
    refs = await asyncio.to_thread(
        lambda: _panel_reference_bytes(raw, panel, chars, include_page=False)
    )
    # Optional custom prompt — lets the user steer a single re-gen (e.g. "make
    # the lighting warmer") instead of the default clean+extend. Blank → default.
    custom = params.get("prompt")
    custom = custom.strip() if isinstance(custom, str) else ""
    base = custom or (prompts.CLEAN_PROMPT + prompts.EXTEND_9_16)
    prompt = base + (prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE if refs else "")
    n = max(1, min(int(params.get("variant_count") or 1), 4))
    try:
        if n == 1:
            outs = [await bridge.edit_image(
                raw, prompt, reference_images=refs or None, project_id=project_id.strip(),
                aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT", image_model=image_model,
            )]
        else:
            outs = await bridge.edit_image_variants(
                raw, prompt, reference_images=refs or None, project_id=project_id.strip(),
                aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT", image_model=image_model,
                variant_count=n,
            )
    except bridge.BridgeEditError as exc:
        return {}, f"bridge_failed: {exc.reason}"[:200]

    def _pad_ingest(b: bytes) -> str:
        return _ingest_png(panel_svc.encode_png(panel_svc.pad_to_aspect(panel_svc.decode_bgr(b), 9, 16)))

    if n > 1:
        # x4: return the candidates for this cell — the user picks one, then the
        # frontend commits it via restitch_cells. Don't change the grid yet.
        candidates = await asyncio.to_thread(lambda: [_pad_ingest(o) for o in outs])
        return {"candidates": candidates, "index": index, "node_id": params.get("__node_id")}, None

    def _rebuild() -> tuple[list, bytes, int, int]:
        new_cells = list(cells)
        new_cells[index] = _pad_ingest(outs[0])
        composite, w, h = _stitch_cells(new_cells)
        return new_cells, composite, w, h

    new_cells, composite, w, h = await asyncio.to_thread(_rebuild)
    return {"mediaId": _ingest_png(composite), "cells": new_cells, "width": w, "height": h, "node_id": params.get("__node_id")}, None


async def _handle_restitch_cells(params: dict) -> tuple[dict, Optional[str]]:
    """Re-stitch the 2×2 from given cell media ids — used after the user picks a
    re-gen candidate for one cell. Pure local (no bridge call)."""
    cells = params.get("cells")
    if not isinstance(cells, list) or not cells:
        return {}, "missing_cells"

    def _do() -> tuple[str, int, int]:
        composite, w, h = _stitch_cells(cells)
        return _ingest_png(composite), w, h

    mid, w, h = await asyncio.to_thread(_do)
    return {"mediaId": mid, "cells": cells[:4], "width": w, "height": h, "node_id": params.get("__node_id")}, None


_DEFAULT_HANDLERS: dict[str, Handler] = {
    "proxy": _handle_proxy,
    "create_project": _handle_create_project,
    "gen_image": _handle_gen_image,
    "gen_video": _handle_gen_video,
    "gen_video_omni": _handle_gen_video_omni,
    "edit_image": _handle_edit_image,
    "extract_panels": _handle_extract_panels,
    "import_pages": _handle_import_pages,
    "detect_page_panels": _handle_detect_page_panels,
    "crop_panels": _handle_crop_panels,
    "clean_panel": _handle_clean_panel,
    "enhance_panel": _handle_enhance_panel,
    "build_character_db": _handle_build_character_db,
    "combine_panels": _handle_combine_panels,
    "regen_cell": _handle_regen_cell,
    "restitch_cells": _handle_restitch_cells,
}


class WorkerController:
    """Single-consumer async queue worker."""

    def __init__(self, handlers: Optional[dict[str, Handler]] = None) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._handlers = dict(handlers or _DEFAULT_HANDLERS)
        self._shutdown = asyncio.Event()
        self._active = 0
        self._started_at: Optional[float] = None

    # ── enqueue ────────────────────────────────────────────────────────────
    def enqueue(self, request_id: int) -> None:
        self._queue.put_nowait(request_id)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._started_at = time.time()
        logger.info("worker started")
        while not self._shutdown.is_set():
            try:
                rid = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self._process_one(rid)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def drain(self) -> None:
        # Wait for any in-flight task to finish.
        while self._active > 0:
            await asyncio.sleep(0.05)

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def uptime_s(self) -> Optional[float]:
        if self._started_at is None:
            return None
        return time.time() - self._started_at

    # ── execution ──────────────────────────────────────────────────────────
    async def _process_one(self, rid: int) -> None:
        self._active += 1
        try:
            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    logger.warning("worker: request %s not found", rid)
                    return
                # Drift guard — the row might have been canceled (or
                # otherwise transitioned out of queued) between enqueue
                # and pop. The cancel endpoint mutates the DB row only;
                # it can't yank the rid back off the in-memory queue, so
                # we re-check here and bail without flipping status.
                if req.status != "queued":
                    logger.info(
                        "worker: skipping rid=%s (status=%s)", rid, req.status
                    )
                    return
                handler = self._handlers.get(req.type)
                if handler is None:
                    req.status = "failed"
                    req.error = f"unknown_request_type:{req.type}"
                    req.finished_at = datetime.now(timezone.utc)
                    s.add(req)
                    s.commit()
                    return

                req.status = "running"
                s.add(req)
                s.commit()
                params = dict(req.params or {})
                # Enrich with the request's node_id so handlers that need
                # to look up Node.data don't depend on the caller copying
                # it into params explicitly. Underscore prefix avoids
                # colliding with handler-defined fields.
                if req.node_id is not None and "__node_id" not in params:
                    params["__node_id"] = req.node_id
                # Long-running handlers re-check this rid between polls
                # to honor user-initiated cancels.
                params["__request_id"] = rid

            # Release the session during the possibly-long RPC.
            result, err = await handler(params)

            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    return
                # Don't overwrite a canceled row with a late-arriving
                # done/failed stamp. The cancel endpoint already set
                # status='canceled' and finished_at; we only persist the
                # partial result for debugging visibility.
                if req.status == "canceled":
                    if isinstance(result, dict):
                        req.result = result
                        s.add(req)
                        s.commit()
                    return
                req.result = result if isinstance(result, dict) else {"value": result}
                req.finished_at = datetime.now(timezone.utc)
                if err:
                    # Video-poll exhaustion gets its own status so the UI
                    # can render "TIMEOUT" instead of a generic failure.
                    req.status = "timeout" if err == "timeout_waiting_video" else "failed"
                    req.error = err
                else:
                    req.status = "done"
                    req.error = None
                s.add(req)
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker exception on rid=%s", rid)
            try:
                with get_session() as s:
                    req = s.get(Request, rid)
                    if req is not None and req.status != "canceled":
                        req.status = "failed"
                        req.error = str(exc)[:500]
                        req.finished_at = datetime.now(timezone.utc)
                        s.add(req)
                        s.commit()
            except Exception:  # noqa: BLE001
                logger.exception("worker: failed to record failure for rid=%s", rid)
        finally:
            self._active -= 1


_worker: Optional[WorkerController] = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker
