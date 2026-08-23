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

★ Booting the real app also runs the real app's startup side effects, and one of
them WROTE TO THE REPO: register_ambassador_routes() runs two full ambassador
cycles at registration, each appending generated partner-outreach drafts to the
TRACKED data/ambassador_state.json. Every `pytest tests/` left that file
modified (+44/-2), so `git add -A` swept machine-generated outreach copy into
real commits and a dirty `git status` hid genuine uncommitted work. The write is
redirected to tmp_path via DCHUB_AMBASSADOR_STATE_FILE, and the digest check
below fails if this gate ever dirties data/ again — by that route or a new one.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATE = os.path.join(_ROOT, "scripts", "app_contract_gate.py")
_DATA = os.path.join(_ROOT, "data")
_STRICT = os.environ.get("DCHUB_CONTRACT_GATE_STRICT") == "1"


def _data_digests() -> dict:
    """sha256 of every file under data/ — all of them are tracked, so any
    rewrite here shows up as a dirty working tree after the suite runs.

    Deliberately plain listdir/isfile, not glob/rglob: tests/_scan_floors.py
    wraps the scan primitives to police repo-coverage floors, and this is
    bookkeeping, not a repo scan.
    """
    out = {}
    if not os.path.isdir(_DATA):
        return out
    for name in sorted(os.listdir(_DATA)):
        path = os.path.join(_DATA, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_app_boots_and_serves_its_contract(tmp_path):
    env = dict(os.environ)
    env.setdefault("JWT_SECRET", "contract-gate-placeholder-not-a-secret")
    env.setdefault("DCHUB_ADMIN_KEY", "contract-gate-placeholder")
    # Booting the app runs two ambassador cycles that persist their state.
    # Send that write to tmp_path instead of the tracked repo file. Always
    # override — never setdefault — so an inherited value cannot aim it back at
    # the working tree.
    env["DCHUB_AMBASSADOR_STATE_FILE"] = str(tmp_path / "ambassador_state.json")
    before = _data_digests()
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

    after = _data_digests()
    dirtied = sorted(n for n, d in before.items() if after.get(n) != d)
    assert not dirtied, (
        "booting the app rewrote tracked repo state under data/: "
        + ", ".join(dirtied)
        + ".\n\nRunning the test suite must not modify the working tree — a "
        "dirty `git status` gets swept into commits by `git add -A` and hides "
        "genuine uncommitted work. Give the writer an env-overridable path (see "
        "STATE_FILE in agentic_ambassador.py) and point it at tmp_path here."
    )


def test_the_gate_cannot_dirty_tracked_repo_state_however_it_is_invoked():
    """★ The digest check above only bites when the SUITE runs the gate.

    A human typing `python3 scripts/app_contract_gate.py` gets neither the
    tmp_path redirect nor the sha256 comparison — and that is how three
    generated ambassador outreach drafts, stamped 2026-08-22T22:41-22:42,
    reached a pull request that had nothing to do with them (shell #65 audit),
    leaving data/ambassador_state.json and the `total_outreach` counter that
    /status publishes as outreach.total_sent disagreeing by 3.

    So the gate now defaults the redirect itself. Pinned by executing the
    shipped function against a plain dict (no boot, no deps), plus AST proof
    that boot() actually calls it — a comment saying it does cannot satisfy it.
    """
    import ast
    import tempfile

    src = open(_GATE, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "_isolate_repo_state"), None)
    assert fn is not None, (
        "EXTRACTION EMPTY: scripts/app_contract_gate.py no longer defines "
        "_isolate_repo_state — booting it by hand can dirty tracked data/ again")
    ns = {"os": os, "tempfile": tempfile}
    exec(compile(ast.Module([fn], []), _GATE, "exec"), ns)
    isolate = ns["_isolate_repo_state"]

    env = {}
    path = isolate(env)
    assert env["DCHUB_AMBASSADOR_STATE_FILE"] == path
    assert not os.path.abspath(path).startswith(os.path.abspath(_ROOT) + os.sep), (
        f"the gate still aims ambassador state INSIDE the repo: {path}")
    # an explicit value always wins (the suite's own tmp_path redirect)
    assert isolate({"DCHUB_AMBASSADOR_STATE_FILE": "/somewhere/else.json"}) == \
        "/somewhere/else.json"
    # an empty string is not a value
    assert isolate({"DCHUB_AMBASSADOR_STATE_FILE": "  "}) != "  "

    boot_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "boot")
    assert [c for c in ast.walk(boot_fn) if isinstance(c, ast.Call)
            and getattr(c.func, "id", None) == "_isolate_repo_state"], (
        "boot() no longer calls _isolate_repo_state() — the default is inert")
