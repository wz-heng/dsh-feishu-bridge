from __future__ import annotations

import pytest

from dsh_feishu_bridge.config import ConfigError, load_settings


@pytest.fixture
def clean_env(monkeypatch):
    for key in [
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TRANSPORT",
        "FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY", "FEISHU_DOMAIN",
        "FEISHU_ALLOWED_OPEN_IDS", "FEISHU_ALLOWED_CHAT_IDS",
        "FEISHU_PAIRING", "FEISHU_PAIRING_TTL_SECONDS",
        "FEISHU_PAIRING_MAX_ATTEMPTS", "FEISHU_PAIRING_CODE_LENGTH",
        "FEISHU_PAIRING_STATE_PATH",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DSH_PROVIDER", "DSH_MODEL",
        "DSH_MAX_TOKENS", "DSH_CORDIS", "DSH_SESSION_ROOT", "DSH_WORKSPACE",
        "DSH_APPROVAL_MODE", "DSH_APPROVAL_TIMEOUT_SECONDS",
        "DSH_FEISHU_BRIDGE_HOST", "DSH_FEISHU_BRIDGE_PORT",
        "DSH_FEISHU_BRIDGE_CONFIG",
    ]:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_defaults(clean_env):
    s = load_settings()
    assert s.feishu_transport == "ws"
    assert s.feishu_domain == "https://open.feishu.cn"
    assert s.feishu_allowed_open_ids == []
    assert s.dsh_provider == "deepseek-official"
    assert s.dsh_model == "deepseek-v4-flash"
    assert s.host == "0.0.0.0"
    assert s.port == 8788
    assert s.dsh_approval_mode is False
    assert s.dsh_approval_timeout_seconds == 60.0
    assert s.feishu_pairing_enabled is True
    assert s.feishu_pairing_ttl_seconds == 900.0
    assert s.feishu_pairing_max_attempts == 5
    assert s.feishu_pairing_code_length == 8
    assert s.feishu_pairing_state_path == "data/feishu_paired_open_ids.json"


def test_env_vars_applied(clean_env):
    clean_env.setenv("FEISHU_APP_ID", "cli_x")
    clean_env.setenv("FEISHU_APP_SECRET", "sec")
    clean_env.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_a, ou_b ,ou_c")
    clean_env.setenv("DSH_MAX_TOKENS", "4096")
    clean_env.setenv("DSH_FEISHU_BRIDGE_PORT", "9999")

    s = load_settings()
    assert s.feishu_app_id == "cli_x"
    assert s.feishu_app_secret == "sec"
    assert s.feishu_allowed_open_ids == ["ou_a", "ou_b", "ou_c"]
    assert s.dsh_max_tokens == 4096
    assert s.port == 9999


def test_empty_deepseek_base_url_env_is_treated_as_unset(clean_env):
    clean_env.setenv("DEEPSEEK_BASE_URL", "")
    s = load_settings()
    assert s.deepseek_base_url is None


def test_missing_config_file_raises(clean_env, tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_yaml_file_applied_then_env_overrides(clean_env, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
feishu:
  transport: webhook
  allowed_open_ids: ["ou_from_yaml"]
dsh:
  model: yaml-model
server:
  port: 1234
"""
    )
    s = load_settings(config_file)
    assert s.feishu_transport == "webhook"
    assert s.feishu_allowed_open_ids == ["ou_from_yaml"]
    assert s.dsh_model == "yaml-model"
    assert s.port == 1234

    # Env wins over YAML when both set.
    clean_env.setenv("DSH_MODEL", "env-model")
    s2 = load_settings(config_file)
    assert s2.dsh_model == "env-model"
    assert s2.feishu_transport == "webhook"  # untouched field still from YAML


def test_yaml_must_be_a_mapping(clean_env, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_settings(config_file)


class TestApprovalMode:
    def test_env_vars_applied(self, clean_env):
        clean_env.setenv("DSH_APPROVAL_MODE", "true")
        clean_env.setenv("DSH_APPROVAL_TIMEOUT_SECONDS", "12.5")
        s = load_settings()
        assert s.dsh_approval_mode is True
        assert s.dsh_approval_timeout_seconds == 12.5

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
    def test_truthy_spellings(self, clean_env, value):
        clean_env.setenv("DSH_APPROVAL_MODE", value)
        assert load_settings().dsh_approval_mode is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_spellings(self, clean_env, value):
        clean_env.setenv("DSH_APPROVAL_MODE", value)
        assert load_settings().dsh_approval_mode is False

    def test_yaml_applied(self, clean_env, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("dsh:\n  approval_mode: true\n  approval_timeout_seconds: 30\n")
        s = load_settings(config_file)
        assert s.dsh_approval_mode is True
        assert s.dsh_approval_timeout_seconds == 30.0

    def test_yaml_quoted_false_string_is_not_truthy(self, clean_env, tmp_path):
        # Snape nit: Python's bool("false") is True — a quoted YAML string
        # must not silently turn approval mode ON.
        config_file = tmp_path / "config.yaml"
        config_file.write_text('dsh:\n  approval_mode: "false"\n')
        assert load_settings(config_file).dsh_approval_mode is False

    def test_yaml_quoted_true_string_is_truthy(self, clean_env, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('dsh:\n  approval_mode: "true"\n')
        assert load_settings(config_file).dsh_approval_mode is True

    def test_conflicts_with_custom_cordis(self, clean_env):
        clean_env.setenv("DSH_APPROVAL_MODE", "1")
        clean_env.setenv("DSH_CORDIS", "/some/custom/cordis.yml")
        with pytest.raises(ConfigError, match="DSH_APPROVAL_MODE"):
            load_settings()


class TestPairingConfig:
    def test_env_vars_applied(self, clean_env):
        clean_env.setenv("FEISHU_PAIRING", "0")
        clean_env.setenv("FEISHU_PAIRING_TTL_SECONDS", "60")
        clean_env.setenv("FEISHU_PAIRING_MAX_ATTEMPTS", "3")
        clean_env.setenv("FEISHU_PAIRING_CODE_LENGTH", "10")
        clean_env.setenv("FEISHU_PAIRING_STATE_PATH", "/tmp/custom-paired.json")
        s = load_settings()
        assert s.feishu_pairing_enabled is False
        assert s.feishu_pairing_ttl_seconds == 60.0
        assert s.feishu_pairing_max_attempts == 3
        assert s.feishu_pairing_code_length == 10
        assert s.feishu_pairing_state_path == "/tmp/custom-paired.json"

    def test_yaml_applied(self, clean_env, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "feishu:\n"
            "  pairing:\n"
            "    enabled: false\n"
            "    ttl_seconds: 120\n"
            "    max_attempts: 2\n"
            "    code_length: 9\n"
            "    state_path: custom/paired.json\n"
        )
        s = load_settings(config_file)
        assert s.feishu_pairing_enabled is False
        assert s.feishu_pairing_ttl_seconds == 120.0
        assert s.feishu_pairing_max_attempts == 2
        assert s.feishu_pairing_code_length == 9
        assert s.feishu_pairing_state_path == "custom/paired.json"

    def test_yaml_quoted_false_string_is_not_truthy(self, clean_env, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('feishu:\n  pairing:\n    enabled: "false"\n')
        assert load_settings(config_file).feishu_pairing_enabled is False

    def test_env_wins_over_yaml(self, clean_env, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("feishu:\n  pairing:\n    enabled: false\n")
        clean_env.setenv("FEISHU_PAIRING", "1")
        assert load_settings(config_file).feishu_pairing_enabled is True

    @pytest.mark.parametrize("length", [1, 7])
    def test_code_length_below_floor_is_boot_failure(self, clean_env, length):
        clean_env.setenv("FEISHU_PAIRING_CODE_LENGTH", str(length))
        with pytest.raises(ConfigError, match="FEISHU_PAIRING_CODE_LENGTH"):
            load_settings()

    def test_code_length_floor_does_not_apply_when_pairing_disabled(self, clean_env):
        clean_env.setenv("FEISHU_PAIRING", "0")
        clean_env.setenv("FEISHU_PAIRING_CODE_LENGTH", "1")
        load_settings()  # must not raise

    def test_zero_max_attempts_is_boot_failure(self, clean_env):
        clean_env.setenv("FEISHU_PAIRING_MAX_ATTEMPTS", "0")
        with pytest.raises(ConfigError, match="FEISHU_PAIRING_MAX_ATTEMPTS"):
            load_settings()

    def test_non_positive_ttl_is_boot_failure(self, clean_env):
        clean_env.setenv("FEISHU_PAIRING_TTL_SECONDS", "0")
        with pytest.raises(ConfigError, match="FEISHU_PAIRING_TTL_SECONDS"):
            load_settings()
