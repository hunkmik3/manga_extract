"""Unit tests for FlowClient correlation + callback resolution.

We don't exercise a real WebSocket in these tests — we inject a FakeWs that
records outgoing JSON so we can verify the protocol shape, and we resolve
futures via ``flow_client.resolve_callback`` (same code path the HTTP
callback handler uses).
"""
import asyncio
import json

import pytest

from flowboard.services.flow_client import FlowClient


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_api_request_round_trip_via_callback():
    client = FlowClient()
    ws = FakeWs()
    client.set_extension(ws)

    async def later_resolve() -> None:
        # give _send a chance to register the future
        await asyncio.sleep(0)
        assert len(client._pending) == 1
        req_id = next(iter(client._pending))
        client.resolve_callback({"id": req_id, "status": 200, "data": {"ok": True}})

    asyncio.create_task(later_resolve())

    result = await client.api_request(
        url="https://aisandbox-pa.googleapis.com/v1/ping",
        method="GET",
    )

    assert result == {"id": result["id"], "status": 200, "data": {"ok": True}}
    assert ws.sent[0]["method"] == "api_request"
    assert ws.sent[0]["params"]["url"].startswith("https://aisandbox-pa.googleapis.com/")
    assert client._pending == {}
    assert client.ws_stats["request_count"] == 1
    assert client.ws_stats["success_count"] == 1


@pytest.mark.asyncio
async def test_api_request_without_extension_returns_disconnected():
    client = FlowClient()
    result = await client.api_request(url="https://aisandbox-pa.googleapis.com/v1/ping")
    assert result == {"error": "extension_disconnected"}


@pytest.mark.asyncio
async def test_clear_extension_fails_pending_futures():
    client = FlowClient()
    ws = FakeWs()
    client.set_extension(ws)

    async def run_call() -> dict:
        return await client.api_request(
            url="https://aisandbox-pa.googleapis.com/v1/ping"
        )

    task = asyncio.create_task(run_call())
    await asyncio.sleep(0)  # let _send register future
    assert len(client._pending) == 1

    client.clear_extension()
    result = await task
    assert result == {"error": "extension_disconnected"}


@pytest.mark.asyncio
async def test_handle_token_captured_updates_stats():
    client = FlowClient()
    await client.handle_message({"type": "token_captured", "flowKey": "ya29.xxx"})
    stats = client.ws_stats
    assert stats["flow_key_present"] is True
    assert stats["token_age_s"] is not None
    assert stats["token_age_s"] >= 0


def test_callback_secret_is_unique_per_instance():
    a = FlowClient()
    b = FlowClient()
    assert a.callback_secret != b.callback_secret
    assert len(a.callback_secret) >= 32


@pytest.mark.asyncio
async def test_api_request_passes_captcha_action_through():
    client = FlowClient()
    ws = FakeWs()
    client.set_extension(ws)

    async def resolve_soon() -> None:
        await asyncio.sleep(0)
        req_id = next(iter(client._pending))
        client.resolve_callback({"id": req_id, "status": 200, "data": {}})

    asyncio.create_task(resolve_soon())
    await client.api_request(
        url="https://aisandbox-pa.googleapis.com/v1/video:batchGenerateImages",
        captcha_action="IMAGE_GENERATION",
    )
    assert ws.sent[0]["method"] == "api_request"
    assert ws.sent[0]["params"]["captchaAction"] == "IMAGE_GENERATION"


@pytest.mark.asyncio
async def test_trpc_request_uses_method_trpc_and_correlates():
    client = FlowClient()
    ws = FakeWs()
    client.set_extension(ws)

    async def resolve_soon() -> None:
        await asyncio.sleep(0)
        req_id = next(iter(client._pending))
        client.resolve_callback({"id": req_id, "status": 200, "data": {"ok": True}})

    asyncio.create_task(resolve_soon())
    result = await client.trpc_request(
        url="https://labs.google/fx/api/trpc/project.createProject",
        body={"json": {"projectTitle": "t", "toolName": "PINHOLE"}},
    )
    assert ws.sent[0]["method"] == "trpc_request"
    assert ws.sent[0]["params"]["url"].startswith("https://labs.google/")
    assert result["status"] == 200


@pytest.mark.asyncio
async def test_api_request_4xx_counts_as_failed():
    client = FlowClient()
    ws = FakeWs()
    client.set_extension(ws)

    async def resolve_soon() -> None:
        await asyncio.sleep(0)
        req_id = next(iter(client._pending))
        client.resolve_callback({"id": req_id, "status": 403, "data": {"e": "CAPTCHA_FAILED"}})

    asyncio.create_task(resolve_soon())
    await client.api_request(url="https://aisandbox-pa.googleapis.com/v1/ping")
    stats = client.ws_stats
    assert stats["failed_count"] == 1
    assert stats["success_count"] == 0
    assert stats["last_error"] == "API_403"


# ── multi-connection registry (account picker) ──────────────────────────────

@pytest.mark.asyncio
async def test_registry_first_active_then_switch_routes_to_new():
    client = FlowClient()
    a, b = FakeWs(), FakeWs()
    client.add_connection(a)
    cb = client.add_connection(b)
    assert client.connected and client._ws is a          # first connection = active
    assert client.ws_stats["connection_count"] == 2
    assert client.set_active(cb) is True
    assert client._ws is b                               # requests now route to b


@pytest.mark.asyncio
async def test_registry_nonactive_disconnect_keeps_active():
    client = FlowClient()
    a, b = FakeWs(), FakeWs()
    client.add_connection(a)                             # active
    cb = client.add_connection(b)
    client.remove_connection(cb)                         # a stale/other profile drops
    assert client.connected and client._ws is a          # active bridge untouched


@pytest.mark.asyncio
async def test_registry_active_disconnect_promotes_remaining():
    client = FlowClient()
    a, b = FakeWs(), FakeWs()
    ca = client.add_connection(a)                        # active
    cb = client.add_connection(b)
    client.remove_connection(ca)                         # the active one drops
    assert client._active == cb and client._ws is b      # b promoted


@pytest.mark.asyncio
async def test_handle_message_updates_target_connection_only():
    client = FlowClient()
    a, b = FakeWs(), FakeWs()
    ca = client.add_connection(a)                        # active
    cb = client.add_connection(b)
    await client.handle_message(
        {"type": "user_info", "userInfo": {"email": "b@x.com", "name": "B"}}, conn_id=cb
    )
    assert client.user_info is None                      # active (a) flat mirror untouched
    conns = {c["id"]: c for c in client.list_connections()}
    assert conns[cb]["email"] == "b@x.com"
    assert conns[ca]["active"] is True


@pytest.mark.asyncio
async def test_keyless_first_connection_yields_to_keyed_one():
    """The extension opens one WS per browser context; the first is often a
    keyless context (service worker). When a LATER connection announces it has
    the Flow key / captures the token, it must take over the active slot —
    otherwise the bridge stays pinned to the keyless connection and every call
    401s while a perfectly good session sits ignored."""
    client = FlowClient()
    keyless = client.add_connection(FakeWs())   # becomes active (first)
    keyed = client.add_connection(FakeWs())
    assert client._active == keyless

    # keyed context announces it actually holds the Flow key → auto-promote
    await client.handle_message({"type": "extension_ready", "flowKeyPresent": True}, conn_id=keyed)
    assert client._active == keyed
    assert client._flow_key_present is True

    # token then lands on the (now-active) keyed connection and mirrors out
    await client.handle_message({"type": "token_captured", "flowKey": "k" * 50}, conn_id=keyed)
    assert client._flow_key == "k" * 50
    assert client._token_captured_at is not None


@pytest.mark.asyncio
async def test_keyed_active_connection_is_never_hijacked():
    """Multi-account safety: once the active connection holds a key, another
    connection announcing a key must NOT steal the slot mid-use."""
    client = FlowClient()
    first = client.add_connection(FakeWs())
    await client.handle_message({"type": "token_captured", "flowKey": "a" * 50}, conn_id=first)
    second = client.add_connection(FakeWs())
    await client.handle_message({"type": "extension_ready", "flowKeyPresent": True}, conn_id=second)
    assert client._active == first              # untouched
    assert client._flow_key == "a" * 50


@pytest.mark.asyncio
async def test_active_close_promotes_keyed_connection_over_keyless():
    client = FlowClient()
    active = client.add_connection(FakeWs())
    keyless = client.add_connection(FakeWs())
    keyed = client.add_connection(FakeWs())
    await client.handle_message({"type": "token_captured", "flowKey": "z" * 50}, conn_id=active)
    await client.handle_message({"type": "extension_ready", "flowKeyPresent": True}, conn_id=keyed)
    assert client._active == active
    client.remove_connection(active)
    assert client._active == keyed              # keyed wins over older keyless
    assert client._flow_key_present is True
    del keyless
