"""One document, two corpora, two of the agent's result slots.

★ WHAT WAS MEASURED (2026-08-31). Ten real queries against the live public MCP
gateway, anonymous tier, `semantic_search`:

    queries with a duplicate : 5/10
    duplicate rows           : 5/30 = 17% of every result slot

The repeat is always the SAME source_id arriving from two different corpora —
`announcements` and `news_articles` share an id scheme and overlap. Nothing
upstream is broken: brain_corpus_embeddings is UNIQUE (source_table, source_id),
so both rows are legitimate. And _duplicate_text_rows() only ever compared rows
WITHIN a single corpus, so it could never see this.

★ WHY THE KEY IS (source_id, title) AND NOT source_id ALONE. Several corpora key
on a bare integer (`t.id::text`), so press_releases#5 and permitting_intel#5 are
routinely the same source_id while being completely different documents. The
false-merge case is tested here explicitly — it is the risk this design carries.

No network, no DB.
"""
import routes.brain_rag as br


def _row(table, sid, text, cos):
    return {"source_table": table, "source_id": sid, "kind": "k",
            "text": text, "score": cos, "cosine": cos}


# ── the pure key ──────────────────────────────────────────────────────

def test_the_same_document_from_two_corpora_collapses_to_one():
    rows = [
        _row("announcements", "f2e0ce106ff47113",
             "Data center power line gets pushback in Northern Virginia — "
             "Data center power line gets pushback in Northern Virginia Bay Journal", 0.83),
        _row("news_articles", "f2e0ce106ff47113",
             "Data center power line gets pushback in Northern Virginia — "
             "Data center power line gets pushback in Northern Virginia Bay Journal", 0.81),
        _row("deals", "9911", "Aligned buys a Texas campus — $1.2bn", 0.79),
    ]
    out, dropped = br._dedup_same_document(rows)
    assert dropped == 1
    assert [r["source_table"] for r in out] == ["announcements", "deals"]
    # the HIGHER-ranked copy is the survivor — dedup must not re-rank
    assert out[0]["cosine"] == 0.83


def test_two_different_documents_sharing_an_integer_id_are_NOT_merged():
    """The false-merge case. `t.id::text` means a bare integer in several
    corpora, so identical source_ids across tables are routine and expected."""
    rows = [
        _row("press_releases", "5", "DC Hub launches the grid index — a new index", 0.80),
        _row("permitting_intel", "5", "Loudoun County zoning update — setback rules", 0.78),
    ]
    out, dropped = br._dedup_same_document(rows)
    assert dropped == 0
    assert len(out) == 2


def test_an_unkeyable_row_is_never_dropped():
    """Fail-open: dedup may cost a repeat, never a unique document."""
    rows = [
        _row("x", None, "no id at all — body", 0.9),
        _row("y", "7", "", 0.8),
        _row("z", "8", " — leading separator, empty title", 0.7),
    ]
    out, dropped = br._dedup_same_document(rows)
    assert dropped == 0
    assert len(out) == 3


def test_entities_and_whitespace_do_not_defeat_the_key():
    rows = [
        _row("announcements", "abc", "Grid  pushback&nbsp;in Virginia — body one", 0.9),
        _row("news_articles", "abc", "Grid pushback in Virginia — body two", 0.8),
    ]
    out, dropped = br._dedup_same_document(rows)
    assert dropped == 1 and len(out) == 1


# ── wired in, not merely present ──────────────────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def rollback(self):
        return None

    def close(self):
        return None


def test_retrieve_core_actually_applies_the_dedup(monkeypatch):
    """★ The helper existing is not the same as the pipeline calling it. This
    drives _retrieve_core end to end and fails if the wiring is removed."""
    dup = ("Same headline — Same headline Source")
    rows = [("announcements", "dup1", "announcement", dup, 0.90),
            ("news_articles", "dup1", "news", dup, 0.88),
            ("deals", "77", "deal", "Another doc — different body", 0.60)]
    monkeypatch.setattr(br, "_embed", lambda *a, **k: [[0.1, 0.2, 0.3]])
    monkeypatch.setattr(br, "_db", lambda: _Conn(rows))
    monkeypatch.setattr(br, "_rerank_on", lambda: False)
    monkeypatch.setattr(br, "_neutral_rerank_on", lambda: False)

    rec = {}
    out, degraded = br._retrieve_core("northern virginia power", 3, None, rec)
    assert degraded is None
    assert rec.get("dedup_dropped") == 1
    ids = [(r["source_table"], r["source_id"]) for r in out]
    assert ids == [("announcements", "dup1"), ("deals", "77")]


def test_the_cosine_only_leg_over_fetches_so_dedup_refills_slots(monkeypatch):
    """With both rerank legs off the pool used to be exactly k, so dropping a
    duplicate returned k-1. It must over-fetch instead."""
    seen = {}

    class _CapCur(_Cur):
        def execute(self, sql, params=None):
            if params:
                seen["limit"] = params[-1]
            return None

    class _CapConn(_Conn):
        def cursor(self):
            return _CapCur(self._rows)

    monkeypatch.setattr(br, "_embed", lambda *a, **k: [[0.1]])
    monkeypatch.setattr(br, "_db", lambda: _CapConn([]))
    monkeypatch.setattr(br, "_rerank_on", lambda: False)
    monkeypatch.setattr(br, "_neutral_rerank_on", lambda: False)
    br._retrieve_core("q", 8, None, {})
    assert seen["limit"] > 8, (
        "the plain-cosine leg fetched exactly k, so any dedup there returns "
        "fewer than k results instead of refilling from the pool")
