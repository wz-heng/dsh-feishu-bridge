"""Package data for approval mode's dsh runtime composition.

Two composition artifacts ship here, for the two SDK shapes
``dsh_adapter.py``'s "SDK-version compat shim" docstring documents:

- ``cordis.yml``: a FULL replacement plugin composition, used verbatim via
  ``DeepSeekHarnessConfig.cordis`` on an SDK old enough to still have that
  kwarg (deepseek-harness-sdk <= 0.1.1rc1).
- ``approval.patch.yml``: an overlay PATCH (id-targeted
  insert/config-override operations, not a full composition) applied on top
  of the runtime's own default ``"sdk"`` profile via
  ``DeepSeekHarnessConfig.patches`` on a newer SDK (>= 0.1.2) that dropped
  ``cordis`` for the profile+patches model.

Both (and the two ``.mjs`` cordis plugins they mount by relative path) ship
inside this Python package's wheel so the ``bundled_*_path()`` functions
resolve regardless of where ``dsh-feishu-bridge`` is installed — see
``dsh_adapter.py`` and ``app.py`` for how ``DshAdapterConfig.cordis``/
``.patches`` and ``DSH_APPROVAL_CALLBACK_URL`` get wired together.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def bundled_cordis_path() -> Path:
    """Path to the approval-mode ``cordis.yml`` shipped with this package."""
    return _PACKAGE_DIR / "cordis.yml"


def bundled_approval_patch_path() -> Path:
    """Path to the approval-mode ``approval.patch.yml`` shipped with this
    package — the ``patches``-model equivalent of :func:`bundled_cordis_path`
    for SDKs new enough to have dropped the ``cordis`` kwarg."""
    return _PACKAGE_DIR / "approval.patch.yml"
