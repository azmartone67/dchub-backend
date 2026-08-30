"""RAG harness contracts (r-rag-receipt / r-rag-chunk-contract, 2026-08-29).

Three guards, each one converting a rule that has so far lived in prose into
a check that fails when the rule is broken.

  (1) RETRIEVAL RECEIPTS. retrieve_context fail-softs to [] on every failure,
      so a real zero-match answer, a dead DB, a 429'd embed provider and an
      HNSW post-filter that removed every row all reached callers BYTE-
      IDENTICALLY. That is why the 08-20 scoped-postfilter regression lived
      six days across 16 of 33 call sites. These tests pin that the four
      states are now distinguishable, and that the DEFAULT return type is
      still a plain list so none of those 33 call sites changed behaviour.

  (2) CHUNK CONTRACT. The chunker for the 17 flat corpora is a SQL `text`
      expression; graph_spine lane 4 measured its deals template collapsing
      11 distinct deals onto one vector via renders like "Google ->  (, )".
      Pinned here against that exact shape.

  (3) SERVE-GATE INVARIANTS. serve_gates() gates what is SERVED at both the
      retrieval and hydration ends. It relies on three relationships that
      currently hold ONLY BY INSPECTION, and two of them fail in the LEAK
      direction — silently, on a keyless public endpoint. They are pinned.

No network, no DB — _embed/_db are mocked.
Run with:  python3 -m pytest tests/test_rag_harness_contracts.py -v
"""
import logging
import os
import pytest

import routes.brain_rag as br


# ── fakes ─────────────────────────────────────────────────────────────
class _Cur:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, rows=None, raise_on_query=False):
        self._rows = rows or []
        self._raise = raise_on_query
    def cursor(self):
        if self._raise:
            raise RuntimeError("connection reset by peer")
        return _Cur(self._rows)
    def rollback(self): pass
    def commit(self): pass
    def close(self): pass


def _row(src, sid, cos):
    """A brain_corpus_embeddings row as _retrieve_core selects it."""
    return (src, sid, "kind", f"text for {sid}", cos)


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    # Kill BOTH rerank legs so fetch_k == k and the pipeline is deterministic.
    monkeypatch.setenv("BRAIN_RAG_RERANK", "0")
    monkeypatch.setattr(br, "_embed", lambda *a, **k: [[0.1] * br.EMBED_DIM])
    br._RECEIPTS.clear()
    yield
    br._RECEIPTS.clear()


# ── (1) receipts ──────────────────────────────────────────────────────
def test_default_return_type_is_still_a_plain_list():
    """The 33 existing call sites index and iterate this. If it ever becomes
    a tuple by default, every one of them breaks at once."""
    monkey = [_row("deals", "d1", 0.9)]
    br._db = lambda: _Conn(monkey)
    out = br.retrieve_context("q", k=1)
    assert isinstance(out, list)
    assert out and out[0]["source_table"] == "deals"


def test_real_zero_match_has_no_degraded_reason():
    """[] with degraded_reason None is the ONLY honest 'we found nothing'."""
    br._db = lambda: _Conn([])
    rows, rec = br.retrieve_context("q", k=8, with_receipt=True)
    assert rows == []
    assert rec["degraded_reason"] is None


@pytest.mark.parametrize("broken,expected", [
    ("db_unavailable", "db_unavailable"),
    ("db_error", "db_error:RuntimeError"),
    ("embed", "embed_unavailable"),
])
def test_each_failure_names_itself(monkeypatch, broken, expected):
    """The whole point: three failures that used to be indistinguishable
    from a zero-match answer now each carry their own reason."""
    if broken == "db_unavailable":
        br._db = lambda: None
    elif broken == "db_error":
        br._db = lambda: _Conn(raise_on_query=True)
    else:
        monkeypatch.setattr(br, "_embed", lambda *a, **k: None)
        monkeypatch.setattr(br, "_keyword_fallback", lambda *a, **k: [])
        br._db = lambda: _Conn([])
    rows, rec = br.retrieve_context("q", k=8, with_receipt=True)
    assert rows == []
    assert rec["degraded_reason"] == expected
    # and it is NOT confusable with the clean-zero case
    assert rec["degraded_reason"] is not None


def test_missing_corpus_is_the_postfilter_signature():
    """Ask for two corpora, get rows from one. That is what
    `announcements 18/32` and `discovered_facilities 0/32` looked like."""
    br._db = lambda: _Conn([_row("deals", "d1", 0.9)])
    rows, rec = br.retrieve_context(
        "q", k=8, corpus=["deals", "announcements"], with_receipt=True)
    assert rec["corpus_requested"] == ["deals", "announcements"]
    assert rec["corpus_returned"] == ["deals"]
    assert rec["corpora_missing"] == ["announcements"]


def test_a_saturated_call_missing_a_corpus_is_not_degraded(caplog):
    """★ The false positive this sensor shipped with.

    k rows cannot seat more than k corpora, so on a SATURATED call
    (k_returned == k_requested) a corpus being absent is a RANKING outcome,
    not a fault. Measured live 2026-08-30 in the first hour after #3337
    deployed: 26 of 40 `rag.retrieve` log lines were WARNING with
    reason=None and k_returned == k_requested, while scoped single-corpus
    probes returned healthy rows for all nine corpora (cos 0.72-0.91).
    Railway files logger.warning under severity=error, so a healthy system
    was flooding its own error stream and would have buried a real fault.

    corpora_missing STAYS on the receipt — it names which corpora fed the
    answer. It just no longer calls a healthy call degraded."""
    caplog.set_level(logging.WARNING, logger=br.logger.name)
    br._db = lambda: _Conn([_row("deals", "d1", 0.9), _row("deals", "d2", 0.88)])
    rows, rec = br.retrieve_context(
        "q", k=2, corpus=["deals", "announcements"], with_receipt=True)

    # saturated, clean, and yet a corpus is absent
    assert rec["k_returned"] == rec["k_requested"] == 2
    assert rec["degraded_reason"] is None
    assert rec["truncated"] is False
    assert rec["corpora_missing"] == ["announcements"]   # still reported

    # ...and NOTHING called it degraded
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "a saturated, undegraded retrieval must not log a DEGRADED warning"


def test_an_unsaturated_missing_corpus_still_warns(caplog):
    """The other side of the same line — do not over-correct into silence.
    Ask for 8, get 1: the set had room for `announcements` and it is absent,
    which is the 18-of-32 signature. That must still be loud."""
    caplog.set_level(logging.WARNING, logger=br.logger.name)
    br._db = lambda: _Conn([_row("deals", "d1", 0.9)])
    rows, rec = br.retrieve_context(
        "q", k=8, corpus=["deals", "announcements"], with_receipt=True)

    assert rec["k_returned"] == 1 and rec["k_requested"] == 8
    assert rec["truncated"] is True
    assert rec["corpora_missing"] == ["announcements"]
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned, "an unsaturated retrieval missing a corpus must still warn"
    assert "DEGRADED" in warned[0].getMessage()


def test_truncation_is_visible():
    """k_returned < k_requested with nothing broken — the silent-truncation
    case that served 18 of a requested 32 for weeks."""
    br._db = lambda: _Conn([_row("deals", f"d{i}", 0.9) for i in range(3)])
    rows, rec = br.retrieve_context("q", k=8, corpus="deals", with_receipt=True)
    assert len(rows) == 3
    assert rec["k_requested"] == 8 and rec["k_returned"] == 3
    assert rec["truncated"] is True
    assert rec["degraded_reason"] is None


def test_receipt_never_retains_the_query_text():
    """/api/v1/rag/search is keyless and public — its queries are other
    people's. A fingerprint correlates repeats; the text is not kept."""
    br._db = lambda: _Conn([])
    secret = "acquisition target codename bluebird"
    _, rec = br.retrieve_context(secret, k=4, with_receipt=True)
    assert secret not in repr(rec)
    assert rec["query_fp"] and len(rec["query_fp"]) == 12
    assert rec["query_chars"] == len(secret)


def test_every_call_is_ringed_even_without_with_receipt():
    br._db = lambda: _Conn([])
    br.retrieve_context("q", k=2)
    assert len(br._RECEIPTS) == 1


# ── (2) chunk contract ────────────────────────────────────────────────
@pytest.mark.parametrize("text,reason", [
    ("Google →  (, )", "too_short"),   # lane 4's buyer-only render, verbatim
    ("Google ->  (, )", "too_short"),            # same with an ascii arrow
    ("Digital Realty →  (, )", "too_short"),
    (" —  (, )", "too_short"),               # pure template, every field empty
    ("Equinix Frankfurt Amsterdam London datacentre", ""),   # 5 tokens, passes
    ("aaa bb cc", "too_short"),
    ("interconnection interconnection interconnection", "uninformative"),
    # ★ Load-bearing case for the punctuation strip: 24+ chars of PURE
    # template. Without the strip this is long enough to reach the token
    # check and comes back "uninformative"; with it, bare is empty and the
    # honest answer is "too_short". Pinning the reason is what makes a
    # mutation that removes the strip fail instead of surviving.
    ("— (, ) — (, ) — (, ) — (, )", "too_short"),   # 27 chars stripped
])
def test_contract_catches_template_only_renders(text, reason):
    """Pins WHICH branch fires, not merely that one does. Asserting only
    truthiness let a mutation that disables the too_short branch survive —
    `uninformative` caught the same strings and the test stayed green."""
    assert br.chunk_contract(text) == reason


@pytest.mark.parametrize("text", [
    "Google → Vantage (acquisition, Northern Virginia)",
    "CyrusOne Sterling IX hyperscale campus, Ashburn VA",
])
def test_contract_passes_real_rows(text):
    assert br.chunk_contract(text) == ""


def test_contract_is_report_only_on_the_index_path():
    """If this ever starts SKIPPING rows, they stay in _pending forever:
    re-selected every run, eating the cap, `remaining` never reaching 0."""
    rows = [("deals", "d1", "deal", "Google →  (, )"),
            ("deals", "d2", "deal", "Google → Vantage (acquisition, NoVA)")]
    rep = br.contract_report(rows)
    assert rep == {"too_short": 1}
    assert len(rows) == 2, "contract_report must not mutate or filter its input"


# ── (3) serve-gate invariants ─────────────────────────────────────────
def test_every_gated_corpus_is_hydratable():
    """_hydrate drops any gated row it cannot confirm servable, and a corpus
    absent from _HYDRATE is `continue`d before it can ever be confirmed. So
    gated-but-not-hydratable silently drops 100% of that corpus's results."""
    missing = sorted(set(br.serve_gates()) - set(br._HYDRATE))
    assert not missing, f"gated but not hydratable (drops 100%): {missing}"


def test_every_public_corpus_is_hydratable():
    """PUBLIC_CORPORA is the keyless surface. A public corpus with no
    _HYDRATE spec is served with no citation and no serveability check."""
    missing = sorted(set(br.PUBLIC_CORPORA) - set(br._HYDRATE))
    assert not missing, f"public but not hydratable: {missing}"


def test_ungated_public_corpora_are_content_only():
    """serve_gates() covers only corpora whose served endpoint refuses rows;
    the rest are claimed to be content-only ("has nothing to enforce"). That
    claim is load-bearing on a KEYLESS surface and, until now, was a comment.

    If a status/flag/visibility column is ever added to one of these tables
    and the serving API starts filtering on it, this fails — instead of the
    leak reopening silently.
    """
    gated = set(br.serve_gates())
    suspicious = ("published", "status", "flag", "duplicate", "visible",
                  "approved", "deleted", "hidden", "quarantine")
    offenders = {}
    for src in br.PUBLIC_CORPORA:
        if src in gated or src in br.CHUNKED_CORPORA:
            continue
        where = (br.CORPORA.get(src) or {}).get("where", "")
        hits = [w for w in suspicious if w in where.lower()]
        if hits:
            offenders[src] = (where, hits)
    assert not offenders, (
        "these public corpora filter on a gate-shaped column in `where` but "
        "declare no serve_gates() entry, so the gate is applied when EMBEDDING "
        "and not when SERVING: " + repr(offenders))
