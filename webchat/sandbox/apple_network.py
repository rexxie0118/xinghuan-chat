"""Apple Container network policy.

Apple Container has no ``--network none``, but it supports host-only networks
via ``container network create --internal``. We map ``none`` / ``isolated`` to a
dedicated host-only network, which blocks external egress while still allowing
the host-side lifecycle API.
"""

from __future__ import annotations

import subprocess

INTERNAL_NETWORK = "xh-isolated"


def network_policy(raw: str | None) -> tuple[str | None, bool]:
    """Return (network name or None, needs_internal_network)."""
    value = (raw or "default").strip()
    lowered = value.lower()
    if lowered in {"none", "isolated"}:
        return INTERNAL_NETWORK, True
    if lowered in {"", "default"}:
        return None, False
    return value, False


def ensure_internal_network(binary: str, *, name: str = INTERNAL_NETWORK) -> None:
    try:
        checked = subprocess.run(
            [binary, "network", "inspect", name],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if checked.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    subprocess.run(
        [binary, "network", "create", "--internal", name],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
