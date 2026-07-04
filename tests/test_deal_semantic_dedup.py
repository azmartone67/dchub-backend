"""Semantic near-duplicate gate on deal ingest (r-rag-dealdedup 2026-07-04).

Covers _semantic_dedup_pass in deal_ingestion_scheduler.py with a fully
mocked routes.brain_rag (no Flask, no DB, no Cohere):

  (a) "Core Weave Inc." vs existing "CoreWeave", same value      -> DUP (dropped)
  (b) same parties, clearly different value (follow-on deal)     -> INSERT (kept)
  (c) embed/RAG failure                                          -> md5-only (all kept)
  (d) one/both values undisclosed                                -> ALWAYS KEPT
      (blocker fix 2026-07-04: dedup may drop ONLY when BOTH values are
      disclosed AND within DEAL_DUP_VALUE_TOL — a dropped real follow-on
      deal between repeat counterparties is permanent loss)
  plus: md5 fast path skips semantic work, rerank scores are never
  thresholded, query enriched with deal_type+deal_date, kill switch.

Run with:  python3 -m pytest tests/test_deal_semantic_dedup.py -v
"""
import re
import sys
import types

import pytest

import deal_ingestion_scheduler as dis


# ── fakes ─────────────────────────────────────────────────────────────
def _fake_vec(text):
    """Toy embedding space: anything mentioning CoreWeave (any spelling,
    punctuation, spacing) lands on one axis, everything else on another."""
    t = re.sub(r'[^a-z0-9]', '', (text or '').lower())
    if 'coreweave' in t:
        return [1.0, 0.0, 0.0]
    return [0.0, 1.0, 0.0]


def fake_embed_ok(texts, input_type="search_document"):
    return [_fake_vec(t) for t in texts]


def fake_embed_fail(texts, input_type="search_document"):
    return None  # mirrors brain_rag._embed on any Cohere failure


class FakeCursor:
    def __init__(self, existing_hashes=(), deals_values=None):
        self.existing_hashes = list(existing_hashes)
        self.deals_values = deals_values or {}   # deals.id -> value in $M
        self._result = None
        self.connection = self                    # for cur.connection.rollback()
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if 'FROM ai_deals WHERE deal_hash' in sql:
            self._result = [(h,) for h in self.existing_hashes]
        elif 'FROM deals WHERE id' in sql:
            v = self.deals_values.get(params[0])
            self._result = (v,) if v is not None else None
        else:
            self._result = None

    def fetchall(self):
        return self._result or []

    def fetchone(self):
        return self._result

    def rollback(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def _install_fake_brain_rag(monkeypatch, retrieve_fn, embed_fn):
    fake = types.ModuleType('routes.brain_rag')
    fake.retrieve_context = retrieve_fn
    fake._embed = embed_fn
    pkg = types.ModuleType('routes')
    pkg.brain_rag = fake
    monkeypatch.setitem(sys.modules, 'routes', pkg)
    monkeypatch.setitem(sys.modules, 'routes.brain_rag', fake)
    return fake


def _deal(buyer, seller, usd, display, h):
    return {
        'buyer': buyer, 'seller': seller, 'deal_type': 'acquisition',
        'deal_value_usd': usd, 'deal_value_str': display,
        'deal_date': '2026-07-04', 'source_url': 'https://x.test/a',
        'source_name': 'Test', 'description': f'acquisition: {buyer} → {seller}',
        'deal_hash': h,
    }


# NOTE the deliberately LOW score (0.08): retrieve_context returns cross-
# encoder RERANK relevances (~0.05-0.3 even for excellent hits). The gate
# must NEVER threshold on it — dedup decides on local embed cosine only.
CORWEAVE_HIT = {
    'source_table': 'deals', 'source_id': 'D1', 'kind': 'deal',
    'text': 'CoreWeave → Core Scientific (acquisition, Texas) all-stock deal',
    'score': 0.08,
}


# ── (a) near-duplicate spelling, same value → dup ─────────────────────
def test_spelling_variant_same_value_is_dropped(monkeypatch):
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)
    cur = FakeCursor(existing_hashes=[], deals_values={'D1': 9000.0})  # $9,000M=$9B
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_a')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == []  # dropped: cosine 1.0 >= 0.92 and value within 15%


def test_low_rerank_score_does_not_block_dup(monkeypatch):
    """Rerank score 0.08 must be ignored — cosine alone gates the dup."""
    hit = dict(CORWEAVE_HIT, score=0.05)
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [hit],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={'D1': 9000.0})
    out = dis._semantic_dedup_pass(
        [_deal('CoreWeave Inc', 'Core Scientific', 9_000_000_000, '$9B', 'h_a2')],
        lambda: FakeConn(cur))
    assert out == []


def test_undisclosed_values_are_kept(monkeypatch):
    """BLOCKER FIX: one/both values undisclosed → ALWAYS KEEP, even at
    cosine 1.0. Repeat counterparties do multiple deals; without both values
    disclosed there is no evidence this is the SAME transaction, and a
    dropped real deal is permanent loss (a dup row is recoverable)."""
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={})  # candidate value unreadable → undisclosed
    deals = [_deal('Core Weave Inc.', 'Core Scientific', None, None, 'h_u')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals  # BOTH undisclosed → kept


def test_followon_undisclosed_after_disclosed_corpus_deal_is_kept(monkeypatch):
    """Follow-on: same parties, first deal $2B DISCLOSED in the corpus, new
    deal UNDISCLOSED → must be KEPT (this exact case was being dropped)."""
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={'D1': 2000.0})  # existing $2,000M = $2B
    deals = [_deal('CoreWeave', 'Core Scientific', None, None, 'h_f1')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals  # new value undisclosed → kept despite cosine 1.0


def test_disclosed_new_vs_undisclosed_corpus_is_kept(monkeypatch):
    """Mirror case: new deal disclosed, corpus candidate undisclosed → KEPT."""
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={})  # candidate has no value row
    deals = [_deal('CoreWeave', 'Core Scientific', 2_000_000_000, '$2B', 'h_f2')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals


# ── (b) same parties, clearly different value → follow-on, insert ─────
def test_same_parties_different_value_is_kept(monkeypatch):
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={'D1': 9000.0})  # existing $9B
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 2_000_000_000, '$2B', 'h_b')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals  # $2B vs $9B >> 15% → follow-on transaction, insert


def test_low_cosine_is_kept(monkeypatch):
    """Different company entirely → cosine ~0 → keep."""
    hit = {'source_table': 'deals', 'source_id': 'D2', 'kind': 'deal',
           'text': 'Equinix → Vantage (acquisition, Virginia)', 'score': 0.30}
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [hit],
                            fake_embed_ok)
    cur = FakeCursor(deals_values={'D2': 9000.0})
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_c')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals


# ── (c) embed/RAG failure → exact md5-only behavior ───────────────────
def test_embed_failure_degrades_to_md5_only(monkeypatch):
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_fail)
    cur = FakeCursor(deals_values={'D1': 9000.0})
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_e1'),
             _deal('Another Buyer', 'Another Seller', None, None, 'h_e2')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals  # nothing dropped, batch untouched


def test_retrieve_exception_degrades_to_md5_only(monkeypatch):
    def boom(q, k=8, corpus=None):
        raise RuntimeError("RAG down")
    _install_fake_brain_rag(monkeypatch, boom, fake_embed_ok)
    cur = FakeCursor()
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_e3')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals


def test_db_hash_lookup_failure_degrades_to_md5_only(monkeypatch):
    _install_fake_brain_rag(monkeypatch, lambda q, k=8, corpus=None: [CORWEAVE_HIT],
                            fake_embed_ok)

    def bad_get_db():
        raise RuntimeError("pool exhausted")
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_e4')]
    out = dis._semantic_dedup_pass(deals, bad_get_db)
    assert out == deals


# ── md5 fast path + kill switch ───────────────────────────────────────
def test_existing_hash_skips_semantic_check(monkeypatch):
    calls = {'retrieve': 0}

    def counting_retrieve(q, k=8, corpus=None):
        calls['retrieve'] += 1
        return [CORWEAVE_HIT]
    _install_fake_brain_rag(monkeypatch, counting_retrieve, fake_embed_ok)
    cur = FakeCursor(existing_hashes=['h_seen'], deals_values={'D1': 9000.0})
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_seen')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(cur))
    assert out == deals            # md5 path: row rides ON CONFLICT upsert
    assert calls['retrieve'] == 0  # no semantic work for known hashes


def test_kill_switch_env(monkeypatch):
    monkeypatch.setenv('DEAL_SEMANTIC_DEDUP', '0')

    def must_not_call(q, k=8, corpus=None):
        raise AssertionError("retrieve_context called despite kill switch")
    _install_fake_brain_rag(monkeypatch, must_not_call, fake_embed_ok)
    deals = [_deal('Core Weave Inc.', 'Core Scientific', 9_000_000_000, '$9B', 'h_k')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(FakeCursor()))
    assert out == deals


# ── pure helpers ──────────────────────────────────────────────────────
def test_cosine_basics():
    assert dis._cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert dis._cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert dis._cosine([], []) == 0.0


def test_values_confirm_dup():
    """Dup may be confirmed ONLY when both values are disclosed AND close."""
    assert dis._values_confirm_dup(9e9, 9.5e9, 0.15)          # both disclosed, within 15%
    assert not dis._values_confirm_dup(2e9, 9e9, 0.15)        # clearly different → keep
    assert not dis._values_confirm_dup(None, 9e9, 0.15)       # undisclosed new → keep
    assert not dis._values_confirm_dup(9e9, None, 0.15)       # undisclosed cand → keep
    assert not dis._values_confirm_dup(None, None, 0.15)      # both undisclosed → keep
    assert not dis._values_confirm_dup(0, 9e9, 0.15)          # zero == undisclosed → keep
    assert not dis._values_confirm_dup('junk', 9e9, 0.15)     # unparseable → keep


def test_query_enriched_with_type_and_date(monkeypatch):
    """The retrieve query must carry deal_type + deal_date discriminators
    (the extracted dicts have NO market/region keys — buyer+seller alone
    collapsed distinct follow-on deals between repeat counterparties)."""
    seen = {}

    def capturing_retrieve(q, k=8, corpus=None):
        seen['query'] = q
        return []  # no candidates → keep; we only care about the query
    _install_fake_brain_rag(monkeypatch, capturing_retrieve, fake_embed_ok)
    deals = [_deal('CoreWeave', 'Core Scientific', 2_000_000_000, '$2B', 'h_q')]
    out = dis._semantic_dedup_pass(deals, lambda: FakeConn(FakeCursor()))
    assert out == deals
    assert 'CoreWeave' in seen['query']
    assert 'Core Scientific' in seen['query']
    assert 'acquisition' in seen['query']    # deal_type
    assert '2026-07-04' in seen['query']     # deal_date


def test_candidate_value_units_are_millions():
    """deals.value is $MILLIONS — must scale to USD before comparing."""
    cur = FakeCursor(deals_values={'D1': 9000.0})
    assert dis._candidate_value_usd(cur, 'deals', 'D1') == pytest.approx(9e9)
    assert dis._candidate_value_usd(cur, 'deals', 'MISSING') is None
