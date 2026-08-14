"""__main__.py: config/credential failures must exit cleanly via the
"dsh-feishu-bridge: ..." message, never an unhandled traceback."""

from __future__ import annotations

import pytest

from dsh_feishu_bridge.__main__ import main


@pytest.fixture
def clean_env(monkeypatch):
    for key in [
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TRANSPORT",
        "FEISHU_VERIFICATION_TOKEN", "FEISHU_ALLOWED_OPEN_IDS",
        "DSH_FEISHU_BRIDGE_CONFIG",
    ]:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_missing_credentials_exits_cleanly(clean_env, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dsh-feishu-bridge"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "Feishu credentials not configured" in capsys.readouterr().err


def test_half_configured_webhook_exits_cleanly_not_traceback(clean_env, monkeypatch, capsys):
    # Snape nit: FeishuConfigError previously wasn't in __main__'s except
    # tuple, so this case raised an unhandled traceback instead of the
    # friendly "dsh-feishu-bridge: ..." error path.
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
    monkeypatch.setenv("FEISHU_TRANSPORT", "webhook")
    # FEISHU_VERIFICATION_TOKEN intentionally left unset.
    monkeypatch.setattr("sys.argv", ["dsh-feishu-bridge"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "verification_token" in capsys.readouterr().err
