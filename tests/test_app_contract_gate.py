"""The behavior gate, reachable from `pytest tests/`.

scripts/app_contract_gate.py boots the real application. That cannot happen
inside the pytest process: importing main.py registers ~741 blueprints, starts
background schedulers and mutates global state that the other ~8,800 tests
assume is absent. So this runs it as a SUBPROCESS — the suite gets the coverage,
the suite's process stays clean.

Skipped where the full runtime deps are absent. The light-install `unit-tests`
CI job legitimately lacks them, and so do most local checkouts; the dedicated
app-contract-gate workflow installs requirements.txt in full and is where this
check actually gates a merge. A skip here is never the only enforcement.

★ The skip is decided by WHY the boot failed, not by probing for a sentinel
module. The first version checked `import flask, psycopg2` — both of which the
light job HAS — so it ran there anyway and died on `dotenv`, turning every PR
red. Any single sentinel is one dependency-list edit away from the same bug.
Missing deps announce themselves as ModuleNotFoundError, so that is what is
keyed on.

To make sure this can never mask a genuinely missing dependency in the job that
enforces it, the workflow sets DCHUB_CONTRACT_GATE_STRICT=1. In strict mode a
ModuleNotFoundError is a hard failure — a full-requirements install that cannot
import a module is a real defect, and exactly the kind this gate exists to
catch.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATE = os.path.join(_ROOT, "scripts", "app_contract_gate.py")
_STRICT = os.environ.get("DCHUB_CONTRACT_GATE_STRICT") == "1"


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
    out = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0 and "ModuleNotFoundError" in out and not _STRICT:
        missing = "a runtime dependency"
        for line in out.splitlines():
            if "ModuleNotFoundError" in line:
                missing = line.strip()
                break
        pytest.skip(
            f"full runtime deps absent here ({missing}). The "
            f"app-contract-gate workflow runs this with requirements.txt "
            f"installed and DCHUB_CONTRACT_GATE_STRICT=1, where this same "
            f"condition is a hard failure."
        )

    assert proc.returncode == 0, (
        "The application failed its behavior gate. Unlike the static tests in "
        "this suite, this one booted the real app and checked what it serves:\n\n"
        + out[-5000:]
    )
