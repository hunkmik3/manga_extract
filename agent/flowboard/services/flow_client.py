"""Bridge to the Chrome MV3 extension over WebSocket.

Ported + trimmed from flowkit (https://github.com/crisng95/flowkit).

Control flow:
1. Extension opens WS to :9223.
2. Agent sends ``{type:"callback_secret", secret}`` immediately.
3. When the agent wants to make an authenticated call against Google Flow /
   aisandbox-pa, it calls ``flow_client.api_request(url, method, headers, body)``
   which sends ``{id, method:"api_request", params}`` over WS and awaits a future.
4. The extension performs ``fetch(url, Authorization: Bearer <token>)`` inside
   the user's browser session and POSTs the response to
   ``/api/ext/callback`` with ``X-Callback-Secret``.
5. That HTTP handler resolves the pending future by id.
6. WS-side inbound messages from the extension (``token_captured``,
   ``extension_ready``, ``pong``, ``status``) update our stats.

Multiple extensions (one per Chrome profile / Google account) can be connected
at once — each is a separate ``_Conn`` in the registry. Exactly one is the
*active* connection; all proxied requests + the public properties reflect it.
The active connection's state is mirrored onto the flat ``self._…`` fields so
the rest of the agent (bridge, routes, tests) can keep reading them unchanged.
The user picks which account is active via ``set_active`` (see the account
picker UI) — handy for switching when one account hits its daily quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# Google Flow's public API key — appears verbatim in every aisandbox-pa
# request URL Flow web emits. It is a public client key (not a private
# secret), but it's loaded from the FLOWBOARD_FLOW_API_KEY env var (see
# .env / .env.example) so it isn't committed to the repo.
_FLOW_API_KEY = os.getenv("FLOWBOARD_FLOW_API_KEY", "")
if not _FLOW_API_KEY:
    logger.warning(
        "FLOWBOARD_FLOW_API_KEY is not set — Flow credits/paygate calls will fail. "
        "Copy .env.example to .env and set it (see README)."
    )
_FLOW_CREDITS_URL = "https://aisandbox-pa.googleapis.com/v1/credits"
# Minimum gap between paygate-tier refreshes when the same Bearer token
# is re-delivered. Tier rarely changes; 60 s is fine for AccountPanel
# freshness and tames the credits-fetch storm an old extension can
# induce by re-emitting `token_captured` on every outbound request.
_TIER_REFRESH_MIN_INTERVAL_S = 60.0


@dataclass
class _Conn:
    """One connected extension (one Chrome profile / Google account)."""

    id: str
    ws: Any
    flow_key: Optional[str] = None
    flow_key_present: bool = False
    user_info: Optional[dict] = None
    paygate_tier: Optional[str] = None
    sku: Optional[str] = None
    credits: Optional[int] = None
    token_captured_at: Optional[float] = None
    last_tier_fetch_at: Optional[float] = None


class FlowClient:
    """Singleton bridge client (multi-connection, single active)."""

    DEFAULT_TIMEOUT = 180.0  # seconds

    def __init__(self) -> None:
        # Registry of all connected extensions + which one is active.
        self._conns: dict[str, _Conn] = {}
        self._active: Optional[str] = None
        self._conn_seq = 0

        self._pending: dict[str, asyncio.Future] = {}
        self._callback_secret: str = secrets.token_urlsafe(32)

        # ── flat "mirror" of the ACTIVE connection ──────────────────────────
        # Kept so the bridge / routes / tests can read a single connection's
        # state without knowing about the registry. _activate() syncs these.
        self._ws: Optional[Any] = None
        self._token_captured_at: Optional[float] = None
        self._flow_key_present: bool = False
        self._flow_key: Optional[str] = None  # Bearer for server-side /v1/credits; never logged
        self._last_tier_fetch_at: Optional[float] = None
        self._last_logged_key: Optional[str] = None
        self._user_info: Optional[dict] = None
        self._paygate_tier: Optional[str] = None
        self._sku: Optional[str] = None  # e.g. "WS_ULTRA" / "WS_PRO"
        self._credits: Optional[int] = None

        self._request_count = 0
        self._success_count = 0
        self._failed_count = 0
        self._last_error: Optional[str] = None

    # ── connection registry ─────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def callback_secret(self) -> str:
        return self._callback_secret

    def add_connection(self, ws: Any) -> str:
        """Register a newly-connected extension. The FIRST connection becomes
        active; later ones are tracked but DON'T steal the active slot (so a
        second profile reconnecting can't hijack the bridge mid-use)."""
        self._conn_seq += 1
        cid = f"conn-{self._conn_seq}"
        self._conns[cid] = _Conn(id=cid, ws=ws)
        if self._active is None:
            self._activate(cid)
        return cid

    def remove_connection(self, cid: str) -> None:
        """Drop a connection. If it was the active one, fail its in-flight
        requests and promote any remaining connection (else go disconnected).
        A non-active connection closing leaves the active bridge untouched."""
        if self._conns.pop(cid, None) is None:
            return
        if cid == self._active:
            self._fail_pending()
            self._activate(next(iter(self._conns), None))

    def set_active(self, cid: str) -> bool:
        """Make ``cid`` the connection all requests route through."""
        if cid not in self._conns:
            return False
        self._activate(cid)
        return True

    def list_connections(self) -> list[dict]:
        """Snapshot of every connected account for the picker UI."""
        now = time.time()
        out = []
        for cid, c in self._conns.items():
            info = c.user_info or {}
            out.append({
                "id": cid,
                "email": info.get("email"),
                "name": info.get("name"),
                "picture": info.get("picture"),
                "tier": c.paygate_tier,
                "sku": c.sku,
                "credits": c.credits,
                "active": cid == self._active,
                "token_age_s": int(now - c.token_captured_at) if c.token_captured_at else None,
            })
        return out

    def _activate(self, cid: Optional[str]) -> None:
        """Point the active slot at ``cid`` (or None) and mirror its state onto
        the flat fields the rest of the agent reads."""
        self._active = cid
        conn = self._conns.get(cid) if cid else None
        if conn is None:
            self._ws = None
            self._flow_key = None
            self._flow_key_present = False
            self._user_info = None
            self._paygate_tier = None
            self._sku = None
            self._credits = None
            self._token_captured_at = None
            return
        self._ws = conn.ws
        self._flow_key = conn.flow_key
        self._flow_key_present = conn.flow_key_present
        self._user_info = conn.user_info
        self._paygate_tier = conn.paygate_tier
        self._sku = conn.sku
        self._credits = conn.credits
        self._token_captured_at = conn.token_captured_at

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("extension_disconnected"))
        self._pending.clear()

    # ── legacy single-connection API (used by tests + as simple shims) ───────
    def set_extension(self, ws: Any) -> None:
        """Register ``ws`` and make it the active connection."""
        cid = self.add_connection(ws)
        self._activate(cid)

    def clear_extension(self) -> None:
        """Drop the active connection (+ fail its pending requests) and promote
        any remaining one. Also resets the flat mirror when nothing is left."""
        cid = self._active
        if cid is not None:
            self._conns.pop(cid, None)
        self._fail_pending()
        self._activate(next(iter(self._conns), None))

    @property
    def user_info(self) -> Optional[dict]:
        return self._user_info

    @property
    def paygate_tier(self) -> Optional[str]:
        return self._paygate_tier

    @property
    def sku(self) -> Optional[str]:
        return self._sku

    @property
    def credits(self) -> Optional[int]:
        return self._credits

    async def fetch_paygate_tier(self) -> bool:
        """Resolve the ACTIVE connection's paygate tier via /v1/credits.

        Triggered on demand via /api/auth/scan when the active cache is cold but
        the WS is open. Per-connection resolution on ``token_captured`` goes
        through ``_fetch_tier_for``. Returns True on success (tier cached).
        """
        conn = self._conns.get(self._active) if self._active else None
        return await self._fetch_tier_for(conn)

    async def _fetch_tier_for(self, conn: Optional[_Conn]) -> bool:
        """Authoritative paygate-tier resolution via Flow's /v1/credits.

        ``conn`` None → operate on the flat mirror (legacy / no-registry path,
        e.g. unit tests that set ``_flow_key`` directly). Otherwise resolve for
        that specific connection (so each account in the picker shows its own
        tier/credits) and mirror onto the flat fields when it's the active one.

        IMPORTANT: never log the Bearer token.
        """
        key = conn.flow_key if conn is not None else self._flow_key
        if not key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _FLOW_CREDITS_URL,
                    params={"key": _FLOW_API_KEY},
                    headers={
                        "authorization": f"Bearer {key}",
                        "origin": "https://labs.google",
                        "referer": "https://labs.google/",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("fetch_paygate_tier transport error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning(
                "fetch_paygate_tier returned HTTP %s (token may be expired)",
                resp.status_code,
            )
            return False
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("fetch_paygate_tier: response was not JSON")
            return False
        tier = data.get("userPaygateTier")
        # Accept ANY PAYGATE_TIER_* — Google adds new tiers (PAYGATE_TIER_TIER1P5
        # etc.); this is the account's authoritative tier, passed back to Flow.
        if not (isinstance(tier, str) and tier.startswith("PAYGATE_TIER_")):
            logger.warning(
                "fetch_paygate_tier: response missing userPaygateTier (got %r)",
                tier,
            )
            return False
        sku = data.get("sku") if isinstance(data.get("sku"), str) else None
        credits_val = data.get("credits") if isinstance(data.get("credits"), int) else None

        if conn is not None:
            conn.paygate_tier = tier
            if sku is not None:
                conn.sku = sku
            if credits_val is not None:
                conn.credits = credits_val
            if conn.id == self._active:
                self._paygate_tier = conn.paygate_tier
                self._sku = conn.sku
                self._credits = conn.credits
        else:
            self._paygate_tier = tier
            if sku is not None:
                self._sku = sku
            if credits_val is not None:
                self._credits = credits_val
        logger.info(
            "fetch_paygate_tier resolved tier=%s sku=%s credits=%s",
            tier, sku, credits_val,
        )
        return True

    # ── inbound handling ───────────────────────────────────────────────────
    async def handle_message(self, data: dict, conn_id: Optional[str] = None) -> None:
        """Process a frame from an extension. ``conn_id`` (from ws_server) names
        the sending connection; None is the legacy/test path that updates the
        flat mirror directly. State is written to the connection's record and
        mirrored onto the flat fields when it's the active connection."""
        t = data.get("type")
        conn = self._conns.get(conn_id) if conn_id is not None else None
        # Mirror to the flat fields when this is the active connection, or when
        # there's no specific connection (legacy/test path).
        mirror = conn is None or conn_id == self._active

        if t == "extension_ready":
            present = bool(data.get("flowKeyPresent"))
            if conn is not None:
                conn.flow_key_present = present
            if mirror:
                self._flow_key_present = present
            logger.info("extension_ready flowKeyPresent=%s (%s)", present, conn_id or "active")
            return

        if t == "token_captured":
            now = time.time()
            if conn is not None:
                conn.flow_key_present = True
                conn.token_captured_at = now
            if mirror:
                self._flow_key_present = True
                self._token_captured_at = now
            flow_key = data.get("flowKey")
            if isinstance(flow_key, str) and flow_key:
                prev_key = conn.flow_key if conn is not None else self._flow_key
                key_changed = flow_key != prev_key
                if conn is not None:
                    conn.flow_key = flow_key
                if mirror:
                    self._flow_key = flow_key
                last = (conn.last_tier_fetch_at if conn is not None else self._last_tier_fetch_at) or 0.0
                if key_changed or (now - last) > _TIER_REFRESH_MIN_INTERVAL_S:
                    if flow_key != self._last_logged_key:
                        logger.info("token_captured (len=%d)", len(flow_key))
                        self._last_logged_key = flow_key
                    if conn is not None:
                        conn.last_tier_fetch_at = now
                    if mirror:
                        self._last_tier_fetch_at = now
                    # Resolve this connection's tier in the background so the
                    # account picker shows a real tier within an HTTP RTT.
                    asyncio.create_task(self._fetch_tier_for(conn))
            return

        if t == "user_info":
            info = data.get("userInfo")
            if isinstance(info, dict):
                # Whitelist on intake — Google's userinfo can carry id / locale
                # / hd / given_name etc. Clamp at the door so nothing leaks PII.
                allowed = ("email", "name", "picture", "verified_email")
                clean = {k: info[k] for k in allowed if k in info}
                if conn is not None:
                    conn.user_info = clean
                if mirror:
                    self._user_info = clean
                logger.info("user_info captured for %s", clean.get("email") or "<no email>")
            return

        if t == "pong":
            return

        # Inbound response (legacy path; production flow uses HTTP callback)
        req_id = data.get("id")
        if req_id and req_id in self._pending:
            self._resolve(req_id, data)

    def resolve_callback(self, data: dict) -> bool:
        """Called by the HTTP callback endpoint after validating the secret.

        Returns True if a pending future matched.
        """
        req_id = data.get("id")
        if not req_id or req_id not in self._pending:
            return False
        self._resolve(req_id, data)
        return True

    def _resolve(self, req_id: str, data: dict) -> None:
        fut = self._pending.pop(req_id, None)
        if not fut or fut.done():
            return
        # Count as failure if (a) an explicit `error` field is set OR
        # (b) the HTTP status is a 4xx/5xx. Otherwise success.
        status = data.get("status")
        http_error = isinstance(status, int) and status >= 400
        explicit_error = bool(data.get("error"))
        if http_error or explicit_error:
            self._failed_count += 1
            msg = data.get("error") or f"API_{status}"
            self._last_error = str(msg)[:200]
            fut.set_result(data)
        else:
            self._success_count += 1
            fut.set_result(data)

    # ── outbound ──────────────────────────────────────────────────────────
    async def notify(self, message: dict) -> bool:
        """Fire-and-forget WS push to the active extension. Returns False when
        no extension is connected so callers can surface a diagnostic.

        Used by the logout flow (tell extension to clear its in-memory token +
        cached userinfo) and the scan flow (re-fetch userinfo).
        """
        if not self.connected or self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(message))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify failed: %s", exc)
            return False

    async def _send(self, method: str, params: dict, timeout: Optional[float] = None) -> dict:
        if not self.connected:
            return {"error": "extension_disconnected"}

        req_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._request_count += 1

        payload = {"id": req_id, "method": method, "params": params}
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=timeout or self.DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._failed_count += 1
            self._last_error = "timeout"
            return {"error": "timeout"}
        except ConnectionError as exc:
            self._pending.pop(req_id, None)
            self._failed_count += 1
            self._last_error = str(exc)
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(req_id, None)
            self._failed_count += 1
            self._last_error = str(exc)
            return {"error": str(exc)}

    async def api_request(
        self,
        url: str,
        method: str = "POST",
        headers: Optional[dict] = None,
        body: Any = None,
        captcha_action: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Proxy an HTTP call against aisandbox-pa.googleapis.com through the
        active extension's browser session. If ``captcha_action`` is set, the
        extension solves reCAPTCHA on an active Flow tab before firing the
        fetch and injects the token into the body's recaptchaContext fields.
        """
        params: dict[str, Any] = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
        }
        if captcha_action:
            params["captchaAction"] = captcha_action
        return await self._send("api_request", params, timeout=timeout)

    async def trpc_request(
        self,
        url: str,
        method: str = "POST",
        headers: Optional[dict] = None,
        body: Any = None,
        timeout: Optional[float] = 30.0,
    ) -> dict:
        """Proxy a TRPC call against labs.google through the active extension.

        No captcha; just Bearer auth passthrough on a `credentials: include`
        fetch. Used for metadata calls like ``project.createProject``.
        """
        return await self._send(
            "trpc_request",
            {"url": url, "method": method, "headers": headers or {}, "body": body},
            timeout=timeout,
        )

    # ── observability ─────────────────────────────────────────────────────
    @property
    def ws_stats(self) -> dict:
        token_age = (
            int(time.time() - self._token_captured_at)
            if self._token_captured_at is not None
            else None
        )
        return {
            "connected": self.connected,
            "flow_key_present": self._flow_key_present,
            "token_age_s": token_age,
            "pending": len(self._pending),
            "request_count": self._request_count,
            "success_count": self._success_count,
            "failed_count": self._failed_count,
            "last_error": self._last_error,
            "connection_count": len(self._conns),
            "active_connection": self._active,
        }


flow_client = FlowClient()
