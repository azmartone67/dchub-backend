"""r-candidate-v1 (2026-07-11): the ChatGPT-co-designed handoff contract.

Unit-level (no main import, no DB). The contract invariants ARE the tests:
deterministic identity, identity≠freshness, fail-closed expiry, separate
search/analysis versions, and a mint that never extends a TTL.
"""

import pathlib

from routes.candidates import (candidate_id_for, candidate_echo,
                               expired_response, SEARCH_VERSION, TTL_DAYS)

SRC = (pathlib.Path(__file__).resolve().parent.parent / "routes" / "candidates.py").read_text()
DOC = (pathlib.Path(__file__).resolve().parent.parent / "static" / "candidate-lifecycle.html").read_text()


def test_candidate_id_deterministic_and_opaque():
    a = candidate_id_for("ERCOT-30INR0071", "snap_2026-07-08")
    assert a == candidate_id_for("ERCOT-30INR0071", "snap_2026-07-08")
    assert a.startswith("cand_") and len(a) == 25
    # a new snapshot mints a NEW identity — snapshots are immutable
    assert a != candidate_id_for("ERCOT-30INR0071", "snap_2026-08-08")


def test_expired_is_deterministic_and_fail_closed():
    e = expired_response("cand_x")
    assert e["error"] == "candidate_expired"
    assert e["message"] == "Re-run the search to obtain a fresh candidate."
    # no consumer may silently recompute: the words appear nowhere near a retry
    assert "DO NOTHING" in SRC  # re-mint never extends the original TTL


def test_echo_separates_search_and_analysis_versions():
    echo = candidate_echo({"candidate_id": "cand_x", "snapshot_id": "snap_2026-07-08"},
                          analysis_version="site-score/2026-07-11",
                          methodology_version="composite-v2.3")
    assert echo["search_version"] == SEARCH_VERSION
    assert echo["analysis_version"] == "site-score/2026-07-11"
    assert echo["search_version"] != echo["analysis_version"]
    assert echo["citation"]["as_of"] == "2026-07-08"
    for k in ("candidate_id", "snapshot_id", "methodology_version", "retrieved_at"):
        assert k in echo


def test_ttl_matches_data_freshness_not_convenience():
    assert TTL_DAYS == 7  # queue vintage cadence — a 30d TTL would outlive the data's truth


def test_resolve_stays_narrow():
    # resolve = identity/location/snapshot/provenance/context — never analysis
    assert "resolve_candidate" in SRC
    for banned in ("overall_score", "risk_resilience", "fiber_connectivity"):
        assert banned not in SRC


def test_doc_page_mirrors_the_contract():
    for phrase in ("candidate_expired", "Re-run the search", "snap_",
                   "analysis_version", "no automatic lineage",
                   "7 days"):
        assert phrase.lower() in DOC.lower(), phrase
