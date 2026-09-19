"""Action gates must not drift between backend and worker (2026-09-18).

RAG_MASTER_ARM was set on dchub-backend only. GET /admin/rag/master-state
runs on WEB and reported mode "armed" for 78 consecutive ticks, while the
tick itself — relayed to the WORKER per main.py's worker-owned list —
recorded "SHADOW (set RAG_MASTER_ARM=1 to act)" every single time. The shell
measured and scored daily for three months and never once acted.

check_env_drift could not see it: routes.env_drift.SHARED_CRITICAL_VARS held
26 entries and every one was a secret or a DB URL. A flag that decides
whether a worker-owned shell ACTS is exactly as drift-sensitive as a key.

This test makes the next one impossible to ship silently. Any env flag whose
name looks like an action gate, read by a module that owns a WORKER-RELAYED
route, must be EITHER in SHARED_CRITICAL_VARS or in _EXEMPT below with a
stated reason. Silence is not an option — that is the whole point.

Run: python3 -m pytest tests/test_env_drift_action_gates.py -v
"""
import ast
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.env_drift import SHARED_CRITICAL_VARS  # noqa: E402

# Flags that are deliberately NOT shared. Each needs a reason a reader can
# check. An entry here is a claim that the flag may legitimately differ
# between the two services — adding one wrongly means a shell can sit dead
# exactly as the RAG shell did, so do not add one to silence this test.
_EXEMPT = {
    # (none today — every action gate found on a worker-relayed module is
    # currently shared. Add here only with a reason, never to go green.)
}

_GATE_RE = re.compile(
    r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*(?:_ARM|_ENABLED|_ACT))["\']')


def _worker_relayed_routes():
    """The route paths main.py relays to the worker. These execute OFF the
    web process, which is exactly where an env flag can silently differ."""
    src = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    # the relay list is a run of quoted /api/v1/... literals
    return set(re.findall(r"'(/api/v1/[^']+)'", src))


def _module_routes(path):
    src = io.open(path, encoding="utf-8", errors="replace").read()
    return set(re.findall(r'route\(\s*["\'](/api/v1/[^"\']+)["\']', src)) | \
        set(re.findall(r'\.(?:post|get)\(\s*["\'](/api/v1/[^"\']+)["\']', src))


def _worker_owned_modules():
    relay = _worker_relayed_routes()
    out = {}
    rdir = os.path.join(ROOT, "routes")
    for fn in sorted(os.listdir(rdir)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(rdir, fn)
        shared = _module_routes(p) & relay
        if shared:
            out[p] = sorted(shared)
    return out


def test_the_scan_finds_worker_owned_modules():
    """★ FLOOR. A scan that finds nothing would report a clean bill of health
    forever — the defect this whole family of checks exists to catch."""
    mods = _worker_owned_modules()
    assert len(mods) >= 5, (
        f"only {len(mods)} worker-relayed modules found — the relay-list or "
        f"route regex is broken, and this test proves nothing while it is")


def _all_worker_side_gates():
    found = {}
    for path in _worker_owned_modules():
        src = io.open(path, encoding="utf-8", errors="replace").read()
        for flag in set(_GATE_RE.findall(src)):
            found.setdefault(flag, []).append(os.path.basename(path))
    return found


def test_the_gate_regex_finds_gates():
    """★ THE SECOND FLOOR, and the one the first mutation run proved was
    missing. test_the_scan_finds_worker_owned_modules only counts MODULES —
    with a broken _GATE_RE the module scan still succeeds, zero gates are
    found, and 'every gate is shared' passes vacuously forever.

    A scan that can find nothing needs a floor on what it finds."""
    gates = _all_worker_side_gates()
    assert len(gates) >= 5, (
        f"_GATE_RE found only {len(gates)} action gates on worker-relayed "
        f"modules ({sorted(gates)}) — the regex is broken, and while it is, "
        f"the sharing assertion proves nothing")


def test_rag_master_arm_is_shared():
    """The one that cost 78 ticks. Pinned by name so it cannot regress."""
    assert "RAG_MASTER_ARM" in SHARED_CRITICAL_VARS


def test_every_worker_side_action_gate_is_shared_or_exempt():
    """★ THE GUARD. A new worker-owned shell cannot ship an action gate that
    drifts invisibly between the two services."""
    undeclared = {}
    for path, routes in _worker_owned_modules().items():
        src = io.open(path, encoding="utf-8", errors="replace").read()
        for flag in set(_GATE_RE.findall(src)):
            if flag in SHARED_CRITICAL_VARS or flag in _EXEMPT:
                continue
            undeclared.setdefault(flag, []).append(
                f"{os.path.basename(path)} (owns {routes[0]})")
    assert not undeclared, (
        "action gates on worker-relayed modules that are neither shared nor "
        "exempt:\n  "
        + "\n  ".join(f"{k}: {', '.join(v)}" for k, v in sorted(undeclared.items()))
        + "\n\nAdd to routes.env_drift.SHARED_CRITICAL_VARS so drift files a "
          "finding, or to _EXEMPT in this file WITH A REASON if the flag may "
          "legitimately differ per service.")


def test_exempt_entries_are_not_also_shared():
    """An exemption that is also shared is a contradiction — one of the two
    statements is stale."""
    both = set(_EXEMPT) & set(SHARED_CRITICAL_VARS)
    assert not both, f"declared both shared and exempt: {sorted(both)}"


def test_unset_flag_is_not_reported_as_drift():
    """Both services unset must NOT look like drift, or every flag added here
    would fire on day one and the detector would be muted."""
    from routes.env_drift import env_fingerprints
    saved = os.environ.pop("RAG_MASTER_ARM", None)
    try:
        assert env_fingerprints().get("RAG_MASTER_ARM") is None
    finally:
        if saved is not None:
            os.environ["RAG_MASTER_ARM"] = saved


def test_set_flag_produces_a_comparable_fingerprint():
    """The mirror: a set flag must fingerprint, or drift on it is invisible
    in the other direction."""
    from routes.env_drift import env_fingerprints
    saved = os.environ.get("RAG_MASTER_ARM")
    os.environ["RAG_MASTER_ARM"] = "1"
    try:
        fp = env_fingerprints().get("RAG_MASTER_ARM")
        assert fp and len(fp) == 12
    finally:
        if saved is None:
            os.environ.pop("RAG_MASTER_ARM", None)
        else:
            os.environ["RAG_MASTER_ARM"] = saved
