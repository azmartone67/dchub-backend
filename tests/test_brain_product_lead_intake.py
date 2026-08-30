"""Tests for routes/brain_product_lead_intake.py — the judgement lane.

This lane carries no measurements, so its whole safety story is that judgement
must pass through the claim contract before it can become brain work:
  0a. only VERIFIER-STAMPED outcomes (an open claim is an unverified opinion)
  0b. only REFUTED (confirmed = good news; unobserved = instrument gap;
      retracted = withdrawn; superseded = a regime that is gone)
  0c. board-level: verifier liveness + a SAMPLE-GATED blast radius that must
      report whether it actually ran
  1.  the producer path cannot bypass the contract or impersonate a producer
  2.  capped + rotated
  3.  never a live board read on the hot path
"""

from datetime import datetime, timedelta, timezone

from routes import brain_product_lead_intake as pl


def _c(cid, outcome="refuted", subject="product:nav.search", superseded=None,
       age_h=2.0, statement="search is discoverable"):
    at = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"id": cid, "kind": "fact", "subject": subject,
            "statement": statement, "regime": {"basis": "b"},
            "expected_metric": "get:/api/v1/x y", "expected_value": ">= 1",
            "outcome": outcome, "outcome_evidence": "measured 0",
            "outcome_at": at.isoformat(), "superseded_by": superseded}


def _board(claims=None, newest_age_h=2.0):
    at = datetime.now(timezone.utc) - timedelta(hours=newest_age_h)
    return {"claims": claims if claims is not None else [_c(1)],
            "newest_outcome_at": at.isoformat()}


# ── 0b: only a refutation is work ───────────────────────────────────────

def test_only_refuted_claims_are_seedable():
    # Kills: seeding confirmations (good news is not work), unobserved (the
    # instrument never measured) or retracted (the owner withdrew it).
    rows = [_c(1, outcome="refuted"), _c(2, outcome="confirmed"),
            _c(3, outcome="unobserved"), _c(4, outcome="retracted"),
            _c(5, outcome=None)]
    got = {c["id"] for c in pl.select_seedable(rows, limit=9, cycle=0)[0]}
    assert got == {1}


def test_an_unstamped_claim_is_never_seedable():
    # An open claim is an unverified opinion — exactly what this lane exists
    # to keep out of the backlog.
    assert pl.select_seedable([_c(1, outcome=None)], limit=9, cycle=0) == ([], 0)


def test_a_superseded_refutation_is_not_seedable():
    # It describes a regime that no longer exists.
    rows = [_c(1, superseded=99)]
    assert pl.select_seedable(rows, limit=9, cycle=0) == ([], 0)


def test_claims_outside_the_product_namespace_are_ignored():
    # Kills: this lane hoovering up the canon/squasher producers' claims.
    rows = [_c(1, subject="canon:facilities"), _c(2, subject="finding:/x"),
            _c(3, subject="product:nav.search")]
    got = {c["id"] for c in pl.select_seedable(rows, limit=9, cycle=0)[0]}
    assert got == {3}


# ── 0c: verifier liveness + sample-gated blast radius ───────────────────

def test_a_stale_verifier_refuses_the_board():
    b = _board([_c(1)], newest_age_h=1000.0)
    why = pl.run_refusal(b, max_age_h=168.0)
    assert why and "verifier has not judged anything recently" in why


def test_unreadable_verdict_time_is_refused_not_waved_through():
    b = _board([_c(1)]); b["newest_outcome_at"] = "not-a-date"
    assert pl.run_refusal(b) is not None
    b["newest_outcome_at"] = None
    assert pl.run_refusal(b) is not None


def test_blast_radius_refuses_an_implausible_refutation_rate():
    claims = [_c(i) for i in range(9)] + [_c(99, outcome="confirmed")]
    why = pl.run_refusal(_board(claims), max_ratio=0.75, min_sample=5)
    assert why and "came back refuted" in why


def test_blast_radius_says_it_is_not_a_canary():
    claims = [_c(i) for i in range(9)] + [_c(99, outcome="confirmed")]
    why = pl.run_refusal(_board(claims), max_ratio=0.75, min_sample=5)
    assert "must-fail control" in why


def test_blast_radius_is_not_applied_below_the_sample_floor():
    # Two refutations out of two is 100%, but two is not a rate. The gate must
    # not fire — and must not pretend it ran.
    b = _board([_c(1), _c(2)])
    assert pl.run_refusal(b, max_ratio=0.75, min_sample=5) is None
    assert pl.blast_radius_applied(b, min_sample=5) is False


def test_blast_radius_reports_that_it_ran_when_it_did():
    b = _board([_c(i) for i in range(6)])
    assert pl.blast_radius_applied(b, min_sample=5) is True


def test_a_healthy_mixed_board_is_accepted():
    claims = [_c(1)] + [_c(i, outcome="confirmed") for i in range(2, 8)]
    assert pl.run_refusal(_board(claims), max_ratio=0.75, min_sample=5) is None


def test_an_empty_ledger_is_quiet_not_refused():
    # Kills: reporting a problem where there is none. No product claims yet is
    # a correct state, and calling it "refused" would cry wolf forever.
    assert pl.run_refusal(_board([], newest_age_h=1.0)) is None


def test_refresh_persists_the_refusal(monkeypatch):
    saved = {}
    monkeypatch.setattr(pl, "state_get", lambda k: None)
    monkeypatch.setattr(pl, "state_set",
                        lambda k, v: saved.update({"v": v}) or True)
    b = _board([_c(i) for i in range(9)])
    out = pl.refresh_snapshot(force=True, load_fn=lambda: b)
    assert out["ok"] and out["rows"] == 0 and out["refused"]
    assert saved["v"]["rows"] == [] and saved["v"]["refused"]


# ── 1: the producer path cannot bypass the contract ─────────────────────

def test_a_claim_with_no_expectation_is_refused():
    # Kills: re-implementing (and weakening) the ledger contract here. The
    # wrapper must let register_claim refuse an unfalsifiable claim.
    r = pl.file_claim({"kind": "fact", "subject": "nav.search",
                       "statement": "the nav is confusing"})
    assert r.get("refused") and "expected_metric" in r["error"]


def test_a_claim_with_a_bad_comparator_is_refused():
    r = pl.file_claim({"kind": "fact", "subject": "nav.search",
                       "statement": "s", "expected_metric": "get:/api/v1/x y",
                       "expected_value": "probably fine"})
    assert r.get("refused") and "comparator" in r["error"]


def test_a_claim_with_an_unknown_instrument_is_refused():
    r = pl.file_claim({"kind": "fact", "subject": "nav.search",
                       "statement": "s", "expected_metric": "vibes:nav",
                       "expected_value": ">= 1"})
    assert r.get("refused") and "expected_metric" in r["error"]


def test_the_product_lead_cannot_impersonate_the_canon_or_fix_producers():
    # Kills: a product-lead session filing a `canon` claim, which belongs to
    # claim_ledger's own producer, or a `fix` claim, which belongs to the
    # squasher — both would launder judgement into another lane's evidence.
    for kind in ("canon", "fix", "listing", "post"):
        r = pl.file_claim({"kind": kind, "subject": "x", "statement": "s",
                           "expected_metric": "get:/api/v1/x y",
                           "expected_value": ">= 1"})
        assert r.get("refused"), kind
        assert "kind must be one of" in r["error"]


def test_subject_is_forced_into_the_product_namespace():
    assert pl.normalize_subject("nav.search") == "product:nav.search"


def test_namespacing_is_idempotent():
    # Kills: product:product:nav.search, which would never match the intake.
    assert pl.normalize_subject("product:nav.search") == "product:nav.search"


def test_an_empty_subject_is_refused():
    r = pl.file_claim({"kind": "fact", "subject": "  ", "statement": "s",
                       "expected_metric": "get:/api/v1/x y",
                       "expected_value": ">= 1"})
    assert r.get("refused") and "subject required" in r["error"]


def test_a_non_object_regime_is_refused():
    r = pl.file_claim({"kind": "fact", "subject": "x", "statement": "s",
                       "expected_metric": "get:/api/v1/x y",
                       "expected_value": ">= 1", "regime": "oops"})
    assert r.get("refused") and "regime" in r["error"]


# ── 2: order, cap, rotation ─────────────────────────────────────────────

def test_freshest_refutation_first():
    rows = [_c(1, age_h=100.0), _c(2, age_h=1.0), _c(3, age_h=50.0)]
    got = [c["id"] for c in pl.select_seedable(rows, limit=3, cycle=0)[0]]
    assert got == [2, 3, 1]


def test_cap_limits_rows_and_reports_the_true_total():
    rows = [_c(i, age_h=float(i)) for i in range(1, 8)]
    got, total = pl.select_seedable(rows, limit=2, cycle=0)
    assert len(got) == 2 and total == 7


def test_rotation_gives_every_refutation_budget():
    rows = [_c(i, age_h=float(i)) for i in range(1, 8)]
    seen = set()
    for cyc in range(4):
        seen |= {c["id"] for c in pl.select_seedable(rows, limit=2, cycle=cyc)[0]}
    assert seen == set(range(1, 8))


def test_env_cap_is_honoured(monkeypatch):
    monkeypatch.setenv("PLEAD_INTAKE_MAX", "1")
    rows = [_c(i, age_h=float(i)) for i in range(1, 5)]
    assert len(pl.select_seedable(rows, cycle=0)[0]) == 1


# ── 3: hot path + shape ─────────────────────────────────────────────────

def test_findings_read_snapshot_only(monkeypatch):
    def _boom():
        raise AssertionError("live board read on the hot path")
    monkeypatch.setattr(pl, "_load_board", _boom)
    monkeypatch.setattr(pl, "state_get",
                        lambda k: {"rows": [_c(1)], "board_as_of": "x"})
    assert len(pl.product_lead_findings()) == 1


def test_kill_switch_silences_the_lane(monkeypatch):
    monkeypatch.setenv("PLEAD_INTAKE_DISABLE", "1")
    monkeypatch.setattr(pl, "state_get", lambda k: {"rows": [_c(1)]})
    assert pl.product_lead_findings() == []


def test_findings_are_fail_soft_on_state_error(monkeypatch):
    def _boom(_k):
        raise RuntimeError("brain_state unreachable")
    monkeypatch.setattr(pl, "state_get", _boom)
    assert pl.product_lead_findings() == []


def test_issue_label_uses_the_plead_prefix():
    assert pl.to_findings([_c(1)])[0]["issue"].startswith("plead_")


def test_identity_is_the_immutable_claim_id():
    a = pl.to_findings([_c(7)])[0]
    b = pl.to_findings([_c(7, statement="reworded")])[0]
    assert a["url"] == b["url"] == "dchub://product-lead/claim/7"


def test_detail_names_the_instrument_and_the_expectation():
    # The whole point of this lane: the reader must be able to see WHAT would
    # have turned it red, without opening the ledger.
    d = pl.to_findings([_c(1)])[0]["detail"]
    assert "get:/api/v1/x y" in d and ">= 1" in d
    assert "written by the verifier" in d


def test_rows_without_an_id_are_dropped():
    assert pl.to_findings([{"outcome": "refuted"}]) == []


def test_non_dict_regime_does_not_explode():
    assert len(pl.to_findings([dict(_c(1), regime="oops")])) == 1
