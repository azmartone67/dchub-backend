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
import re

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
)

# The swallow, in both styles it was written in.
#
# ★The negative lookahead is load-bearing. routes/radar.py returns
# `{}, f"HTTP {r.status_code}"` — the CORRECT shape, learned the hard way when
# our own paywall 402'd a loopback call and every grid-intel field on /radar
# pinned to baseline while retrieved_at kept moving. Without `(?!\s*,)` this
# guard flags that fix as the bug it prevents, and the obvious "fix" is to
# delete the reason string.
_SWALLOW = re.compile(
    r"if r\.status_code != 200:\s*\n?\s*return \{\}(?!\s*,)", re.M)


def _src(rel: str) -> str:
    return (_ROOT / "routes" / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("mod", _MIGRATED)
def test_module_uses_the_envelope(mod):
    src = _src(mod)
    assert "util.internal_fetch" in src, \
        "%s no longer routes through the envelope" % mod


@pytest.mark.parametrize("mod", _MIGRATED)
def test_module_has_no_bare_swallow(mod):
    src = _src(mod)
    assert not _SWALLOW.search(src), \
        "%s re-grew a bare-{} status swallow" % mod


def test_no_new_swallowers_anywhere_in_routes():
    """★The regression that matters most: a NEW module copied from an old one.
    Nine copies of this bug existed because copying was easier than importing."""
    offenders = []
    for p in sorted((_ROOT / "routes").glob("*.py")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"def _internal\w*\(path", src) and _SWALLOW.search(src):
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
