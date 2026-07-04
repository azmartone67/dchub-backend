"""Semantic news→facility entity resolution (r-entityres 2026-07-04).

Covers _resolve_existing_semantic() in routes/news_entity_extraction.py —
the promote-time check that stops near-duplicate names from inflating
discovered_facilities:

  (a) near-name + same-city         → resolves to the EXISTING facility
  (b) same-name + different-state   → returns None (CREATE — new site)
  (c) embed failure                 → returns None (exact current behavior)
  plus: retrieve_context failure, below-threshold cosine, and the
  missing-location conservative path all return None.

No network, no DB — routes.brain_rag.retrieve_context/_embed and
routes.news_entity_extraction._get_db are all mocked. The local-cosine
rule is honored: rerank scores from retrieve_context are noise here
(set to 0.07-ish on purpose) and must never drive the decision.

Run with:  python3 -m pytest tests/test_news_entity_resolution.py -v
"""
from unittest import mock

import routes.news_entity_extraction as ner


# ── fakes ─────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def close(self):
        pass

    def rollback(self):
        pass


def _hit(source_id, text="whatever"):
    # NOTE: score is a cross-encoder rerank value — absolute magnitude is
    # meaningless (0.05-0.3 even for good hits) and must never be
    # thresholded. Deliberately tiny here.
    return {"source_table": "discovered_facilities", "source_id": str(source_id),
            "kind": "facility", "text": text, "score": 0.07}


def _patch(rag_hits, db_rows, embed_return):
    """Patch retrieve_context/_embed (lazily imported from routes.brain_rag)
    and the module's _get_db. Returns the context managers as a tuple."""
    return (
        mock.patch("routes.brain_rag.retrieve_context",
                   return_value=rag_hits),
        mock.patch("routes.brain_rag._embed",
                   return_value=embed_return),
        mock.patch.object(ner, "_get_db",
                          return_value=FakeConn(db_rows)),
    )


NEW_FACILITY = {
    "name": "QTS Atlanta DC-1", "provider": "QTS",
    "city": "Atlanta", "state": "GA", "country": "US",
}


# ── (a) near-name + same-city → resolve to EXISTING ─────────────────
def test_near_name_same_city_resolves_to_existing():
    rows = [(101, "QTS Atlanta Data Center 1", "Atlanta", "GA")]
    # cosine(query, cand) = 0.98 ≥ 0.90 default threshold
    vecs = [[1.0, 0.0], [0.98, 0.19899748742]]
    p1, p2, p3 = _patch([_hit(101)], rows, vecs)
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(dict(NEW_FACILITY))
    assert got is not None
    assert got["id"] == 101
    assert got["name"] == "QTS Atlanta Data Center 1"
    assert got["cosine"] >= 0.90


# ── (b) same-name + different-state → CREATE (None) ─────────────────
def test_same_name_different_state_creates_new():
    rows = [(202, "Meta Hyperscale Campus", "Altoona", "IA")]
    # Identical vectors → cosine 1.0: name similarity is MAX, but the
    # location clearly differs (both cities and both states present and
    # different) → never suppress a genuinely new facility.
    vecs = [[1.0, 0.0], [1.0, 0.0]]
    new = {"name": "Meta Hyperscale Campus",
           "city": "Mesa", "state": "AZ", "country": "US"}
    p1, p2, p3 = _patch([_hit(202)], rows, vecs)
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(new)
    assert got is None


# ── (c) embed failure → exact-match behavior (None → create) ────────
def test_embed_failure_falls_back_to_exact_match_behavior():
    rows = [(101, "QTS Atlanta Data Center 1", "Atlanta", "GA")]
    p1, p2, p3 = _patch([_hit(101)], rows, None)  # _embed → None
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(dict(NEW_FACILITY))
    assert got is None


def test_retrieve_context_failure_falls_back():
    p2 = mock.patch("routes.brain_rag._embed", return_value=None)
    p1 = mock.patch("routes.brain_rag.retrieve_context",
                    side_effect=RuntimeError("rag down"))
    p3 = mock.patch.object(ner, "_get_db", return_value=FakeConn([]))
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(dict(NEW_FACILITY))
    assert got is None


# ── below-threshold cosine → create ──────────────────────────────────
def test_low_cosine_creates_new():
    rows = [(303, "Switch Citadel", "Atlanta", "GA")]
    # cosine = 0.5 < 0.90 → not a duplicate even though city matches
    vecs = [[1.0, 0.0], [0.5, 0.86602540378]]
    p1, p2, p3 = _patch([_hit(303)], rows, vecs)
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(dict(NEW_FACILITY))
    assert got is None


# ── missing location on the new side → conservative create ──────────
def test_missing_location_never_merges():
    rows = [(101, "QTS Atlanta Data Center 1", "Atlanta", "GA")]
    vecs = [[1.0, 0.0], [1.0, 0.0]]  # cosine 1.0
    new = {"name": "QTS Atlanta DC-1", "city": None, "state": None,
           "country": "US"}
    p1, p2, p3 = _patch([_hit(101)], rows, vecs)
    with p1, p2, p3:
        got = ner._resolve_existing_semantic(new)
    assert got is None  # no POSITIVE location agreement possible


# ── pure-helper sanity ────────────────────────────────────────────────
def test_loc_agrees_matrix():
    assert ner._loc_agrees("Atlanta", "GA", "atlanta", None) is True   # city
    assert ner._loc_agrees(None, "GA", "Altoona", "ga") is True        # state
    assert ner._loc_agrees("Mesa", "AZ", "Altoona", "IA") is False     # differ
    assert ner._loc_agrees(None, None, "Atlanta", "GA") is False       # unknown
    assert ner._loc_agrees("Atlanta", None, None, "GA") is False       # cross


def test_cos_sim_basics():
    assert abs(ner._cos_sim([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(ner._cos_sim([1, 0], [0, 1])) < 1e-9
    assert ner._cos_sim([0, 0], [1, 0]) is None  # zero vector → None
