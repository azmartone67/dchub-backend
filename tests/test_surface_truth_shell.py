"""Surface Truth Master Shell (#30, 2026-07-25) — pins the shell's contract.

The shell exists because the canonical-counts FENCE went green while every
live agent-facing surface still served the retired pre-dedup facility floor:
the fence scans repo-root files that nothing serves. So the properties worth
pinning are not "does it fetch" but:

  1. an unreachable surface is INDETERMINATE, never PASS (green-by-silence is
     the exact failure the shell was built to end);
  2. a served body carrying a retired floor FAILS;
  3. the repo-vs-served lane FAILS precisely on the 07-25 shape — fence file
     clean, served body stale;
  4. it is registered and killable.

CI-SAFETY: the unit-tests job installs ONLY pytest. The shell imports flask
(Blueprint), so the module import is importorskip-guarded; the pure helpers are
tested directly and the wiring is checked as source text.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(ROOT, "routes", "surface_truth_master_shell.py")
MAIN = os.path.join(ROOT, "main.py")
CRON = os.path.join(ROOT, "routes", "cron_heartbeat.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import surface_truth_master_shell as st
    return st


# ── 1 · never green-by-silence ────────────────────────────────────────

def test_unreachable_surface_is_indeterminate_not_pass(shell):
    """An unfetchable surface must render '?', never PASS. The board lied for
    weeks by treating absence of evidence as evidence of health."""
    checks = shell._audit_body("x", "/llms.txt", None, "URLError: timeout",
                               "15,000+")
    assert shell._lane_verdict(checks) == "?"
    assert all(c["pass"] is None for c in checks)
    assert any(c["critical"] for c in checks)


def test_missing_canon_makes_every_lane_indeterminate(shell, monkeypatch):
    """No canon = nothing to compare against. Say so, don't render green."""
    monkeypatch.setattr(shell, "_canon_floor", lambda: None)
    monkeypatch.setattr(shell, "_beat_ledger", lambda note, failing=False: None)
    out = shell._run_tick()
    assert out["lanes"][0]["verdict"] == "?"
    assert out["any_fail"] is False          # '?' is not FAIL — it is unknown


# ── 2 · a stale served body fails ─────────────────────────────────────

def test_served_retired_floor_fails(shell):
    body = "- 21,000+ physical data center facilities across 170+ countries"
    checks = shell._audit_body("x", "/llms.txt", body, None, "15,000+")
    assert shell._lane_verdict(checks) == "FAIL"
    assert any("21,000+" in c["detail"] for c in checks)


def test_served_canon_body_passes(shell):
    body = "- 15,000+ physical data center facilities across 170+ countries"
    checks = shell._audit_body("x", "/llms.txt", body, None, "15,000+")
    assert shell._lane_verdict(checks) == "PASS"


@pytest.mark.parametrize("floor", ["12,650+", "19,500+", "20,000+",
                                   "21,000+", "22,000+", "23,400+"])
def test_many_distinct_over_claims_are_caught_not_just_one(shell, floor):
    """Four different floors were live at once — pinning a single retired
    value would have missed three of them.

    ★ 2026-08-31: this used to be named "..._regex_is_a_range_not_one_value"
    and called _floors_in with no canon. The RANGE is exactly what rotted: it
    banned 19,000-23,999, and once PINNED reached 18,500+ the live healed floor
    19,700+ landed inside BOTH the accept band and the ban, so every surface
    passed "carries canon floor" and failed "free of retired floors" on the
    same bytes. The ban is now derived from canon, and _floors_in takes it.
    The PROPERTY this test protects is unchanged — many distinct over-claims,
    not one pinned literal — and against canon 15,000+ (ceiling 16,500) all six
    values below are still caught."""
    assert shell._floors_in("we track %s facilities" % floor,
                            "15,000+") == [floor]


def test_canon_floor_is_not_itself_flagged_stale(shell):
    assert shell._floors_in("we track 15,000+ facilities", "15,000+") == []
    assert shell._floors_in("we track 15,500+ facilities", "15,000+") == []  # live-healed


def test_a_live_healed_floor_inside_the_band_is_never_flagged(shell):
    """The specific rot. At canon 18,500+ the served floor 19,700+ is
    canon-family; the old range banned it anyway and reddened three lanes."""
    assert shell._floors_in("we track 19,700+ facilities", "18,500+") == []


def test_unknown_canon_is_indeterminate_not_clean(shell):
    """A fence that cannot resolve canon must not certify a page as clean —
    [] would read as PASS, the fail-open direction."""
    assert shell._floors_in("we track 99,000+ facilities", None) is None


# ── 3 · the lane that would have caught 2026-07-25 ────────────────────

def test_repo_clean_but_served_stale_is_a_failure(shell, monkeypatch):
    """THE regression: fence file canon-clean, served body still stale. The
    fence reads green; the agent reads a lie."""
    monkeypatch.setattr(shell, "_read_repo",
                        lambda rel: "15,000+ facilities")
    monkeypatch.setattr(shell, "_fetch",
                        lambda url: ("21,000+ facilities", None))
    checks = shell._lane_repo_vs_served("15,000+")
    assert shell._lane_verdict(checks) == "FAIL"
    assert any("FENCE GREEN, LIVE STALE" in c["detail"] for c in checks)


def test_repo_and_served_agreeing_on_canon_passes(shell, monkeypatch):
    monkeypatch.setattr(shell, "_read_repo", lambda rel: "15,000+ facilities")
    monkeypatch.setattr(shell, "_fetch", lambda url: ("15,000+ facilities", None))
    assert shell._lane_verdict(shell._lane_repo_vs_served("15,000+")) == "PASS"


def test_unreachable_url_does_not_pass_the_parity_lane(shell, monkeypatch):
    monkeypatch.setattr(shell, "_read_repo", lambda rel: "15,000+ facilities")
    monkeypatch.setattr(shell, "_fetch", lambda url: (None, "HTTPError: 503"))
    assert shell._lane_verdict(shell._lane_repo_vs_served("15,000+")) == "?"


# ── 4 · emitter sources + wiring ──────────────────────────────────────

def test_emitter_sources_are_clean_on_this_branch():
    """ai_discovery_routes.py BUILDS /llms.txt inline — it is the real serving
    path, and it carried 21,000+ in ten places until 2026-07-25."""
    src = _read(os.path.join(ROOT, "ai_discovery_routes.py"))
    stale = sorted(set(re.findall(r"\b(?:19|20|21|22|23),\d{3}\+", src)))
    assert not stale, "ai_discovery_routes.py emits retired floor(s): %s" % stale


def test_served_copies_are_clean_on_this_branch():
    """static/ and dchub-frontend/ carry their own copies of every surface —
    all four numbers that were live at once came from these."""
    offenders = {}
    for rel in ("static/llms.txt", "static/llms-full.txt",
                "static/.well-known/mcp.json", "mcp.json",
                "dchub-frontend/llms.txt", "dchub-frontend/llms-full.txt"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        stale = sorted(set(re.findall(r"\b(?:19|20|21|22|23),\d{3}\+",
                                      _read(path))))
        if stale:
            offenders[rel] = stale
    assert not offenders, "retired floors still served: %s" % offenders


def test_shell_is_registered_and_killable():
    main = _read(MAIN)
    assert "surface_truth_master_shell_bp" in main
    assert "/admin/surface-truth" in main
    shell_src = _read(SHELL)
    assert "SURFACE_TRUTH_SHELL_DISABLE" in shell_src
    assert "surface-truth-shell-daily" in shell_src      # dead-man feed
    cron = _read(CRON)
    assert "surface_truth_shell_daily" in cron
    assert "SURFACE_TRUTH_SHELL_DISABLE" in cron


def test_shell_sends_a_user_agent():
    """urllib without a UA gets CF-403'd on this zone — a 403 would render '?'
    forever and the shell would be decorative."""
    assert "User-Agent" in _read(SHELL)


def test_shell_writes_nothing_but_its_own_beat():
    """L8: read-only. The only write is the dead-man beat."""
    src = _read(SHELL)
    for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM", "gh pr merge",
                   "workflow enable"):
        assert banned not in src, "shell must not mutate: found %r" % banned


# ── 5 · accept-either (r-beat-either 2026-07-31) ──────────────────────
# Heal-bound pages carry resolve_canon()'s LIVE floor (15,300+/15,500+), not
# PINNED (15,000+) — the exact-PINNED check redded them by construction, which
# is why /ai was deferred from _TEXT_SURFACES on 07-30. The beat now accepts
# the PINNED phrase or any comma-"N+" floor within [PINNED, PINNED x 1.10].

def test_served_live_healed_floor_passes(shell):
    """The deferral case: a healthy heal-bound body carrying the live floor."""
    body = "- 15,500+ physical data center facilities across 170+ countries"
    checks = shell._audit_body("x", "/ai", body, None, "15,000+")
    assert shell._lane_verdict(checks) == "PASS"
    assert any("15,500+" in c["detail"] for c in checks)


def test_floor_above_band_fails(shell):
    """A raw/legacy-basis over-claim outside the band must NOT be accepted —
    18,000+ is neither PINNED nor a plausible live-healed floor."""
    body = "we index 18,000+ facilities worldwide"
    checks = shell._audit_body("x", "/ai", body, None, "15,000+")
    assert shell._lane_verdict(checks) == "FAIL"
    canon_check = next(c for c in checks if c["id"].endswith("_canon"))
    assert canon_check["pass"] is False


def test_floor_below_pinned_is_not_accepted(shell):
    """13,000+ (the US-plants floor) sits below PINNED and must not satisfy
    the facilities check — the band's lower bound is PINNED itself."""
    body = "we map 13,000+ US power plants"
    checks = shell._audit_body("x", "/ai", body, None, "15,000+")
    canon_check = next(c for c in checks if c["id"].endswith("_canon"))
    assert canon_check["pass"] is False


def test_parity_lane_accepts_pinned_repo_vs_healed_served(shell, monkeypatch):
    """Repo copy holds PINNED, served body holds the live-healed floor — both
    canon-family, the lane must agree-PASS, not flag phrasing drift."""
    monkeypatch.setattr(shell, "_read_repo", lambda rel: "15,000+ facilities")
    monkeypatch.setattr(shell, "_fetch",
                        lambda url: ("15,500+ facilities", None))
    assert shell._lane_verdict(shell._lane_repo_vs_served("15,000+")) == "PASS"


def test_ai_is_a_text_surface(shell):
    """/ai is heal-bound and now held by the daily beat (deferred 07-30 until
    the accept-either fix; a drop here silently unguards the page again)."""
    assert "/ai" in shell._TEXT_SURFACES
