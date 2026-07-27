"""tests/test_question_class.py — free-text question classifier (2026-07-27).

Guards routes/_question_class.py, the instrument that closes Shell #37 lane 4.
Its ONLY job is to reproduce, mechanically, the hand-audited taxonomy the
2026-07-27 GraphRAG demand read used, so a re-read in ~October is like-for-like
(reference_dchub_global_question_demand). That makes its failure modes unusual:

  · a RENAMED or re-cut bucket silently invalidates the baseline — the numbers
    still look plausible, they just no longer mean the same thing
  · a bucket that swallows an unrelated shape (e.g. classifying "ashburn" as
    global_synthesis) would manufacture demand that does not exist, and
    global_synthesis is the single number a GraphRAG build decision turns on
  · raising inside the /track callback would break call logging

Baseline over 1,032 EXTERNAL free-text calls (self/test platforms excluded):
    entity_lookup 674 · topical 253 · noise 84 · parametric 14 · thematic 4
    · question_local 3 · global_synthesis 0

Run:  python3 -m pytest tests/test_question_class.py -v
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from routes._question_class import BUCKETS, classify  # noqa: E402


# ── the buckets are a FROZEN contract ─────────────────────────────────

def test_bucket_names_are_frozen():
    """Renaming a bucket breaks comparability with the 07-27 baseline. If the
    taxonomy genuinely must change, VERSION it — never redefine in place."""
    assert BUCKETS == ("entity_lookup", "topical", "noise", "parametric",
                       "thematic", "question_local", "global_synthesis")


def test_classify_only_ever_returns_a_known_bucket_or_none():
    for probe in ("ashburn", "", "why do themes emerge across markets?",
                  "rank markets for a 200 MW AI campus", "test", None, 42,
                  {"nested": "dict"}, "data center liquid cooling pue"):
        got = classify({"query": probe} if not isinstance(probe, dict) else probe)
        assert got is None or got in BUCKETS, f"{probe!r} -> {got!r}"


# ── None means "no question", never "unclassified" ────────────────────

def test_none_only_when_no_text_key_present():
    assert classify({"market": "ashburn", "iso": "PJM"}) is None
    assert classify({}) is None
    assert classify(None) is None
    # key PRESENT but blank is a degenerate question, not an absent one —
    # 30 such calls exist in the 07-27 window and belong in `noise`
    assert classify({"query": ""}) == "noise"
    assert classify({"query": "   "}) == "noise"


def test_classify_never_raises():
    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")
    assert classify(Exploding()) is None


# ── the taxonomy, by example ──────────────────────────────────────────

@pytest.mark.parametrize("text,bucket", [
    # entity / place lookups — 65% of real traffic
    ("ashburn", "entity_lookup"),
    ("Equinix", "entity_lookup"),
    ("northern virginia", "entity_lookup"),
    # structured asks the deterministic planner already serves
    ("rank markets for a 200 MW AI campus", "parametric"),
    ("find 50 MW in Dallas", "parametric"),
    ("site selection", "parametric"),
    # single-anchor questions
    ("why is northern virginia constrained", "question_local"),
    # cross-document synthesis — THE bucket a graph would serve
    ("what are the main risk patterns across our supplier relationships",
     "global_synthesis"),
    ("which themes are emerging across the 2026 filings?", "global_synthesis"),
    # thematic phrasing without a question form
    ("data center emerging markets", "thematic"),
    # keyword bags
    ("data center liquid cooling waste heat recovery pue 2026", "topical"),
    # noise
    ("test", "noise"),
    ("hello", "noise"),
    ("any kind of leads in google map any city", "noise"),
])
def test_taxonomy_examples(text, bucket):
    assert classify({"query": text}) == bucket


def test_parametric_beats_question_form():
    """"rank markets for a 200 MW campus" reads like a request but is a planner
    job — it must not inflate the synthesis bucket."""
    assert classify({"query": "how do I rank markets for a 200 MW campus"}) == "parametric"


def test_entity_lookup_never_becomes_global_synthesis():
    """The number a GraphRAG build decision turns on. A single-word place name
    leaking into this bucket would manufacture demand that does not exist."""
    for t in ("ashburn", "virginia", "texas", "equinix", "dallas", "phoenix"):
        assert classify({"query": t}) != "global_synthesis"


def test_all_text_keys_are_read():
    for key in ("intent", "query", "q"):
        assert classify({key: "ashburn"}) == "entity_lookup"


# ── live: must still reproduce the frozen baseline ────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL — live checks skipped")

_SELF = {"dchub-internal", "dchub-regression-test", "probe", "mcp-probe",
         "mcp-server-validator", "step2_test", "gating-audit", "capwall2",
         "curl", "t", "w", "v", "p", "?"}


@_live
def test_live_replay_reproduces_the_frozen_baseline():
    """★ The whole point. Replay every external free-text call recorded before
    the baseline read and require the same distribution. Traffic accumulates,
    so counts may only GROW — a bucket that shrinks means the classifier was
    re-cut and the October comparison is void."""
    import psycopg2
    base = {"entity_lookup": 674, "topical": 253, "noise": 84, "parametric": 14,
            "thematic": 4, "question_local": 3, "global_synthesis": 0}
    c = psycopg2.connect(_DB, connect_timeout=15)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(params->>'intent', params->>'query', params->>'q'),"
            "       COALESCE(platform,'?'), count(*)"
            "  FROM mcp_call_log"
            " WHERE COALESCE(params->>'intent', params->>'query', params->>'q')"
            "       IS NOT NULL"
            "   AND timestamp < '2026-07-27 04:00:00+00'"
            " GROUP BY 1, 2")
        rows = cur.fetchall()
    c.close()

    got: dict[str, int] = {}
    for text, platform, n in rows:
        if platform in _SELF:
            continue
        b = classify({"query": text})
        assert b is not None, f"external free text classified as absent: {text!r}"
        got[b] = got.get(b, 0) + int(n)

    for bucket, expected in base.items():
        actual = got.get(bucket, 0)
        assert actual >= expected, (
            f"{bucket}: {actual} < baseline {expected} — the classifier was "
            "re-cut and the October comparison is no longer valid")
    assert got.get("global_synthesis", 0) == 0, (
        "global_synthesis moved off zero on HISTORICAL data — the bucket "
        "widened, it is not new demand")
