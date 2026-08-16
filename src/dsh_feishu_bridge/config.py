"""Configuration loading — environment variables only, nothing sensitive ever
touches a config file that could land in git.

Feishu app credentials and ``DEEPSEEK_API_KEY`` are read straight from the
process environment. An optional YAML file (``DSH_FEISHU_BRIDGE_CONFIG``,
see examples/config.example.yaml) can supply the non-secret allowlists and
model knobs, but env vars always win when both are set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised for incomplete/inconsistent configuration — a hard boot
    failure, never a silent partial start."""


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    # Feishu
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_transport: str = "ws"
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_domain: str = "https://open.feishu.cn"
    feishu_allowed_open_ids: list[str] = field(default_factory=list)
    feishu_allowed_chat_ids: list[str] = field(default_factory=list)

    # dsh
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    dsh_provider: str = "deepseek-official"
    dsh_model: str = "deepseek-v4-flash"
    dsh_max_tokens: int | None = None
    dsh_cordis: str | None = None
    dsh_session_root: str | None = None
    dsh_workspace: str | None = None

    # HTTP server (webhook transport + health check)
    host: str = "0.0.0.0"
    port: int = 8788


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from an optional YAML file, then apply env-var
    overrides on top. ``config_path`` defaults to the
    ``DSH_FEISHU_BRIDGE_CONFIG`` env var; no file is required — a
    fully-env-var deployment (Docker, systemd) works with no file at all.
    """
    settings = Settings()

    path = config_path or os.environ.get("DSH_FEISHU_BRIDGE_CONFIG")
    if path:
        data = _load_yaml(Path(path))
        _apply_yaml(settings, data)

    _apply_env(settings)
    return settings


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a YAML mapping")
    return data


def _apply_yaml(settings: Settings, data: dict[str, Any]) -> None:
    feishu = data.get("feishu") or {}
    settings.feishu_transport = feishu.get("transport", settings.feishu_transport)
    settings.feishu_domain = feishu.get("domain", settings.feishu_domain)
    settings.feishu_allowed_open_ids = list(
        feishu.get("allowed_open_ids", settings.feishu_allowed_open_ids)
    )
    settings.feishu_allowed_chat_ids = list(
        feishu.get("allowed_chat_ids", settings.feishu_allowed_chat_ids)
    )

    dsh = data.get("dsh") or {}
    settings.dsh_provider = dsh.get("provider", settings.dsh_provider)
    settings.dsh_model = dsh.get("model", settings.dsh_model)
    settings.dsh_max_tokens = dsh.get("max_tokens", settings.dsh_max_tokens)
    settings.dsh_cordis = dsh.get("cordis", settings.dsh_cordis)
    settings.dsh_session_root = dsh.get("session_root", settings.dsh_session_root)
    settings.dsh_workspace = dsh.get("workspace", settings.dsh_workspace)

    server = data.get("server") or {}
    settings.host = server.get("host", settings.host)
    settings.port = server.get("port", settings.port)


def _apply_env(settings: Settings) -> None:
    env = os.environ
    settings.feishu_app_id = env.get("FEISHU_APP_ID", settings.feishu_app_id)
    settings.feishu_app_secret = env.get("FEISHU_APP_SECRET", settings.feishu_app_secret)
    settings.feishu_transport = env.get("FEISHU_TRANSPORT", settings.feishu_transport)
    settings.feishu_verification_token = env.get(
        "FEISHU_VERIFICATION_TOKEN", settings.feishu_verification_token
    )
    settings.feishu_encrypt_key = env.get("FEISHU_ENCRYPT_KEY", settings.feishu_encrypt_key)
    settings.feishu_domain = env.get("FEISHU_DOMAIN", settings.feishu_domain)
    if "FEISHU_ALLOWED_OPEN_IDS" in env:
        settings.feishu_allowed_open_ids = _split_csv(env["FEISHU_ALLOWED_OPEN_IDS"])
    if "FEISHU_ALLOWED_CHAT_IDS" in env:
        settings.feishu_allowed_chat_ids = _split_csv(env["FEISHU_ALLOWED_CHAT_IDS"])

    settings.deepseek_api_key = env.get("DEEPSEEK_API_KEY", settings.deepseek_api_key)
    # `or` on purpose, not `.get(key, default)`: an env referencing an
    # unconfigured secret (e.g. GitHub Actions `${{ secrets.X }}` for a secret
    # that was never created) sets the var to "" rather than omitting it. An
    # empty base_url breaks the dsh runtime's request construction outright —
    # reproduced locally against CI run 31948093525 — so "set but empty" must
    # be treated the same as "unset", not as an explicit override.
    settings.deepseek_base_url = env.get("DEEPSEEK_BASE_URL") or settings.deepseek_base_url
    settings.dsh_provider = env.get("DSH_PROVIDER", settings.dsh_provider)
    settings.dsh_model = env.get("DSH_MODEL", settings.dsh_model)
    if "DSH_MAX_TOKENS" in env:
        settings.dsh_max_tokens = int(env["DSH_MAX_TOKENS"])
    settings.dsh_cordis = env.get("DSH_CORDIS", settings.dsh_cordis)
    settings.dsh_session_root = env.get("DSH_SESSION_ROOT", settings.dsh_session_root)
    settings.dsh_workspace = env.get("DSH_WORKSPACE", settings.dsh_workspace)

    settings.host = env.get("DSH_FEISHU_BRIDGE_HOST", settings.host)
    if "DSH_FEISHU_BRIDGE_PORT" in env:
        settings.port = int(env["DSH_FEISHU_BRIDGE_PORT"])
