"""Unit tests for the comic-pipeline image-edit wrapper.

These stub the Flow SDK + media cache so the orchestration (upload → edit →
ingest → fetch), the retry loop, and the failure modes are exercised without a
live extension / Flow session. The end-to-end "through the real bridge" check
is the manual smoke script (scripts/comic_edit_smoke.py).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flowboard.services.comic import bridge
from flowboard.services.comic.bridge import BridgeEditError, edit_image

# project_id must satisfy flow_sdk._PROJECT_ID_RE (^[A-Za-z0-9_-]{1,128}$).
PROJECT_ID = "test-project-0001"


def _media_id() -> str:
    # media_id must satisfy media._MEDIA_ID_RE (^[0-9a-fA-F-]{1,64}$).
    return str(uuid.uuid4())


def _fake_sdk(*, edit_return=None, edit_side_effect=None):
    """A MagicMock SDK whose upload_image always succeeds and whose edit_image
    returns / raises whatever the test wants."""
    sdk = MagicMock()
    sdk.upload_image = AsyncMock(side_effect=lambda **kw: {"media_id": _media_id()})
    if edit_side_effect is not None:
        sdk.edit_image = AsyncMock(side_effect=edit_side_effect)
    else:
        sdk.edit_image = AsyncMock(return_value=edit_return)
    return sdk


def _ok_edit_response():
    rid = _media_id()
    return {"media_ids": [rid], "media_entries": [{"media_id": rid, "url": "https://flow-content.google/x"}]}


@pytest.fixture(autouse=True)
def _no_sleep():
    # Don't actually wait through retry backoff in tests.
    with patch("flowboard.services.comic.bridge.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_happy_path_uploads_source_and_refs_then_returns_bytes():
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock(return_value=1)) as ingest, \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"PNGBYTES", "image/png", "/tmp/x.png"))):
        out = await edit_image(
            b"sourcebytes", "make it grayscale",
            reference_images=[b"ref1", b"ref2"],
            project_id=PROJECT_ID, paygate_tier="PAYGATE_TIER_ONE",
        )

    assert out == b"PNGBYTES"
    # 1 source + 2 refs uploaded.
    assert sdk.upload_image.await_count == 3
    # edit_image was called once with source + 2 ref ids, BASE first is handled
    # inside flow_sdk; here we assert refs were threaded through.
    _, kw = sdk.edit_image.await_args
    assert kw["project_id"] == PROJECT_ID
    assert kw["paygate_tier"] == "PAYGATE_TIER_ONE"
    assert isinstance(kw["source_media_id"], str)
    assert kw["ref_media_ids"] is not None and len(kw["ref_media_ids"]) == 2
    ingest.assert_called_once()


@pytest.mark.asyncio
async def test_no_references_passes_none():
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"OUT", "image/png", "/tmp/x.png"))):
        out = await edit_image(b"src", "p", project_id=PROJECT_ID, paygate_tier="PAYGATE_TIER_ONE")

    assert out == b"OUT"
    assert sdk.upload_image.await_count == 1
    _, kw = sdk.edit_image.await_args
    assert kw["ref_media_ids"] is None


@pytest.mark.asyncio
async def test_retries_generate_then_succeeds_without_reuploading():
    # First edit attempt returns an error, second succeeds.
    responses = [{"error": "transient_flow_error"}, _ok_edit_response()]
    sdk = _fake_sdk(edit_side_effect=responses)
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"OK", "image/png", "/tmp/x.png"))):
        out = await edit_image(b"src", "p", project_id=PROJECT_ID,
                               paygate_tier="PAYGATE_TIER_ONE", max_attempts=3)

    assert out == b"OK"
    assert sdk.edit_image.await_count == 2
    # Upload happened exactly once — not re-done per retry.
    assert sdk.upload_image.await_count == 1


@pytest.mark.asyncio
async def test_exhausts_retries_raises_with_attempt_count():
    sdk = _fake_sdk(edit_return={"error": "still_broken"})
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache", AsyncMock()):
        with pytest.raises(BridgeEditError) as ei:
            await edit_image(b"src", "p", project_id=PROJECT_ID,
                             paygate_tier="PAYGATE_TIER_ONE", max_attempts=2)

    assert ei.value.attempts == 2
    assert "still_broken" in ei.value.reason
    assert sdk.edit_image.await_count == 2


@pytest.mark.asyncio
async def test_fetch_failure_is_retried():
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(side_effect=[None, (b"RECOVERED", "image/png", "/tmp/x.png")])):
        out = await edit_image(b"src", "p", project_id=PROJECT_ID,
                               paygate_tier="PAYGATE_TIER_ONE", max_attempts=3)
    assert out == b"RECOVERED"
    assert sdk.edit_image.await_count == 2


@pytest.mark.asyncio
async def test_missing_tier_fails_loud_before_any_upload():
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.flow_client, "_paygate_tier", None):
        with pytest.raises(BridgeEditError) as ei:
            await edit_image(b"src", "p", project_id=PROJECT_ID)
    assert ei.value.reason == "paygate_tier_unknown"
    assert ei.value.attempts == 0
    sdk.upload_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_live_flow_client_tier():
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.flow_client, "_paygate_tier", "PAYGATE_TIER_TWO"), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"OUT", "image/png", "/tmp/x.png"))):
        await edit_image(b"src", "p", project_id=PROJECT_ID)  # no explicit tier
    _, kw = sdk.edit_image.await_args
    assert kw["paygate_tier"] == "PAYGATE_TIER_TWO"


@pytest.mark.asyncio
async def test_upload_failure_raises_pre_flight():
    sdk = MagicMock()
    sdk.upload_image = AsyncMock(return_value={"error": "flow_rejected_upload"})
    sdk.edit_image = AsyncMock()
    with patch.object(bridge, "get_flow_sdk", return_value=sdk):
        with pytest.raises(BridgeEditError) as ei:
            await edit_image(b"src", "p", project_id=PROJECT_ID, paygate_tier="PAYGATE_TIER_ONE")
    assert ei.value.attempts == 0
    assert "upload_failed" in ei.value.reason
    sdk.edit_image.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(image_bytes=b"", prompt="p", project_id=PROJECT_ID),
        dict(image_bytes=b"x", prompt="   ", project_id=PROJECT_ID),
        dict(image_bytes=b"x", prompt="p", project_id="bad id with spaces"),
    ],
)
@pytest.mark.asyncio
async def test_input_validation(kwargs):
    with pytest.raises(ValueError):
        await edit_image(**kwargs, paygate_tier="PAYGATE_TIER_ONE")


@pytest.mark.asyncio
async def test_upload_cache_dedupes_identical_bytes_across_calls():
    """The same source bytes (same project) upload only once — later edits reuse
    the cached Flow media_id (speeds up re-gens / shared refs)."""
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock(return_value=1)), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"PNG", "image/png", "/tmp/x.png"))):
        for _ in range(3):
            await edit_image(b"same-source", "p", project_id=PROJECT_ID, paygate_tier="PAYGATE_TIER_ONE")
    assert sdk.upload_image.await_count == 1  # uploaded once despite 3 edits
    assert sdk.edit_image.await_count == 3


@pytest.mark.asyncio
async def test_edit_image_variants_returns_all_candidates():
    """variant_count>1 → bridge returns every downloaded candidate's bytes."""
    rids = [_media_id() for _ in range(4)]
    resp = {"media_ids": rids, "media_entries": [{"media_id": r, "url": f"https://flow/{r}"} for r in rids]}
    sdk = _fake_sdk(edit_return=resp)
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock(return_value=4)), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(side_effect=lambda mid: (f"bytes-{mid}".encode(), "image/png", "/tmp/x.png"))):
        outs = await bridge.edit_image_variants(
            b"src", "p", project_id=PROJECT_ID, paygate_tier="PAYGATE_TIER_ONE", variant_count=4,
        )
    assert len(outs) == 4
    assert sdk.edit_image.await_args.kwargs["variant_count"] == 4


@pytest.mark.asyncio
async def test_upsample_image_uploads_then_decodes_4k():
    """Upscale path: upload source → upsampleImage → base64 decode the result."""
    import base64
    sdk = _fake_sdk()  # upload_image returns a fresh media_id
    sdk.upsample_image = AsyncMock(return_value={"encoded_image": base64.b64encode(b"UPSCALED-4K").decode()})
    with patch.object(bridge, "get_flow_sdk", return_value=sdk):
        out = await bridge.upsample_image(
            b"source-bytes", project_id=PROJECT_ID, target="4K", paygate_tier="PAYGATE_TIER_ONE",
        )
    assert out == b"UPSCALED-4K"
    assert sdk.upload_image.await_count == 1  # uploaded once
    assert sdk.upsample_image.await_args.kwargs["target_resolution"] == "UPSAMPLE_IMAGE_RESOLUTION_4K"


@pytest.mark.asyncio
async def test_upsample_image_raises_on_flow_error():
    sdk = _fake_sdk()
    sdk.upsample_image = AsyncMock(return_value={"error": "PUBLIC_ERROR_UNUSUAL_ACTIVITY"})
    with patch.object(bridge, "get_flow_sdk", return_value=sdk):
        with pytest.raises(BridgeEditError):
            await bridge.upsample_image(b"x", project_id=PROJECT_ID, target="2K", paygate_tier="PAYGATE_TIER_ONE")


@pytest.mark.asyncio
async def test_quota_error_fails_fast_without_retries():
    """Daily-quota / anti-abuse / content-filter errors can never succeed on
    retry — the bridge must surface them after ONE attempt instead of burning
    the remaining attempts (extra doomed calls make the account look MORE
    bot-like)."""
    for err in (
        "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED",
        "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
    ):
        sdk = _fake_sdk(edit_return={"error": err})
        with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
             patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
             patch.object(bridge.media_service, "fetch_and_cache", AsyncMock()):
            with pytest.raises(BridgeEditError) as ei:
                await edit_image(b"src" + err.encode(), "p", project_id=PROJECT_ID,
                                 paygate_tier="PAYGATE_TIER_ONE", max_attempts=3)
        assert err in ei.value.reason
        assert ei.value.attempts == 1          # failed fast
        assert sdk.edit_image.await_count == 1  # no doomed retries


@pytest.mark.asyncio
async def test_transient_error_still_retries():
    """Sanity: the fail-fast classifier must NOT catch ordinary transient
    errors — those keep the existing retry behaviour."""
    responses = [{"error": "extension_disconnected"}, _ok_edit_response()]
    sdk = _fake_sdk(edit_side_effect=responses)
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch.object(bridge.media_service, "ingest_urls", MagicMock()), \
         patch.object(bridge.media_service, "fetch_and_cache",
                      AsyncMock(return_value=(b"OK", "image/png", "/tmp/x.png"))):
        out = await edit_image(b"src-transient", "p", project_id=PROJECT_ID,
                               paygate_tier="PAYGATE_TIER_ONE", max_attempts=3)
    assert out == b"OK"
    assert sdk.edit_image.await_count == 2


def _gemini_ok_response(png=b"\x89PNG-fake"):
    import base64
    return {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}
    ]}}]}


def _fake_httpx_client(post_side_effects):
    """An AsyncClient stand-in whose .post returns the queued responses."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=post_side_effects)
    return client


def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    return r


@pytest.mark.asyncio
async def test_gemini_model_routes_to_api_engine(monkeypatch):
    """image_model='gemini-…' must bypass the Flow SDK entirely (no extension,
    no tier check) and return the API result."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")
    client = _fake_httpx_client([_resp(200, _gemini_ok_response(b"APIIMG"))])
    sdk = _fake_sdk(edit_return=_ok_edit_response())
    with patch.object(bridge, "get_flow_sdk", return_value=sdk), \
         patch("flowboard.services.comic.gemini_api.httpx.AsyncClient", return_value=client):
        out = await edit_image(b"src-api", "make it anime", project_id=PROJECT_ID,
                               image_model="gemini-3-pro-image")  # NOTE: no paygate_tier
    assert out == b"APIIMG"
    sdk.edit_image.assert_not_awaited()      # Flow never touched
    sdk.upload_image.assert_not_awaited()
    # request body carried source + prompt and the model in the URL
    args, kwargs = client.post.await_args
    assert "gemini-3-pro-image:generateContent" in args[0]
    assert kwargs["json"]["contents"][0]["parts"][-1]["text"] == "make it anime"


@pytest.mark.asyncio
async def test_gemini_api_429_retries_and_503_retries(monkeypatch):
    from flowboard.services.comic import gemini_api
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")
    # 429 (intermittent Nano Banana error, per Atrium) → retried, then succeeds.
    client = _fake_httpx_client([
        _resp(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}),
        _resp(200, _gemini_ok_response(b"OK1")),
    ])
    with patch("flowboard.services.comic.gemini_api.httpx.AsyncClient", return_value=client), \
         patch("flowboard.services.comic.gemini_api.asyncio.sleep", new=AsyncMock()):
        out = await gemini_api.edit_image_variants(b"s", "p", image_model="gemini-3-pro-image")
    assert out == [b"OK1"]
    assert client.post.await_count == 2
    # 503 → retried, then succeeds
    client2 = _fake_httpx_client([
        _resp(503, {"error": {"status": "UNAVAILABLE", "message": "high demand"}}),
        _resp(200, _gemini_ok_response(b"OK2")),
    ])
    with patch("flowboard.services.comic.gemini_api.httpx.AsyncClient", return_value=client2), \
         patch("flowboard.services.comic.gemini_api.asyncio.sleep", new=AsyncMock()):
        outs = await gemini_api.edit_image_variants(b"s", "p", image_model="gemini-3-pro-image")
    assert outs == [b"OK2"]
    assert client2.post.await_count == 2


@pytest.mark.asyncio
async def test_gemini_api_missing_key_errors(monkeypatch):
    from flowboard.services.comic import gemini_api
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(BridgeEditError) as ei:
        await gemini_api.edit_image_variants(b"s", "p", image_model="gemini-3-pro-image")
    assert "GEMINI_API_KEY" in ei.value.reason
