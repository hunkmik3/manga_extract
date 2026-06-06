import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware

from flowboard.config import WS_HOST
from flowboard.db import get_session, init_db
from flowboard.db.models import Request
from flowboard.routes import activity, auth, boards, chat, comic, edges, flow_projects, llm, media, nodes, plans, projects, prompt, upload, vision
from flowboard.routes import references as references_route
from flowboard.routes import requests as requests_route
from flowboard.services.flow_client import flow_client
from flowboard.services.ws_server import run_ws_server
from flowboard.worker.processor import get_worker

# Guard rail: the dedicated WS server is unauthenticated and would expose the
# callback secret to any process that can reach it. Refuse to boot if someone
# overrode WS_HOST to a non-loopback address.
if WS_HOST not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError(
        f"FLOWBOARD_WS_HOST must be loopback (got {WS_HOST!r}); the extension WS "
        "is unauthenticated by design and must not be network-reachable."
    )

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _recover_orphan_running_requests() -> int:
    """Mark any pre-existing 'running' requests as failed so a restart doesn't
    leave nodes polling a request that nobody is processing anymore."""
    from datetime import datetime, timezone
    from sqlmodel import select as _select

    touched = 0
    with get_session() as s:
        rows = s.exec(_select(Request).where(Request.status == "running")).all()
        for r in rows:
            r.status = "failed"
            r.error = "agent_restart_lost"
            r.finished_at = datetime.now(timezone.utc)
            s.add(r)
            touched += 1
        if touched:
            s.commit()
    return touched


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered = _recover_orphan_running_requests()
    if recovered:
        logger.info("recovered %d orphan running request(s) → failed", recovered)
    worker = get_worker()
    ws_task = asyncio.create_task(run_ws_server(), name="ext-ws-server")
    worker_task = asyncio.create_task(worker.start(), name="request-worker")
    logger.info("flowboard agent started (ws:9223 + worker)")
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(worker.drain(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("worker drain timed out")
        for t in (ws_task, worker_task):
            t.cancel()
        await asyncio.gather(ws_task, worker_task, return_exceptions=True)
        logger.info("flowboard agent stopped")


app = FastAPI(title="Flowboard Agent", version="0.0.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(boards.router)
app.include_router(nodes.router)
app.include_router(edges.router)
app.include_router(chat.router)
app.include_router(projects.router)
app.include_router(flow_projects.router)
app.include_router(references_route.router)
app.include_router(requests_route.router)
app.include_router(media.bytes_router)
app.include_router(media.api_router)
app.include_router(upload.router)
app.include_router(plans.router)
app.include_router(vision.router)
app.include_router(prompt.router)
app.include_router(auth.router)
app.include_router(llm.router)
app.include_router(activity.router)
app.include_router(comic.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "extension_connected": flow_client.connected,
        "ws_stats": flow_client.ws_stats,
    }


@app.post("/api/ext/callback")
async def ext_callback(
    body: FastAPIRequest,
    x_callback_secret: str | None = Header(default=None, alias="X-Callback-Secret"),
) -> dict:
    """HTTP callback for the extension to deliver API responses."""
    if not x_callback_secret or not hmac.compare_digest(
        x_callback_secret, flow_client.callback_secret
    ):
        raise HTTPException(status_code=401, detail="invalid callback secret")

    try:
        payload = await body.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(status_code=400, detail="missing id")

    matched = flow_client.resolve_callback(payload)
    return {"ok": matched}


# ── Serve the bundled frontend (single-process / packaged exe) ──────────────
# Mounted LAST so every API/media route registered above wins; only unmatched
# GET paths fall through to the SPA. Skipped entirely in dev when there's no
# build (Vite serves :5173 and the agent stays API-only).
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from flowboard.config import frontend_dist_dir  # noqa: E402

_DIST = frontend_dist_dir()
if _DIST is not None:
    _DIST_RESOLVED = _DIST.resolve()
    _ASSETS = _DIST / "assets"
    if _ASSETS.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")

    logger.info("serving bundled frontend from %s", _DIST)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # Never shadow the API / media / ws surface — let those 404 naturally.
        if full_path.startswith(("api/", "media/", "ws", "assets/")):
            raise HTTPException(status_code=404, detail="not found")
        # Serve a real static file (favicon, vite.svg, …) when it exists and is
        # safely under dist/; otherwise hand back index.html for client routing.
        candidate = (_DIST / full_path).resolve()
        try:
            candidate.relative_to(_DIST_RESOLVED)
        except ValueError:
            raise HTTPException(status_code=404, detail="not found")
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_DIST / "index.html"))
