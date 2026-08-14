"""app.py wiring: health endpoint, webhook route gating, missing-credential
failure. Uses webhook transport with a loopback domain so the lifespan never
dials out to real Feishu infrastructure (ws mode dials on start; webhook mode
"returns ready without dialing" — see FeishuBridge.start)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dsh_feishu_bridge.app import build_app
from dsh_feishu_bridge.config import Settings


def _settings(**over) -> Settings:
    s = Settings(
        feishu_app_id="cli_x",
        feishu_app_secret="sec",
        feishu_transport="webhook",
        feishu_verification_token="vtok",
        feishu_encrypt_key="ekey",
        feishu_domain="http://127.0.0.1:9",
        feishu_allowed_open_ids=["ou_me"],
    )
    for key, value in over.items():
        setattr(s, key, value)
    return s


def test_build_app_without_credentials_raises():
    with pytest.raises(RuntimeError):
        build_app(Settings())


def test_health_endpoint():
    app = build_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_webhook_route_mounted_in_webhook_mode():
    app = build_app(_settings())
    paths = {route.path for route in app.routes}
    assert "/feishu/webhook" in paths


def test_webhook_route_not_mounted_in_ws_mode():
    app = build_app(_settings(feishu_transport="ws", feishu_verification_token=None))
    paths = {route.path for route in app.routes}
    assert "/feishu/webhook" not in paths
