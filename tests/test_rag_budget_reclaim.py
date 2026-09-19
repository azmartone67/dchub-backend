"""The reindex budget must not strand itself in empty corpora.

`_pending` splits the per-run cap evenly across all 18 corpora so one large
corpus cannot starve the others. Measured 2026-09-19, that split was also the
ceiling: two cron runs at cap=500 embedded 193 rows between them (19% of
budget) of which exactly 26 per run were `announcements` — 500//18 — against a
1,383-row backlog that grows daily. Nine days to drain a backfill the budget
could absorb in three runs.

The fix is a second pass that hands back only what no other corpus asked for.
Fairness first, then reclamation.

Stdlib + pytest; no DB, no network.
"""
import re

import pytest

from routes import brain_rag as rag


class FakeCursor:
    """A cursor that OBEYS the SQL it is given.

    It reads the corpus out of the SELECT, honours LIMIT, and applies the
    `<> ALL(%s)` exclusion from the bound parameters. A stub that ignored any
    of those would make every assertion below pass for the wrong reason --
    the duplicate test especially, which is the one real hazard in a second
    pass over unordered rows."""

    def __init__(self, pending):
        self.pending = {k: list(v) for k, v in pending.items()}
        self._rows = []
        self.queries = []

    def execute(self, q, params=None):
        self.queries.append((q, params))
        src = re.search(r"SELECT '([a-z_]+)'", q).group(1)
        lim = int(re.search(r"LIMIT (\d+)", q).group(1))
        pool = self.pending.get(src, [])
        if "<> ALL(%s)" in q:
            assert params, "the exclusion clause was rendered with no parameters"
            excluded = set(params[0])
            pool = [sid for sid in pool if sid not in excluded]
        else:
            assert params is None, "params bound with no clause to consume them"
        self._rows = [(src, sid, "kind", f"text-{sid}") for sid in pool[:lim]]

    def fetchall(self):
        return self._rows


@pytest.fixture(autouse=True)
def _no_fresh_col(monkeypatch):
    """Every corpus behaves as insert-only, like `announcements` really is."""
    monkeypatch.setattr(rag, "_fresh_col_active", lambda *a, **k: False)


def _ids(n, tag="a"):
    return [f"{tag}{i}" for i in range(n)]


N_CORPORA = len(rag.CORPORA) + len(rag.CHUNKED_CORPORA)


def test_the_even_split_is_still_what_a_full_field_gets():
    """Fairness is the property the split exists for: when every corpus has
    work, nobody may take more than its slice before the others are served."""
    cap = 360
    per = cap // N_CORPORA
    cur = FakeCursor({src: _ids(500, src) for src in rag.CORPORA})
    rows = rag._pending(cur, cap)
    by = {}
    for src, *_ in rows:
        by[src] = by.get(src, 0) + 1
    assert len(rows) <= cap
    # ★ The POPULATION first. `min(by.values()) >= per` alone is vacuous: if
    # one corpus swallowed the whole cap, `by` has a single entry and its
    # minimum sails past. Mutation P6 (pass one stops honouring the slice)
    # passed against exactly that hole.
    assert set(by) == set(rag.CORPORA), (
        f"{len(set(rag.CORPORA) - set(by))} corpora got nothing: "
        f"{sorted(set(rag.CORPORA) - set(by))}")
    assert min(by.values()) >= per, by
    # NOT `max <= per`: once every corpus has been served, reclamation giving
    # the leftover to one of them is the feature, not a fairness breach.


def test_one_corpus_backlog_gets_the_budget_the_empty_corpora_did_not_use():
    """★ The measured case. `announcements` is 1,383 behind and every other
    corpus is current, so the run should spend the cap on it -- not 1/18 of
    the cap and then stop with 94% of the budget unspent."""
    cap = 500
    cur = FakeCursor({"announcements": _ids(1383)})
    rows = rag._pending(cur, cap)
    assert len(rows) == cap, f"took {len(rows)} of a {cap} budget"
    assert {r[0] for r in rows} == {"announcements"}
    # and the old behaviour is what this rules out
    assert len(rows) > cap // N_CORPORA * 2


def test_the_second_pass_never_returns_a_row_twice():
    """The real hazard. Rows are unordered for an insert-only corpus, so a
    naive second query returns the SAME rows -- they would be embedded twice
    in one run and the cap would buy half as much as it claims."""
    cur = FakeCursor({"announcements": _ids(400)})
    rows = rag._pending(cur, 300)
    sids = [r[1] for r in rows]
    assert len(sids) == len(set(sids)), "the second pass re-took pass-one rows"


def test_a_corpus_with_less_than_its_slice_is_not_asked_twice():
    """Reclamation targets corpora that FILLED their slice. One that came up
    short has nothing left, and re-querying it costs a round trip per run."""
    cur = FakeCursor({"announcements": _ids(3)})
    rows = rag._pending(cur, 500)
    assert len(rows) == 3
    asked = [q for q, _ in cur.queries if "'announcements'" in q]
    assert len(asked) == 1, f"queried a short corpus {len(asked)}x"


def test_the_cap_is_still_the_cap():
    cap = 100
    cur = FakeCursor({src: _ids(900, src) for src in rag.CORPORA})
    assert len(rag._pending(cur, cap)) <= cap


def test_no_corpus_is_queried_once_the_budget_is_gone():
    """A spent budget renders `LIMIT 0`, which returns nothing but still costs
    a round trip per remaining corpus. Mutation P4 (dropping the cap check in
    the second pass) was invisible until this existed."""
    cap = 100
    cur = FakeCursor({src: _ids(900, src) for src in rag.CORPORA})
    rag._pending(cur, cap)
    zero = [q for q, _ in cur.queries if "LIMIT 0" in q]
    assert not zero, f"{len(zero)} query/queries issued with an empty budget"


def test_a_corpus_that_raises_is_skipped_not_fatal():
    """Unchanged behaviour: a corpus whose columns do not resolve rolls back
    and the run continues."""
    class Boom(FakeCursor):
        class _C:
            def rollback(self): pass
        connection = _C()

        def execute(self, q, params=None):
            if "'deals'" in q:
                raise RuntimeError("column does not exist")
            super().execute(q, params)

    cur = Boom({"deals": _ids(50), "announcements": _ids(50)})
    rows = rag._pending(cur, 200)
    assert {r[0] for r in rows} == {"announcements"}
