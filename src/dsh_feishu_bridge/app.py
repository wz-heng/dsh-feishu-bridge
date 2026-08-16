"""FastAPI app: mounts the Feishu webhook route (webhook transport only) and
a health check, and drives bridge start/stop off the app lifespan.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .approval_gateway import ApprovalGateway
from .approval_runtime import bundled_cordis_path
from .bridges.feishu import build_feishu_bridge
from .bridges.manager import BridgeManager
from .config import Settings
from .dsh_adapter import DshAdapter, DshAdapterConfig
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    # Populated below, in place, once the approval gateway is up (see
    # lifespan) — DshAdapterConfig.env is only ever READ lazily, on the
    # harness subprocess's first actual spawn (DshAdapter._ensure_started,
    # triggered by the first run_turn call), which always happens after
    # lifespan startup has finished. Kept as the SAME dict object end to
    # end so that later mutation is the one DshAdapter's config already
    # holds — see config.py's DSH_APPROVAL_MODE + DSH_CORDIS conflict check
    # for why `cordis` below is never both bundled and caller-supplied.
    env: dict[str, str] = {}
    cordis = settings.dsh_cordis
    if settings.dsh_approval_mode:
        if settings.dsh_cordis:
            # load_settings() already rejects this combination (config.py) —
            # this is a second, defensive gate for callers that build a
            # Settings object directly rather than going through it.
            raise RuntimeError(
                "DSH_APPROVAL_MODE is set together with a custom DSH_CORDIS — "
                "approval mode ships its own runtime composition; see README "
                "'Remote tool approval' for how to combine the two."
            )
        cordis = str(bundled_cordis_path())

    adapter = DshAdapter(
        DshAdapterConfig(
            provider=settings.dsh_provider,
            model=settings.dsh_model,
            max_tokens=settings.dsh_max_tokens,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            cwd=settings.dsh_workspace,
            session_root=settings.dsh_session_root,
            cordis=cordis,
            env=env,
        )
    )
    session_mgr = SessionManager(adapter)

    approval_gateway: ApprovalGateway | None = None
    if settings.dsh_approval_mode:
        approval_gateway = ApprovalGateway(
            notify=session_mgr.notify_tool_approval_request,
            session_exists=lambda sid: session_mgr.get_session(sid) is not None,
            timeout_seconds=settings.dsh_approval_timeout_seconds,
        )
    manager = BridgeManager(session_mgr, approval_gateway)

    feishu_bridge = build_feishu_bridge(manager, settings)
    if feishu_bridge is None:
        raise RuntimeError(
            "Feishu credentials not configured — set FEISHU_APP_ID and "
            "FEISHU_APP_SECRET (see README quickstart)."
        )
    manager.register_bridge(feishu_bridge)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if approval_gateway is not None:
            await approval_gateway.start()
            env["DSH_APPROVAL_CALLBACK_URL"] = approval_gateway.url
            env["DSH_APPROVAL_TIMEOUT_MS"] = str(
                int(settings.dsh_approval_timeout_seconds * 1000)
            )
        manager.register_broadcast()
        await manager.start_all()
        try:
            yield
        finally:
            await manager.stop_all()
            manager.unregister_broadcast()
            await session_mgr.shutdown()
            if approval_gateway is not None:
                await approval_gateway.stop()

    app = FastAPI(title="dsh-feishu-bridge", lifespan=lifespan)
    app.state.manager = manager
    app.state.session_manager = session_mgr
    app.state.feishu_bridge = feishu_bridge
    app.state.approval_gateway = approval_gateway

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "feishu_healthy": feishu_bridge.healthy}

    if feishu_bridge.transport == "webhook":

        @app.post("/feishu/webhook")
        async def feishu_webhook(request: Request) -> Response:
            body = await request.body()
            status_code, payload = await feishu_bridge.handle_webhook(
                dict(request.headers), body
            )
            return Response(
                content=payload, status_code=status_code, media_type="application/json"
            )

    return app
