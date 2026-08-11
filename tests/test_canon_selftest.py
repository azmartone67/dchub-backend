"""Guards for routes/canon_selftest.py — the published self-test.

★ WHY THIS FILE EXISTS

We briefed eight external agents by hand for a week. Seven agreed with the
brief. The one that CALLED THE TOOLS found a defect nobody else had. A brief is
a chat message: it expires, it reaches only whoever was pasted it, and it cannot
be re-run. This endpoint is the benchmark as data instead — one fetch, no
operator in the loop.

★ THE TEST THAT MATTERS IS test_invariants_encode_classes_not_values.

The obvious version of this endpoint encodes "top row = Midland-Odessa" and is
worthless within a week: DCPI rescores nightly, the row changes, every agent
reports a false failure, and everyone learns to ignore the benchmark. A
self-test that cries wolf is worse than none. Invariants must encode CLASSES of
correctness; today's data belongs in `informational`, which is never asserted.

Pure module data: no DB, no network, and never imports main.
"""
import json
import re

import pytest

st = pytest.importorskip("routes.canon_selftest")


def test_checks_are_well_formed():
    assert len(st.CHECKS) >= 3
    seen = set()
    for c in st.CHECKS:
        assert set(c) == {"id", "tool", "args", "why", "defect_origin",
                          "invariant", "informational"}
        assert c["id"] not in seen, f"duplicate check id: {c['id']}"
        seen.add(c["id"])
        assert re.fullmatch(r"[a-z][a-z0-9-]+", c["id"])
        assert c["invariant"], f"{c['id']} asserts nothing"
        assert len(c["why"]) >= 60, f"{c['id']}: why must state the defect"


def test_every_check_names_the_defect_it_would_have_caught():
    """A check whose origin nobody remembers gets deleted by a future cleanup
    as 'probably redundant'. Naming the day it was false keeps it alive.
    """
    for c in st.CHECKS:
        assert re.match(r"^20\d\d-\d\d-\d\d", c["defect_origin"]), c["id"]


def test_invariants_encode_classes_not_values():
    """THE test. An invariant that pins today's data will be false tomorrow and
    will train every consumer to ignore this endpoint.

    Volatile facts — which market ranks first, what it scored — belong in
    `informational`, which is reported and never asserted.
    """
    volatile = re.compile(
        r"midland|odessa|el paso|woodlands|katy|ashburn|columbus|dallas"
        r"|composite_score|excess_power_score",
        re.I)
    for c in st.CHECKS:
        for a in c["invariant"]:
            blob = json.dumps(a)
            assert not volatile.search(blob), (
                f"{c['id']} asserts a volatile value in an invariant: {a}. "
                "Move it to `informational` — it will be false after the next "
                "rescore and the benchmark will start crying wolf."
            )


def test_the_geography_invariant_is_the_class_not_the_row():
    """The Texas defect stated correctly: not 'returns Midland-Odessa' but
    'every returned row is inside the requested geography'. That statement was
    FALSE on 2026-08-10 and stays the right assertion forever.
    """
    geo = next(c for c in st.CHECKS if c["id"] == "geography-is-honored")
    ops = {a["op"] for a in geo["invariant"]}
    assert "all_equal" in ops
    row_check = next(a for a in geo["invariant"] if a["op"] == "all_equal")
    assert "shortlist[*].state" in row_check["path"]
    assert row_check["value"] == "TX"


def test_informational_is_where_the_volatile_data_lives():
    """Inverse of the above: the volatile fields must actually be published
    somewhere, or the output is less useful than it should be for a human.
    """
    geo = next(c for c in st.CHECKS if c["id"] == "geography-is-honored")
    paths = " ".join(a["path"] for a in geo["informational"])
    assert "market" in paths
    for a in geo["informational"]:
        assert "note" in a and a["note"]


def test_every_invariant_says_what_it_means():
    """An assertion an agent cannot interpret produces a report we cannot act
    on. Each one carries a plain-language `means`.
    """
    for c in st.CHECKS:
        for a in c["invariant"]:
            assert a.get("means"), f"{c['id']}: invariant without `means`: {a}"


def test_known_gaps_are_published():
    """Without this list, an agent finds something we already know, files it,
    and the signal-to-noise of the channel drops until nobody reads it.
    """
    assert len(st.KNOWN_GAPS) >= 3
    ids = set()
    for g in st.KNOWN_GAPS:
        assert set(g) == {"id", "symptom", "status", "why", "do"}
        assert g["id"] not in ids
        ids.add(g["id"])
        # The caller can only observe the SYMPTOM — that is what they'd file.
        assert len(g["symptom"]) >= 30
        assert g["do"], f"{g['id']} states a gap with no guidance"
    # The two live gaps agents are most likely to trip must be listed.
    assert "citation-string-on-anonymous" in ids
    assert "capacity-mw-not-a-filter" in ids


def test_counts_are_derived():
    for c, payload in zip(st.CHECKS, st._checks()):
        assert payload["invariant_count"] == len(c["invariant"])


def test_report_back_asks_for_facts_not_prose():
    """The failure mode of the whole programme: agents returning strategy
    documents instead of a failing call.
    """
    rb = st.selftest_payload()["report_back"]
    assert any("verbatim" in w for w in rb["what"])
    assert "raw response" in " ".join(rb["what"])
    assert "assessment" in rb["not_wanted"]


def test_contract_hash_is_content_sensitive():
    before = st.contract_hash()
    original = st.KNOWN_GAPS
    try:
        st.KNOWN_GAPS = original + ({
            "id": "new-gap", "symptom": "a newly discovered observable symptom here",
            "status": "known", "why": "because", "do": "read the docs"},)
        assert st.contract_hash() != before
    finally:
        st.KNOWN_GAPS = original
    assert st.contract_hash() == before


def test_endpoint_serves_the_module_payload():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(st.canon_selftest_bp)
    r = app.test_client().get("/api/v1/canon/selftest")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["contract_hash"] == st.contract_hash()
    assert len(body["checks"]) == len(st.CHECKS)
    assert len(body["known_gaps"]) == len(st.KNOWN_GAPS)
    assert "public" in r.headers.get("Cache-Control", "")
