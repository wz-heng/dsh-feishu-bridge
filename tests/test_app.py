"""app.py wiring: health endpoint, webhook route gating, missing-credential
failure. Uses webhook transport with a loopback domain so the lifespan never
dials out to real Feishu infrastructure (ws mode dials on start; webhook mode
"returns ready without dialing" — see FeishuBridge.start)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dsh_feishu_bridge.app import _APPROVAL_RELAY_TIMEOUT_MARGIN_SECONDS, build_app
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
        # The Node relay's own fallback timeout must be strictly larger than
        # the gateway's deny-timeout — see _APPROVAL_RELAY_TIMEOUT_MARGIN_SECONDS
        # in app.py for why (avoids racing the gateway's own deny response).
        expected_ms = int((12.0 + _APPROVAL_RELAY_TIMEOUT_MARGIN_SECONDS) * 1000)
        assert adapter._config.env["DSH_APPROVAL_TIMEOUT_MS"] == str(expected_ms)
        assert expected_ms > 12000
        assert adapter._config.cordis is not None
        assert adapter._config.cordis.endswith("cordis.yml")


def test_approval_mode_with_custom_cordis_raises():
    with pytest.raises(RuntimeError, match="DSH_APPROVAL_MODE"):
        build_app(_settings(dsh_approval_mode=True, dsh_cordis="/custom/cordis.yml"))


def test_approval_mode_injects_loopback_no_proxy(monkeypatch):
    """The runtime subprocess inherits the full parent env (deepseek_harness
    merges DshAdapterConfig.env on TOP of os.environ.copy(), never replacing
    it) — so a system-wide proxy left un-exempted for 127.0.0.1/localhost can
    intercept `approval-relay.mjs`'s loopback callback. Regression test for
    the real-machine failure where that callback got proxied into a 502 and
    the approval turn hung with no card ever shown."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    app = build_app(_settings(dsh_approval_mode=True))
    with TestClient(app):
        adapter = app.state.session_manager._backend
        assert adapter._config.env["no_proxy"] == "127.0.0.1,localhost"
        assert adapter._config.env["NO_PROXY"] == "127.0.0.1,localhost"


def test_approval_mode_merges_into_existing_no_proxy(monkeypatch):
    """A caller's own no_proxy entries (other bypass rules) must survive the
    injection, not be clobbered by it."""
    monkeypatch.setenv("no_proxy", "example.internal,10.0.0.1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    app = build_app(_settings(dsh_approval_mode=True))
    with TestClient(app):
        adapter = app.state.session_manager._backend
        merged = adapter._config.env["no_proxy"]
        assert merged == "example.internal,10.0.0.1,127.0.0.1,localhost"


def test_approval_mode_does_not_duplicate_existing_loopback_entry(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,example.internal")
    monkeypatch.delenv("NO_PROXY", raising=False)
    app = build_app(_settings(dsh_approval_mode=True))
    with TestClient(app):
        adapter = app.state.session_manager._backend
        merged = adapter._config.env["no_proxy"]
        assert merged == "127.0.0.1,example.internal,localhost"


def test_approval_mode_off_does_not_touch_no_proxy():
    """no_proxy injection is scoped to approval mode — it's the loopback
    callback channel it exists to protect; a plain deployment with no
    ApprovalGateway has nothing on 127.0.0.1 to exempt."""
    app = build_app(_settings())
    with TestClient(app):
        adapter = app.state.session_manager._backend
        assert "no_proxy" not in adapter._config.env
        assert "NO_PROXY" not in adapter._config.env
