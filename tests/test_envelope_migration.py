"""tests/test_envelope_migration.py — the bare-{} swallow stays dead (2026-08-12).

PR #2596 replaced brain_layer14_causal's `_internal` with util/internal_fetch,
because collapsing a timeout, a 500 and an honest empty into one `{}` had
consumed 17 of the brain's 20 live L18 lessons. Eight sibling modules still
carried their own copy. This guards the migration of all nine.

Ways it comes back:
  (1) REGROWTH — a module re-adds a private swallow (or a new module is written
      by copying an old one, which is how nine copies happened in the first
      place).
  (2) BUDGET-LOSS — L8's fetcher carries a (connect, read) tuple timeout that
      its docstring argues for at length; a migration that flattens it to a
      scalar silently restores the 60s+ in-flight blowup it was written to fix.
  (3) LEDGER-BLINDNESS — brain_capability_ledger lists only customer-facing
      gateway surfaces, so three already-shipped brain-internal capabilities
      were proposed as missing work in one session. The ledger cannot prevent
      a re-proposal of something it does not know about.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_envelope_migration.py -v
"""
from __future__ import annotations

import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every module migrated off a private bare-{} fetcher.
_MIGRATED = (
    "brain_layer14_causal.py",
    "brain_layer16_self_critique.py",
    "brain_layer18_memory_consolidation.py",
    "brain_layer8_orchestrator.py",
    "brain_layer9_conversational.py",
    "brain_layer15_auto_action.py",
    "brain_layer22_auto_code.py",
    "brain_fast_qa.py",
    "brain_layer19_awareness.py",
    "phx_live.py",
)

# ★2026-08-12: this file used to carry its OWN swallow regex and lane 1 of
# shell #63 carried a different one. They disagreed in both directions — the
# lane flagged radar.py and an auth helper, and BOTH missed phx_live's ternary
# form. The detector now lives in util/internal_fetch and is imported by the
# guard and the meter alike, so they cannot drift apart again.
def _detector():
    import importlib
    return importlib.import_module("util.internal_fetch").looks_like_swallowing_fetcher


def _src(rel: str) -> str:
    return (_ROOT / "routes" / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("mod", _MIGRATED)
def test_module_uses_the_envelope(mod):
    src = _src(mod)
    assert "util.internal_fetch" in src, \
        "%s no longer routes through the envelope" % mod


@pytest.mark.parametrize("mod", _MIGRATED)
def test_module_has_no_bare_swallow(mod):
    assert not _detector()(_src(mod)), \
        "%s re-grew a bare-{} status swallow" % mod


def test_no_new_swallowers_anywhere_in_routes():
    """★The regression that matters most: a NEW module copied from an old one.
    Nine copies of this bug existed because copying was easier than importing."""
    detect, offenders = _detector(), []
    for p in sorted((_ROOT / "routes").glob("*.py")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        if detect(src):
            offenders.append(p.name)
    assert offenders == [], (
        "modules carrying a private bare-{} internal fetcher:\n  "
        + "\n  ".join(offenders)
        + "\nImport util.internal_fetch.probe instead — a failure and an empty "
          "payload must not be the same value.")


def test_l8_keeps_its_tuple_timeout_budget():
    """L8 does 9-10 self-calls; its docstring argues for a 1s-connect/3s-read
    tuple so one slow chunk cannot blow the 60s in-flight budget. A migration
    that flattens it to a scalar reintroduces that outage quietly."""
    src = _src("brain_layer8_orchestrator.py")
    assert "probe(path, (1, timeout))" in src, \
        "L8 lost its (connect, read) tuple timeout"


def test_probe_forwards_a_tuple_timeout_untouched():
    """The claim L8's guard depends on, verified against the real function
    rather than assumed from the call site."""
    import importlib
    m = importlib.import_module("util.internal_fetch")
    seen = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": 1}

    class _FakeRequests:
        @staticmethod
        def get(url, timeout=None, headers=None):
            seen["timeout"] = timeout
            return _FakeResp()

    import sys
    sys.modules["requests"] = _FakeRequests
    try:
        env = m.probe("/x", (1, 3))
    finally:
        del sys.modules["requests"]
    assert seen["timeout"] == (1, 3), "probe mangled a tuple timeout"
    assert env["ok"] is True


# ── ledger blindness ──────────────────────────────────────────────────

def _ledger_src() -> str:
    return (_ROOT / "routes" / "brain_capability_ledger.py").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("marker,why", [
    ("LOOP_EDGES", "loop graph (#49) was re-proposed as missing work"),
    ("retrieve_prior_fixes", "fix-history recall was re-proposed as missing"),
    ("rag/retrieve", "authenticated any-corpus retrieve was re-proposed"),
    ("internal_fetch", "the envelope itself must be listed as BUILT"),
])
def test_ledger_knows_the_brain_internal_capabilities(marker, why):
    """★The ledger exists to stop re-proposing shipped work and could not,
    because it only listed customer-facing gateway surfaces. Each marker here
    is a capability that WAS re-proposed on 2026-08-11: %s"""
    assert marker in _ledger_src(), \
        "capability ledger has no entry mentioning %r — %s" % (marker, why)


def test_ledger_entries_are_well_formed():
    """A malformed tuple breaks the seed for EVERY capability, not just the new
    ones — the ledger would go quiet and the brain would resume re-proposing."""
    import importlib
    m = importlib.import_module("routes.brain_capability_ledger")
    caps = m._CURATED_CAPABILITIES
    assert len(caps) >= 4
    for row in caps:
        assert len(row) == 4, "capability row is not (name, loc, status, note)"
        name, loc, status, note = row
        assert all(isinstance(x, str) and x.strip() for x in row), \
            "empty field in capability %r" % (name,)
        assert status in ("LIVE", "FLAG-GATED", "INERT"), \
            "capability %r has unknown status %r" % (name, status)


def test_ledger_names_are_unique():
    """`name` is the table's PRIMARY KEY — a duplicate makes the ON CONFLICT
    upsert silently drop one entry, and the dropped one gets re-proposed."""
    import importlib
    m = importlib.import_module("routes.brain_capability_ledger")
    names = [r[0] for r in m._CURATED_CAPABILITIES]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, "duplicate capability names (PK collision): %s" % dupes


# ── the detector itself: both false directions ────────────────────────
# ★Every case below is a REAL string from this repo that the two divergent
# detectors got wrong on 2026-08-12, reported live by shell #63's lane 1.

_RADAR_CORRECT = 'def _internal(path: str, timeout: int = 6) -> tuple[dict, str | None]:\n    try:\n        import requests\n        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)\n        if r.status_code != 200:\n            return {}, f"HTTP {r.status_code}"\n        return (r.json() or {}), None\n    except Exception as e:\n        return {}, f"{type(e).__name__}: {str(e)[:80]}"\n'

_AUTH_HELPER = 'def _internal_ok(req) -> bool:\n    sent = (req.headers.get("X-Internal-Key") or "").strip()\n    if not sent:\n        return False\n    return sent == (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()\n'

_TERNARY_SWALLOW = 'def _internal(path: str, timeout: int = 6) -> dict:\n    try:\n        import requests\n        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)\n        return (r.json() or {}) if r.status_code == 200 else {}\n    except Exception:\n        return {}\n'

_CLASSIC_SWALLOW = 'def _internal(path: str, timeout: int = 8) -> dict:\n    try:\n        import requests\n        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)\n        if r.status_code != 200: return {}\n        return r.json() or {}\n    except Exception:\n        return {}\n'


def test_detector_spares_radars_reason_string():
    """radar.py returns `{}, "HTTP 502"` — the CORRECT shape. Lane 1 called it
    a swallower, and the obvious 'fix' would delete the reason string that took
    a two-week undiagnosed outage to learn."""
    assert _detector()(_RADAR_CORRECT) is False


def test_detector_spares_an_auth_helper():
    """_internal_ok(req) performs no fetch. It was flagged because the old
    pattern matched any `def _internal*` plus any `return {}` in the file."""
    assert _detector()(_AUTH_HELPER) is False


def test_detector_catches_the_ternary_form():
    """★The false NEGATIVE: phx_live wrote the swallow as a ternary and both
    detectors missed it for a full day."""
    assert _detector()(_TERNARY_SWALLOW) is True


def test_detector_catches_the_classic_form():
    assert _detector()(_CLASSIC_SWALLOW) is True


# ── lane 3 asks the app, not the file ─────────────────────────────────

def _shell_src() -> str:
    return (_ROOT / "routes" / "context_integrity_master_shell.py").read_text(
        encoding="utf-8")


def test_lane3_reads_live_blueprints_not_a_main_py_grep():
    """★webmcp_master_shell registers from cron_heartbeat._register_webmcp_shell,
    not main.py, and its live endpoint answers 403. Grepping one file reported
    it as dead code — a false red costs what a false green costs."""
    src = _shell_src()
    assert "current_app.blueprints" in src, \
        "lane 3 no longer asks the running app which blueprints exist"
    assert 'f[:-3] not in main_src' not in src, \
        "lane 3 re-grew the main.py text scan"


def test_lane3_is_indeterminate_without_an_app_context():
    """Outside a request context registration is UNKNOWN. Answering 'all
    registered' from ignorance is the confident-green this shell refuses."""
    import importlib
    s = importlib.import_module("routes.context_integrity_master_shell")
    checks = s._safe_lane(s._lane_retire)
    reg = [c for c in checks if c["id"] == "unregistered_shells"]
    assert reg, "lane 3 dropped its registration check"
    assert reg[0]["pass"] is None, \
        "lane 3 claimed a registration verdict with no app context"


def test_lane3_actually_reads_the_live_blueprint_set():
    """★MUT-driven: replacing current_app.blueprints with an empty set broke no
    test, because the only registration guard exercised the FALLBACK path. A
    guard that cannot tell the live branch from the dead one is not guarding
    it. This drives the real branch inside an app context."""
    import importlib

    from flask import Flask

    s = importlib.import_module("routes.context_integrity_master_shell")
    app = Flask(__name__)
    app.register_blueprint(s.context_integrity_master_shell_bp)
    with app.test_request_context("/"):
        checks = s._safe_lane(s._lane_retire)
    reg = [c for c in checks if c["id"] == "unregistered_shells"][0]

    # Inside a context the verdict must be REAL, not indeterminate...
    assert reg["pass"] is not None, \
        "lane 3 fell back to 'unknown' despite a live app context"
    # ...and it must say what it read it from.
    assert "live app" in reg["detail"], \
        "lane 3 no longer reports the basis of its registration verdict"
    # Only ONE shell blueprint is registered on this bare test app, so every
    # OTHER master shell must be reported unregistered. If this comes back
    # "all registered", the check is not reading the set at all.
    assert reg["pass"] is False and "never registered" in reg["detail"], \
        "lane 3 reported a clean board on an app with one blueprint — it is " \
        "not reading current_app.blueprints"
