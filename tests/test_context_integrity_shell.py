"""tests/test_context_integrity_shell.py — Context-Integrity shell (#63, 2026-08-11).

Guards util/internal_fetch.py and routes/context_integrity_master_shell.py. Both
exist because a bare `{}` made "the instrument failed" and "the answer is
nothing" indistinguishable, and 17 of the brain's 20 live L18 lessons were that
ambiguity being re-learned. So every test here is a way THAT SAME COLLAPSE could
come back:

  (1) ORPHAN — blueprint written, never registered; /admin/context-integrity
      404s forever while the module looks wired.
  (2) COLLAPSE-BACK — someone "simplifies" probe() so an empty 200 reports
      ok=False, or a failure reports empty=True. Either direction rebuilds the
      bug from one side, and the shell would then read green.
  (3) CONFIDENT-GREEN — a lane whose critical check could not be evaluated
      renders PASS instead of '?'. The shell that exists to expose blind probes
      must never go blind silently.
  (4) 5xx KILL — the kill switch returns 503, which the CF worker reads as a
      dead origin and fails over site-wide to the stale Render backend.
  (5) CLASSIFIER-DRIFT — lane 2's instrument-blindness regex widens until real
      domain lessons are swept in, and the meter reads 85% forever.

House rules: pre-merge pytest has NO DB and must NEVER import main — the wiring
guard reads main.py as TEXT. Nothing runs at module scope.

Run:  python3 -m pytest tests/test_context_integrity_shell.py -v
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SHELL = _ROOT / "routes" / "context_integrity_master_shell.py"
_FETCH = _ROOT / "util" / "internal_fetch.py"


def _shell_src() -> str:
    return _SHELL.read_text(encoding="utf-8")


# ── (1) orphan ────────────────────────────────────────────────────────

def test_blueprint_registered_in_main():
    """A shell nobody registers is dead code carrying a dashboard."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8", errors="ignore")
    assert "context_integrity_master_shell" in src, \
        "shell module never imported in main.py"
    assert "register_blueprint(context_integrity_master_shell_bp)" in src, \
        "blueprint imported but never registered"


def test_shell_and_fetch_parse():
    ast.parse(_shell_src())
    ast.parse(_FETCH.read_text(encoding="utf-8"))


# ── (2) collapse-back ─────────────────────────────────────────────────

def _fetch_mod():
    import importlib
    return importlib.import_module("util.internal_fetch")


def test_empty_200_is_a_measurement_not_a_failure():
    """★The whole point. An endpoint answering `{}` with HTTP 200 WORKED and
    reported nothing. Reporting that as ok=False rebuilds the bug."""
    m = _fetch_mod()
    env = m._envelope("/x", True, {}, None, 200)
    assert env["ok"] is True
    assert env["empty"] is True


def test_failure_is_never_reported_as_empty():
    """A failure has no measurement at all, so `empty` must stay False —
    otherwise a dead probe joins the 'genuinely zero' list."""
    m = _fetch_mod()
    for reason, status in (("HTTP 500", 500), ("Timeout: read", None),
                           ("non-JSON body", 200)):
        env = m._envelope("/x", False, {}, reason, status)
        assert env["ok"] is False
        assert env["empty"] is False, "failed probe reported as measured-empty"
        assert env["reason"], "a failure must carry a reason"


def test_health_of_separates_failed_from_empty():
    m = _fetch_mod()
    h = m.health_of({
        "dead":  m._envelope("/a", False, {}, "HTTP 502", 502),
        "zero":  m._envelope("/b", True, {}, None, 200),
        "full":  m._envelope("/c", True, {"n": 1}, None, 200),
    })
    assert [f["probe"] for f in h["instrument_failed"]] == ["dead"]
    assert h["measured_empty"] == ["zero"]
    assert h["ok"] == ["full"]
    assert h["any_instrument_failed"] is True


def test_data_of_survives_a_raw_dict():
    """Back-compat: call sites still passing a plain payload must not crash."""
    m = _fetch_mod()
    assert m.data_of({"a": 1}) == {"a": 1}
    assert m.data_of(None) == {}
    assert m.data_of(m._envelope("/x", True, {"a": 1}, None, 200)) == {"a": 1}


def test_l14_no_longer_swallows_into_a_bare_dict():
    """The regression this shell exists to prevent: L14 re-growing its own
    bare-{} fetcher instead of delegating to the envelope."""
    src = (_ROOT / "routes" / "brain_layer14_causal.py").read_text(
        encoding="utf-8")
    assert "util.internal_fetch" in src, "L14 no longer uses the envelope"
    assert "context_health" in src, \
        "L14 stopped reporting which probes could not be measured"
    assert "if r.status_code != 200: return {}" not in src, \
        "the bare-{} swallow is back in L14"


# ── (3) confident-green ───────────────────────────────────────────────

def _shell_mod():
    import importlib
    return importlib.import_module("routes.context_integrity_master_shell")


def test_indeterminate_critical_check_is_not_pass():
    s = _shell_mod()
    checks = [s._check("a", "measurable", None, "could not measure",
                       critical=True)]
    assert s._lane_verdict(checks) == "?", \
        "a critical check that could not be evaluated rendered green"


def test_any_false_check_fails_the_lane():
    s = _shell_mod()
    checks = [s._check("a", "ok", True, ""),
              s._check("b", "bad", False, "", critical=False)]
    assert s._lane_verdict(checks) == "FAIL"


def test_all_true_passes():
    s = _shell_mod()
    assert s._lane_verdict([s._check("a", "ok", True, "", critical=True)]) == "PASS"


def test_lane_crash_is_indeterminate_not_silent():
    s = _shell_mod()

    def _boom():
        raise RuntimeError("nope")

    checks = s._safe_lane(_boom)
    assert s._lane_verdict(checks) == "?"
    assert any(c["critical"] for c in checks)


# ── (4) 5xx kill ──────────────────────────────────────────────────────

def test_kill_switch_never_returns_5xx():
    """A 5xx from Railway trips the CF worker's failover to the stale Render
    origin; two within 10s break the site for 30s. Walks the AST so a guard
    whose block has no blank line inside it cannot be skipped."""
    tree = ast.parse(_shell_src())
    guards = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        guards += 1
        codes = [n.value for n in ast.walk(node)
                 if isinstance(n, ast.Constant) and isinstance(n.value, int)]
        assert codes, "kill guard returns no explicit status code"
        for c in codes:
            assert c < 500, "kill switch returns %d — CF reads 5xx as a dead origin" % c
    assert guards >= 2, "expected a kill guard on both the JSON and HTML routes"


# ── (5) classifier drift ──────────────────────────────────────────────

# Verbatim from /api/v1/brain/lessons on 2026-08-11 — the 17 that motivated
# the shell, and the ones that must NOT be swept in with them.
_BLIND = [
    "Predictions requiring non-empty endpoint payloads (per-agent breakdowns, "
    "attribution metrics, conversion funnels, weekly-series, detector findings) "
    "null when endpoints return empty",
    "Predictions requiring granular breakdowns null when endpoints return "
    "empty—per-datacolo, per-IP, or per-agent distribution data required, not "
    "aggregates",
    "Multi-metric bridge predictions null when any required endpoint returns "
    "empty—paid attribution requires both signal-side and conversion-side "
    "metrics, not endpoint availability alone",
    "Detector-blindness predictions null when findings[] query unavailable or "
    "returns empty—requires re-run proof via non-empty findings array",
]
_DOMAIN = [
    "Spike-decay predictions falsify when post-spike week collapses to "
    "near-zero rather than settling at rolling baseline—extinction differs "
    "from mean-reversion",
    "Shadow Stripe webhooks always break MCP attribution",
    "CF Pages routing requires both _routes.json AND PHASE_282 set updates — "
    "neither alone is sufficient",
]


@pytest.mark.parametrize("lesson", _BLIND)
def test_instrument_blindness_is_detected(lesson):
    assert _shell_mod()._is_instrument_blindness(lesson) is True


@pytest.mark.parametrize("lesson", _DOMAIN)
def test_real_domain_lessons_are_not_swept_in(lesson):
    """★If this loosens, the meter reads 85% forever and lane 2 stops meaning
    anything — a domain lesson counted as blindness is the metric eating
    itself."""
    assert _shell_mod()._is_instrument_blindness(lesson) is False


def test_blindness_threshold_is_below_birth_share():
    """17/20 = 85% at birth. A threshold at or above that would render PASS on
    the exact condition the shell was built to catch."""
    s = _shell_mod()
    assert s._BLINDNESS_FAIL_SHARE < 0.85


# ── lane 4 is coverage, not a rebuild ─────────────────────────────────

def test_lane4_reads_the_shipped_edge_set():
    """Shell #49 already ships LOOP_EDGES + input_status. Lane 4 must MEASURE
    that, not re-declare its own edges — re-implementing shipped capability is
    the exact failure brain_capability_ledger exists to stop."""
    src = _shell_src()
    assert "from routes.graph_master_shell import LOOP_EDGES" in src
    assert "LOOP_EDGES = (" not in src, "lane 4 re-declared the edge set"
    assert "input_status" in src


def test_shell_is_read_only():
    """Every lane names its actuator and fires nothing. A shell that can delete
    its own siblings is a worse problem than the one it solves."""
    src = _shell_src()
    for forbidden in ("os.remove", "os.unlink", "shutil.rmtree", "DELETE FROM",
                      "DROP TABLE"):
        assert forbidden not in src, "shell performs a write: %s" % forbidden
