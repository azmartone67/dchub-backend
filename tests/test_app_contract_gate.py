"""The behavior gate, reachable from `pytest tests/`.

scripts/app_contract_gate.py boots the real application. That cannot happen
inside the pytest process: importing main.py registers ~741 blueprints, starts
background schedulers and mutates global state that the other ~8,800 tests
assume is absent. So this runs it as a SUBPROCESS — the suite gets the coverage,
the suite's process stays clean.

Skipped when the full runtime deps are not installed. Local runs and the
light-install `unit-tests` CI job legitimately lack them; the dedicated
app-contract-gate workflow installs requirements.txt in full and is where this
check actually gates a merge. A skip here is never the only enforcement.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATE = os.path.join(_ROOT, "scripts", "app_contract_gate.py")


def _deps_present() -> bool:
    try:
        import flask  # noqa: F401
        import psycopg2  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _deps_present(),
                    reason="full runtime deps absent; the app-contract-gate "
                           "workflow runs this with requirements.txt installed")
def test_app_boots_and_serves_its_contract():
    env = dict(os.environ)
    env.setdefault("JWT_SECRET", "contract-gate-placeholder-not-a-secret")
    env.setdefault("DCHUB_ADMIN_KEY", "contract-gate-placeholder")
    # The gate ends in os._exit because the app's non-daemon scheduler threads
    # block a normal interpreter shutdown. The timeout is a backstop for a hang
    # introduced upstream of that, not an expected path.
    proc = subprocess.run(
        [sys.executable, _GATE],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        "The application failed its behavior gate. Unlike the static tests in "
        "this suite, this one booted the real app and checked what it serves:\n\n"
        + (proc.stdout or "")[-4000:]
        + "\n"
        + (proc.stderr or "")[-2000:]
    )
