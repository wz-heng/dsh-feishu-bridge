"""Package data for approval mode's dsh runtime composition.

``cordis.yml`` (and the two ``.mjs`` cordis plugins it mounts by relative
path) ship inside this Python package's wheel so ``bundled_cordis_path()``
resolves regardless of where ``dsh-feishu-bridge`` is installed — see
``dsh_adapter.py`` and ``app.py`` for how ``DshAdapterConfig.cordis`` and
``DSH_APPROVAL_CALLBACK_URL`` get wired together.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def bundled_cordis_path() -> Path:
    """Path to the approval-mode ``cordis.yml`` shipped with this package."""
    return _PACKAGE_DIR / "cordis.yml"
