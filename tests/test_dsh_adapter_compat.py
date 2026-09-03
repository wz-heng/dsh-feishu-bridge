"""Unit coverage for the SDK-version compat shim itself (dsh_adapter.py's
``_build_harness_config`` / ``_harness_config_field_names`` /
``_default_dsh_home``) — independent of whichever ``deepseek-harness-sdk``
happens to be installed locally, by monkeypatching
``dsh_adapter_module.DeepSeekHarnessConfig`` with small stand-ins that mimic
the OLD (session_root/cordis) and NEW (dsh_home/profile/patches) real
shapes. See dsh_adapter.py's "SDK-version compat shim" docstring and
test_dsh_adapter_real_construction.py, which covers the same translation
against whichever real SDK is actually installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dsh_feishu_bridge import dsh_adapter as dsh_adapter_module
from dsh_feishu_bridge.dsh_adapter import DshAdapterConfig, DshAdapterError, _build_harness_config


@dataclass(slots=True)
class _OldShapeConfig:
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    max_tokens: int | None = None
    api_key: str | None = None
    base_url: str | None = None
    cwd: str | None = None
    session_root: str | None = None
    cordis: str | None = None
    request_timeout_seconds: float | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _NewShapeConfig:
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    max_tokens: int | None = None
    api_key: str | None = None
    base_url: str | None = None
    cwd: str | None = None
    profile: str = "sdk"
    patches: tuple[str, ...] = ()
    dsh_home: str | None = None
    request_timeout_seconds: float | None = None
    env: dict[str, str] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def _clear_field_probe_cache():
    # The probe is process-wide cached (see its own docstring) — tests swap
    # the patched class out from under it, so each test must start clean.
    dsh_adapter_module._harness_config_field_names.cache_clear()
    yield
    dsh_adapter_module._harness_config_field_names.cache_clear()


def test_old_shape_forwards_session_root_and_cordis(monkeypatch):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _OldShapeConfig)
    config = DshAdapterConfig(
        provider="p", model="m", session_root="/tmp/sessions", cordis="/tmp/cordis.yml"
    )

    built = _build_harness_config(config)

    assert isinstance(built, _OldShapeConfig)
    assert built.session_root == "/tmp/sessions"
    assert built.cordis == "/tmp/cordis.yml"


def test_new_shape_maps_session_root_to_dsh_home_verbatim(monkeypatch):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig(session_root="/tmp/sessions")

    built = _build_harness_config(config)

    assert isinstance(built, _NewShapeConfig)
    assert built.dsh_home == "/tmp/sessions"


def test_new_shape_falls_back_to_a_default_dsh_home_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig(cwd=str(tmp_path))

    built = _build_harness_config(config)

    # Never None — the new SDK raises at runtime start if dsh_home/DSH_HOME
    # both end up empty (see module docstring).
    assert built.dsh_home is not None
    assert built.dsh_home == str(tmp_path.resolve() / ".dsh-home")


def test_new_shape_forwards_patches_when_set(monkeypatch):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig(patches=("/tmp/approval.patch.yml",))

    built = _build_harness_config(config)

    assert built.patches == ("/tmp/approval.patch.yml",)


def test_new_shape_omits_patches_when_empty(monkeypatch):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig()

    built = _build_harness_config(config)

    assert built.patches == ()


def test_new_shape_raises_on_unsupported_custom_cordis(monkeypatch):
    # A caller-configured full-composition override has no equivalent on
    # this SDK shape — must fail loud, never silently drop it (that would
    # silently downgrade whatever behavior, e.g. approval mode's
    # fail-closed gate, that composition was providing).
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig(cordis="/tmp/custom-cordis.yml")

    with pytest.raises(DshAdapterError, match="DSH_CORDIS"):
        _build_harness_config(config)


def test_new_shape_drops_cordis_silently_when_patches_supplies_the_equivalent(monkeypatch):
    # This is exactly app.py's approval-mode wiring: it always sets BOTH
    # cordis (bundled_cordis_path(), for the old shape) and patches
    # (bundled_approval_patch_path(), for the new shape) on the same
    # DshAdapterConfig and lets capability detection pick the one the
    # installed SDK actually supports. On the new shape, cordis has no
    # field to land in — but unlike the bare-cordis case above, there IS a
    # new-shape equivalent already supplied via patches, so this must NOT
    # raise (caught live by the SDK canary against 0.1.2a3: the old code
    # raised unconditionally here, breaking approval mode on any new-shape
    # SDK even though patches was already wired correctly).
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _NewShapeConfig)
    config = DshAdapterConfig(
        cordis="/tmp/cordis.yml", patches=("/tmp/approval.patch.yml",)
    )

    built = _build_harness_config(config)

    assert built.patches == ("/tmp/approval.patch.yml",)
    assert not hasattr(built, "cordis")


def test_field_probe_is_cached(monkeypatch):
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarnessConfig", _OldShapeConfig)
    _build_harness_config(DshAdapterConfig())
    info_after_first = dsh_adapter_module._harness_config_field_names.cache_info()
    assert info_after_first.misses == 1

    _build_harness_config(DshAdapterConfig())
    info_after_second = dsh_adapter_module._harness_config_field_names.cache_info()
    assert info_after_second.misses == 1
    assert info_after_second.hits == 1
