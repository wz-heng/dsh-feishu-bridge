"""Construction-only smoke test against the REAL ``deepseek_harness`` classes.

Every test in test_dsh_adapter.py monkeypatches ``DeepSeekHarness`` itself
(see its ``patched_harness`` autouse fixture), so a signature change to
``DeepSeekHarness.__init__`` or ``DeepSeekHarnessConfig`` would slip past the
entire rest of the suite — the only other place that touches the real SDK is
test_real_sdk_smoke.py, which needs a live ``DEEPSEEK_API_KEY`` and is
skipped everywhere except an opted-in real-key run.

This test never calls ``.start()``: the runtime subprocess is spawned lazily
by ``HarnessClient.start()``, not by ``DeepSeekHarness.__init__`` (verified
against deepseek_harness/api.py + client.py @ 0.1.0rc6 — the constructor only
builds config/env dicts and a not-yet-started ``HarnessClient``). So it costs
no API quota and needs no key, which is exactly what makes it a canary
tripwire: it's the one place a `deepseek-harness-sdk` constructor/signature
break shows up under a freshly-installed *latest* SDK
(.github/workflows/canary.yml) without a DEEPSEEK_API_KEY secret.

Goes through ``dsh_adapter._build_harness_config`` — the SAME
capability-detected translation ``DshAdapter._ensure_started`` uses — rather
than hand-rolling ``DeepSeekHarnessConfig(...)`` kwargs directly: that's
exactly what broke against 0.1.2a3 (``session_root``/``cordis`` dropped for
``dsh_home``/``profile``/``patches``, see dsh_adapter.py's "SDK-version
compat shim" docstring) while this test still hardcoded the old shape,
letting canary go red with no local repro. Testing through the real
translation means this test tracks whatever SDK is actually installed —
the pinned range locally, whatever the canary upgrades to in CI — without
needing to know which shape it is.
"""

from __future__ import annotations

from deepseek_harness import DeepSeekHarness

from dsh_feishu_bridge.dsh_adapter import DshAdapterConfig, _build_harness_config


def test_real_harness_constructs_from_adapter_config_fields(monkeypatch):
    """Mirrors the exact field mapping DshAdapter._ensure_started performs,
    against the real SDK class instead of the stub test_dsh_adapter.py
    substitutes it with."""

    def _fail_if_spawned(*_args, **_kwargs):
        raise AssertionError(
            "constructing DeepSeekHarness must not spawn the runtime subprocess"
        )

    # Guards the "construction never starts the runtime" invariant without
    # pinning the test to an SDK-private attribute name (e.g. HarnessClient's
    # own ``_proc`` field) — a rename there shouldn't turn the canary red,
    # only an actual eager-spawn behavior change should.
    monkeypatch.setattr("subprocess.Popen", _fail_if_spawned)

    adapter_config = DshAdapterConfig(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        max_tokens=64,
        api_key="test-key-never-sent-anywhere",
        base_url="https://example.invalid",
        cwd=".",
        session_root=None,
        cordis=None,
        request_timeout_seconds=5.0,
        env={},
    )

    harness = DeepSeekHarness(_build_harness_config(adapter_config))

    assert harness.config.provider == "deepseek-official"
    assert harness.config.model == "deepseek-v4-flash"
