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


def test_approval_mode_off_by_default_no_gateway():
    app = build_app(_settings())
    assert app.state.approval_gateway is None


def test_approval_mode_starts_gateway_and_wires_the_harness_subprocess_env():
    app = build_app(_settings(dsh_approval_mode=True, dsh_approval_timeout_seconds=12.0))
    with TestClient(app):
        gateway = app.state.approval_gateway
        assert gateway is not None
        assert gateway.port is not None

        adapter = app.state.session_manager._backend
        assert adapter._config.env["DSH_APPROVAL_CALLBACK_URL"] == gateway.url
        assert adapter._config.env["DSH_APPROVAL_TIMEOUT_MS"] == "12000"
        assert adapter._config.cordis is not None
        assert adapter._config.cordis.endswith("cordis.yml")


def test_approval_mode_with_custom_cordis_raises():
    with pytest.raises(RuntimeError, match="DSH_APPROVAL_MODE"):
        build_app(_settings(dsh_approval_mode=True, dsh_cordis="/custom/cordis.yml"))
