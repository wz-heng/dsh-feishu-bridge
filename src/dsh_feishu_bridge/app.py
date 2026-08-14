"""FastAPI app: mounts the Feishu webhook route (webhook transport only) and
a health check, and drives bridge start/stop off the app lifespan.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .bridges.feishu import build_feishu_bridge
from .bridges.manager import BridgeManager
from .config import Settings
from .dsh_adapter import DshAdapter, DshAdapterConfig
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    adapter = DshAdapter(
        DshAdapterConfig(
            provider=settings.dsh_provider,
            model=settings.dsh_model,
            max_tokens=settings.dsh_max_tokens,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            cwd=settings.dsh_workspace,
            session_root=settings.dsh_session_root,
            cordis=settings.dsh_cordis,
        )
    )
    session_mgr = SessionManager(adapter)
    manager = BridgeManager(session_mgr)

    feishu_bridge = build_feishu_bridge(manager, settings)
    if feishu_bridge is None:
        raise RuntimeError(
            "Feishu credentials not configured — set FEISHU_APP_ID and "
            "FEISHU_APP_SECRET (see README quickstart)."
        )
    manager.register_bridge(feishu_bridge)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.register_broadcast()
        await manager.start_all()
        try:
            yield
        finally:
            await manager.stop_all()
            manager.unregister_broadcast()
            await session_mgr.shutdown()

    app = FastAPI(title="dsh-feishu-bridge", lifespan=lifespan)
    app.state.manager = manager
    app.state.session_manager = session_mgr
    app.state.feishu_bridge = feishu_bridge

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
