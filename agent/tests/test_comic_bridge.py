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
