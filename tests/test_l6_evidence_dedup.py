"""
tests/test_l6_evidence_dedup.py — L6 evidence-subject dedup (2026-08-31).

NO DB, NO network, NO main import.

THE FAILURE THIS PINS
─────────────────────
One sentinel verdict on /mcp#workos-oauth-challenge produced three separate
MERGED scaffold PRs over six weeks (2026-07-13, 2026-08-17, 2026-08-24)
under three titles that never collided on tokens. be#3448 then proved the
verdict never held — dchub-mcp-server drops the OAuth challenge on
`initialize` BY DESIGN, so anon initialize=200 is correct — and be#3458 /
be#3459 deleted all three scaffolds by hand.

Both pre-existing dedup passes were blind to it: open_pr_exists (exact
title, 2026-06-28) and open_similar_pr_exists (fuzzy title, 2026-07-02)
compare TITLES and query `?state=open`, so a MERGED scaffold is invisible
and a paraphrased title never matches. The evidence keys, however, named
the same page in all three.

The evidence-key sets below are VERBATIM from the three deleted files.

MUTATION CONTROLS — each of these must FAIL if the guard is weakened:
  A. drop "pages"/"page_health" from _EVIDENCE_GENERIC  -> the three sets
     still collide (on the generic segment) but test_unrelated_recs_do_not
     _collide starts flagging unrelated pairs.
  B. remove the _EVID_BRACKET_RE normalisation           -> A-vs-C stops
     colliding (bracket vs dot spelling).
  C. remove the _EVID_ASSERT_RE strip                    -> B stops
     colliding with A and C (`...verdict=broken`).
  D. make _scaffolded_evidence_subjects return {} on DB error instead of
     None                                                -> test_ledger_
     read_error_returns_none_not_empty fails. NB the gate tests stub the
     ledger out, so this mutant SURVIVED until that direct test existed —
     a guard whose mutant survives is not evidence.
  E. move the gate below the branch/commit calls         -> test_duplicate
     _evidence_opens_nothing fails (a branch gets created).
  F. make the gate skip when subjects is empty removed   -> test_empty_
     evidence_does_not_suppress fails.
"""
import json
import sys
import types

import pytest

bsp = pytest.importorskip("routes.brain_strategic_planner")


# The three real evidence-key lists, verbatim from the deleted scaffolds.
KEYS_KLAVIS_0713 = [
    "competitor_signal.presence.competitor_features[klavis_ai]",
    "page_health.pages[/mcp#workos-oauth-challenge]",
    "funnel.ai_agent_top_platforms_external",
]
KEYS_REENABLE_0817 = [
    "page_health.pages./mcp#workos-oauth-challenge.verdict=broken",
    "funnel.paid_signal_attribution_30d.attribution_rate_pct=33.3",
]
KEYS_DURABLE_0824 = [
    "page_health.pages[/mcp#workos-oauth-challenge].last_reason",
    "funnel.now.paid_signal_attribution_30d.attribution_rate_pct",
]

PAGE = "/mcp#workos-oauth-challenge"


# ════════════════════════════════════════════════════════════════════
#  evidence_subjects — the identity the dedup is keyed on
# ════════════════════════════════════════════════════════════════════
def test_the_three_real_scaffolds_share_one_subject():
    """The headline: all three sets resolve to the same page subject."""
    a = bsp.evidence_subjects(KEYS_KLAVIS_0713)
    b = bsp.evidence_subjects(KEYS_REENABLE_0817)
    c = bsp.evidence_subjects(KEYS_DURABLE_0824)
    assert PAGE in a and PAGE in b and PAGE in c
    # Pairwise, because the gate compares one candidate against the ledger.
    assert a & b, "07-13 vs 08-17 must collide"
    assert a & c, "07-13 vs 08-24 must collide"
    assert b & c, "08-17 vs 08-24 must collide"
    assert PAGE in (a & b) and PAGE in (a & c) and PAGE in (b & c)


def test_bracket_and_dot_spellings_normalise_together():
    """`pages[/x]` and `pages./x` are the same path (mutation control B)."""
    assert bsp.evidence_subjects(["page_health.pages[/mcp#foo]"]) == \
        bsp.evidence_subjects(["page_health.pages./mcp#foo"])


def test_assertion_suffix_is_stripped():
    """`...verdict=broken` is the same subject as the bare path (control C)."""
    assert bsp.evidence_subjects(["page_health.pages./x.verdict=broken"]) == \
        bsp.evidence_subjects(["page_health.pages./x"])
    assert bsp.evidence_subjects(["funnel.rate_pct=33.3"]) == \
        bsp.evidence_subjects(["funnel.rate_pct"])


def test_generic_segments_are_dropped():
    """Container/accessor words never become a subject — otherwise every
    page_health rec would collide with every other one (control A)."""
    subj = bsp.evidence_subjects(KEYS_DURABLE_0824)
    for generic in ("page_health", "pages", "funnel", "now", "last_reason"):
        assert generic not in subj, generic


def test_unrelated_recs_do_not_collide():
    """The gate must not suppress genuinely different work (control A)."""
    x = bsp.evidence_subjects(
        ["funnel.now.paywall_hits_30d", "feedback.open[dcpi_export_csv]"])
    y = bsp.evidence_subjects(
        ["backlog.stuck[fiber_route_ingest]", "self_model.weakest_areas"])
    assert not (x & y)
    assert not (x & bsp.evidence_subjects(KEYS_KLAVIS_0713))


@pytest.mark.parametrize("bad", [None, [], [None], [123], [""], ["."],
                                 ["page_health"], [{"k": "v"}]])
def test_garbage_in_yields_no_subject_never_raises(bad):
    """A rec whose keys don't resolve gets an EMPTY set, so it is never
    suppressed on a subject the planner never actually cited."""
    assert bsp.evidence_subjects(bad) == frozenset()


# ════════════════════════════════════════════════════════════════════
#  _open_scaffold_pr — the gate itself
# ════════════════════════════════════════════════════════════════════
class _Recorder:
    """Stands in for routes.brain_pr_opener and records what got called."""

    def __init__(self):
        self.calls = []

    def _fake_module(self):
        m = types.ModuleType("routes.brain_pr_opener")
        m._GITHUB_TOKEN = "t0ken"
        m._GITHUB_REPO = "acme/repo"
        m.open_pr_exists = lambda *a, **k: False
        m.open_similar_pr_exists = lambda *a, **k: False

        def _sha():
            self.calls.append("get_default_branch_sha")
            return "deadbeef"

        def _branch(name, sha):
            self.calls.append(("create_branch", name))
            return True

        def _commit(path, content, msg, branch, sha):
            self.calls.append(("commit_file", path))
            return True

        def _gh(method, path, body=None):
            self.calls.append(("gh", method, path))
            return types.SimpleNamespace(
                status_code=201, text="",
                json=lambda: {"html_url": "https://example.test/pr/1",
                              "number": 1})

        m._get_default_branch_sha = _sha
        m._create_branch = _branch
        m._commit_file = _commit
        m._gh = _gh
        return m


@pytest.fixture
def opener(monkeypatch):
    rec = _Recorder()
    monkeypatch.setitem(sys.modules, "routes.brain_pr_opener",
                        rec._fake_module())
    monkeypatch.delenv("BRAIN_STRATEGIC_EVIDENCE_DEDUP", raising=False)
    return rec


def _rec(keys, title="Re-enable WorkOS durable identity challenge"):
    return {"title": title, "evidence_keys": keys, "week_of": "2026-08-24",
            "kind": "strategic_gap_4w", "spec_md": "spec", "confidence": 0.85}


def test_duplicate_evidence_opens_nothing(opener, monkeypatch):
    """A subject already scaffolded => skip BEFORE any branch is created
    (mutation control E: no orphan branch, no commit, no PR)."""
    monkeypatch.setattr(
        bsp, "_scaffolded_evidence_subjects",
        lambda *a, **k: {PAGE: ("OAuth onboarding parity with Klavis",
                                 "2026-07-13")})
    out = bsp._open_scaffold_pr(_rec(KEYS_DURABLE_0824))
    assert out["ok"] is True
    assert out["skipped"] == "duplicate_evidence"
    assert out["evidence_subject"] == PAGE
    assert out["prior_week"] == "2026-07-13"
    assert opener.calls == [], f"nothing may be created: {opener.calls}"


def test_ledger_unreadable_withholds_the_pr(opener, monkeypatch):
    """Unknown is not empty — fail CLOSED (mutation control D)."""
    monkeypatch.setattr(bsp, "_scaffolded_evidence_subjects",
                        lambda *a, **k: None)
    out = bsp._open_scaffold_pr(_rec(KEYS_DURABLE_0824))
    assert out["skipped"] == "evidence_ledger_unreadable"
    assert opener.calls == []


def test_fresh_evidence_proceeds_past_the_gate(opener, monkeypatch):
    """A genuinely new subject is NOT suppressed — the gate must not be a
    blanket stop on every scaffold."""
    monkeypatch.setattr(bsp, "_scaffolded_evidence_subjects",
                        lambda *a, **k: {"some_other_subject": ("t", "w")})
    bsp._open_scaffold_pr(_rec(KEYS_DURABLE_0824))
    assert "get_default_branch_sha" in opener.calls


def test_empty_evidence_does_not_suppress(opener, monkeypatch):
    """A rec citing nothing has no subject to collide on (control F). It is
    let through rather than blocked on a subject it never claimed."""
    called = {"n": 0}

    def _ledger(*a, **k):
        called["n"] += 1
        return {PAGE: ("prior", "2026-07-13")}

    monkeypatch.setattr(bsp, "_scaffolded_evidence_subjects", _ledger)
    bsp._open_scaffold_pr(_rec([]))
    assert called["n"] == 0, "ledger must not even be read with no subjects"
    assert "get_default_branch_sha" in opener.calls


def test_kill_switch_bypasses_the_gate(opener, monkeypatch):
    monkeypatch.setenv("BRAIN_STRATEGIC_EVIDENCE_DEDUP", "0")

    def _boom(*a, **k):
        raise AssertionError("ledger must not be consulted when disabled")

    monkeypatch.setattr(bsp, "_scaffolded_evidence_subjects", _boom)
    bsp._open_scaffold_pr(_rec(KEYS_DURABLE_0824))
    assert "get_default_branch_sha" in opener.calls


def test_dedup_is_on_by_default():
    """Unlike the other flags in this module it defaults ENABLED, because a
    wrong default only ever withholds a draft."""
    import os
    os.environ.pop("BRAIN_STRATEGIC_EVIDENCE_DEDUP", None)
    assert bsp._evidence_dedup_enabled() is True


def test_window_outlives_the_six_week_span():
    """The three scaffolds spanned 2026-07-13 -> 2026-08-24. A window that
    can't see six weeks back could not have caught the third from the
    first, which is precisely why the 4-week title window missed them."""
    assert bsp._evidence_dedup_weeks() >= 6


# ════════════════════════════════════════════════════════════════════
#  _scaffolded_evidence_subjects — the ledger read itself
#  (the gate tests above stub this out, so its own failure modes need
#   direct cover: mutation control D lived here undetected until they
#   were added.)
# ════════════════════════════════════════════════════════════════════
class _FakeCursor:
    def __init__(self, rows=None, raises=False):
        self._rows, self._raises = rows or [], raises

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        if self._raises:
            raise RuntimeError("connection reset")

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None, raises=False):
        self._rows, self._raises = rows, raises
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows, self._raises)

    def close(self):
        self.closed = True


def test_ledger_read_error_returns_none_not_empty(monkeypatch):
    """Mutation control D. Returning {} here would read as 'nothing has
    been scaffolded' and let every duplicate through — the exact
    unknown-is-not-empty trap _read_recs_for hit on 2026-07-02."""
    conn = _FakeConn(raises=True)
    monkeypatch.setattr(bsp, "_get_db", lambda: conn)
    assert bsp._scaffolded_evidence_subjects() is None
    assert conn.closed, "connection must be closed even on the error path"


def test_ledger_without_db_returns_none(monkeypatch):
    monkeypatch.setattr(bsp, "_get_db", lambda: None)
    assert bsp._scaffolded_evidence_subjects() is None


@pytest.mark.parametrize("stored", [
    KEYS_KLAVIS_0713,                 # driver handed back a list
    json.dumps(KEYS_KLAVIS_0713),     # driver handed back the raw string
])
def test_ledger_maps_subjects_from_either_column_form(monkeypatch, stored):
    conn = _FakeConn(rows=[("OAuth onboarding parity with Klavis",
                            "2026-07-13", stored)])
    monkeypatch.setattr(bsp, "_get_db", lambda: conn)
    got = bsp._scaffolded_evidence_subjects()
    assert got is not None
    assert PAGE in got
    assert got[PAGE] == ("OAuth onboarding parity with Klavis", "2026-07-13")


def test_ledger_keeps_the_newest_prior_for_a_subject(monkeypatch):
    """Rows arrive newest-first; the reported prior should be the most
    recent scaffold on that subject, not the oldest."""
    conn = _FakeConn(rows=[
        ("Durable agent identity re-enablement", "2026-08-24",
         json.dumps(KEYS_DURABLE_0824)),
        ("OAuth onboarding parity with Klavis", "2026-07-13",
         json.dumps(KEYS_KLAVIS_0713)),
    ])
    monkeypatch.setattr(bsp, "_get_db", lambda: conn)
    got = bsp._scaffolded_evidence_subjects()
    assert got[PAGE][1] == "2026-08-24"


def test_ledger_skips_unparseable_rows_without_dropping_the_rest(monkeypatch):
    conn = _FakeConn(rows=[
        ("broken row", "2026-08-01", "{not json"),
        ("good row", "2026-07-13", json.dumps(KEYS_KLAVIS_0713)),
    ])
    monkeypatch.setattr(bsp, "_get_db", lambda: conn)
    got = bsp._scaffolded_evidence_subjects()
    assert PAGE in got and got[PAGE][0] == "good row"
