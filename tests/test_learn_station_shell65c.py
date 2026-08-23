"""Learn station — agentic-loop master shell #65, part C (2026-08-22).

The claim loop's part 6: NEGATIVE results (claims the verifier REFUTED or the
owner RETRACTED, proposals the triage rejected as duplicates, fixes that
FAILED) become what the strategic planner and the lane driver RECALL before
they act. Three pieces, each pinned here:

  1. routes/brain_rag.py — corpora `claim_lessons` (brain_predictions_log
     WHERE outcome IN ('refuted','retracted')) and `proposal_lessons`
     (brain_enhancement_proposals WHERE status IN ('duplicate','rejected')),
     name-keyed with a `table` indirection, in LESSON_CORPORA and NEVER in
     PUBLIC_CORPORA (the press_releases / capacity_pipeline leak class);
     recall_negative_lessons(); learn_station_status();
     GET /api/v1/brain/learn/recall.
  2. routes/brain_strategic_planner.py — ctx["refuted_claims"] rendered under
     "WHAT WE GOT WRONG (do not repeat)"; routes/brain_lane_driver.py —
     RECALL ranks the negatives first.
  3. routes/brain_work_selector.py — the effect bandit reads the action-class
     run ledger (judged by the claim verifier) as a third source, and
     learned_outcome_weights() publishes the earned vocabulary with its raw
     counts.

House rules: NO DB, NO network, main.py is never imported. The scripted
cursor emulates psycopg2's binding step FIRST (`sql % params`), so a bare
percent beside a params tuple fails here exactly as the driver would.
Nothing runs at module scope.

Mutations this file must catch (run by hand, recorded in the PR):
  · drop the outcome gate from CORPORA['claim_lessons']['where']       → RED
  · add a negative corpus to PUBLIC_CORPORA                            → RED
  · drop the `refuted_claims` key / section from _build_prompt         → RED
  · drop recall_negative_lessons from the lane driver's _recall         → RED
  · drop the tertiary source from _read_class_rate                      → RED
  · drop the floor from learned_outcome_weights                         → RED

Run:  python3 -m pytest tests/test_learn_station_shell65c.py -q
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib
import json
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_rag as br                      # noqa: E402
import routes.brain_strategic_planner as planner   # noqa: E402
import routes.brain_lane_driver as lane_driver     # noqa: E402
import routes.brain_work_selector as ws            # noqa: E402

# A percent that is neither doubled nor a psycopg2 placeholder.
_BARE_PCT = re.compile(r"(?<!%)%(?![%s(diouxXeEfFgGcr])")

SEEDED_STATEMENT = ("public.deals pinned 1,800+ on /ai while the resolver "
                    "serves 1,900+")
SEEDED = [{
    "source_table": "claim_lessons", "source_id": "100945",
    "kind": "claim_lesson",
    "text": ("REFUTED: " + SEEDED_STATEMENT +
             " | expected canon:public.deals == 1,800+ | actual 1,900+ "
             "| regime 2026-08-22T04:08:00Z"),
    "score": 0.83, "cosine": 0.83, "negative": True,
}]


# ── scripted psycopg2 stand-ins ───────────────────────────────────────
class _Cur:
    """Answers chosen by a substring of the SQL (first match wins; an answer
    may be a callable (sql, params) -> rows, or an Exception to raise).
    Every execute is recorded. Binding is emulated BEFORE anything else —
    the 0730 geothermal lesson: a stub more forgiving than the driver
    certifies code the driver rejects."""

    def __init__(self, script, sink):
        self.script = script
        self.sink = sink
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if params is not None:
            sql % tuple(params)
        self.sink.append((sql, params))
        self._rows = []
        for key, answer in self.script:
            if key in sql:
                if isinstance(answer, Exception):
                    raise answer
                rows = answer(sql, params) if callable(answer) else answer
                self._rows = list(rows or [])
                break
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, script=()):
        self.script = list(script)
        self.executed = []
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return _Cur(self.script, self.executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _app():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(br.brain_rag_bp)
    return app


def _rule(app, endpoint):
    return next(r.rule for r in app.url_map.iter_rules() if r.endpoint == endpoint)


# ── 1 · registry: gate, membership, never public ──────────────────────
def test_negative_corpora_are_lessons_and_never_public():
    assert set(br.NEGATIVE_LESSON_CORPORA) == {"claim_lessons", "proposal_lessons"}
    for name in br.NEGATIVE_LESSON_CORPORA:
        assert name in br.CORPORA, name
        assert name in br.LESSON_CORPORA, name
        assert name not in br.PUBLIC_CORPORA, (
            f"{name} is brain-internal: the keyless /api/v1/rag/search serves "
            "left(text,500) of every PUBLIC_CORPORA row with a CC-BY stamp")
    # the older lesson corpora are still there — this is an extension, not a swap
    for name in ("autopilot_outcomes", "brain_finding_outcomes", "brain_lane_decisions"):
        assert name in br.LESSON_CORPORA


def test_claim_lessons_where_carries_the_outcome_gate():
    """The outcome IS the corpus. Without `t.outcome IN ('refuted','retracted')`
    every open and confirmed claim would embed as a 'lesson'; without the
    source_layer gate L16's own prediction rows (947 live, outcome NULL) ride
    along the moment one of them gets an outcome."""
    w = br.CORPORA["claim_lessons"]["where"]
    assert "t.outcome IN ('refuted','retracted')" in w
    assert "t.source_layer = 'CLAIM'" in w
    assert "coalesce(t.statement,'') <> ''" in w
    pw = br.CORPORA["proposal_lessons"]["where"]
    assert "t.status IN ('duplicate','rejected')" in pw
    # the text template renders the brief's four fields
    t = br.CORPORA["claim_lessons"]["text"]
    for field in ("t.outcome", "t.statement", "t.expected_value",
                  "t.outcome_evidence", "t.regime->>'as_of'"):
        assert field in t, field


def test_fresh_col_is_a_real_mtime_or_honestly_absent():
    """outcome_at is stamped by _stamp_outcome_sql/_retract_sql (TIMESTAMPTZ on
    live) — a real mtime. brain_enhancement_proposals has only created_at, and
    a creation timestamp is the guaranteed no-op fresh_col (_pending's
    predicate is t.<col> > e.updated_at), so that corpus declares none."""
    assert br.CORPORA["claim_lessons"]["fresh_col"] == "outcome_at"
    assert "fresh_col" not in br.CORPORA["proposal_lessons"]
    assert br._src_table("claim_lessons", br.CORPORA["claim_lessons"]) == "brain_predictions_log"
    assert br._src_table("proposal_lessons", br.CORPORA["proposal_lessons"]) == "brain_enhancement_proposals"
    # a plain corpus resolves to itself — the indirection is opt-in
    assert br._src_table("deals", br.CORPORA["deals"]) == "deals"
    assert br._src_table("deals", {}) == "deals"


def test_no_bare_percent_in_new_specs_or_selector_sql():
    """spec['where'] is f-string-interpolated into queries that pass a params
    tuple; the selector SQL is executed with one. A lone percent in either
    raises client-side before the query is sent (psycopg2 trap, 5th instance
    would be this)."""
    for name in br.NEGATIVE_LESSON_CORPORA:
        for key, val in br.CORPORA[name].items():
            assert "%" not in str(val), (name, key)
    for sql_name in ("_ACTION_CLASS_RATE_SQL", "_ACTION_CLASS_SAMPLES_SQL",
                     "_FIX_OUTCOME_SAMPLES_SQL"):
        sql = getattr(ws, sql_name)
        assert not _BARE_PCT.search(sql), sql_name
    # binding emulation: the placeholders consume exactly the tuple the caller passes
    ws._ACTION_CLASS_RATE_SQL % ("facility_dedup_apply", ws.WORK_WINDOW_DAYS)
    ws._ACTION_CLASS_SAMPLES_SQL % (ws.WORK_WINDOW_DAYS,)
    ws._FIX_OUTCOME_SAMPLES_SQL % (ws.WORK_WINDOW_DAYS,)


def test_every_from_site_reads_the_real_table_with_the_gate(monkeypatch):
    """A name-keyed corpus must never be queried as `FROM claim_lessons`. Each
    of the four registry consumers is driven against the scripted cursor and
    the SQL it EMITS is checked — the table indirection and the gate reach
    the executed statement, not just the dict."""
    monkeypatch.setattr(br, "_FRESH_COL_CACHE", {})
    conn = _Conn(script=[("information_schema.columns", [])])
    with conn.cursor() as cur:
        br._pending(cur, 400)
    pend = [s for s, _ in conn.executed if "e.source_table='claim_lessons'" in s]
    assert len(pend) == 1
    assert "FROM brain_predictions_log t" in pend[0]
    assert "t.outcome IN ('refuted','retracted')" in pend[0]
    assert "SELECT 'claim_lessons'" in pend[0]          # source_table stored = the NAME
    prop = [s for s, _ in conn.executed if "e.source_table='proposal_lessons'" in s]
    assert len(prop) == 1 and "FROM brain_enhancement_proposals t" in prop[0]
    assert "t.status IN ('duplicate','rejected')" in prop[0]

    for fn in (br._corpus_total, br._count_orphans):
        conn = _Conn()
        with conn.cursor() as cur:
            fn(cur)
        sqls = [s for s, _ in conn.executed]
        assert any("FROM brain_predictions_log t" in s and
                   "t.outcome IN ('refuted','retracted')" in s for s in sqls), fn.__name__
        assert not any("FROM claim_lessons" in s or "FROM proposal_lessons" in s
                       for s in sqls), fn.__name__
    conn = _Conn()
    br._sweep_orphans(conn)
    sqls = [s for s, _ in conn.executed]
    assert any("FROM brain_predictions_log t" in s and
               "t.outcome IN ('refuted','retracted')" in s for s in sqls)
    assert not any("FROM claim_lessons" in s or "FROM proposal_lessons" in s for s in sqls)
    # the orphan sweep binds the corpus NAME, not the table — embeddings are keyed by name
    names = [p[0] for s, p in conn.executed if p and "DELETE" in s]
    assert "claim_lessons" in names and "proposal_lessons" in names
    assert "brain_predictions_log" not in names


def test_public_search_cannot_reach_negative_corpora(monkeypatch):
    """Behavioural twin of the membership test: the keyless search refuses
    the corpus by name and never passes it to retrieval by default."""
    app = _app()
    path = _rule(app, "brain_rag.public_search")
    captured = {}

    def fake_retrieve(q, k=8, corpus=None):
        captured["corpus"] = corpus
        return []
    monkeypatch.setattr(br, "retrieve_context", fake_retrieve)
    monkeypatch.setattr(br, "_search_caller_keyed", lambda: True)
    stub = types.ModuleType("routes.agentic_master_shell")
    stub.capture_query_miss = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "routes.agentic_master_shell", stub)
    with app.test_client() as c:
        assert c.get(f"{path}?q=deals&corpus=claim_lessons").status_code == 400
        assert c.get(f"{path}?q=deals&corpus=proposal_lessons").status_code == 400
        r = c.get(f"{path}?q=deals&corpus=claim_lessons,proposal_lessons,news_articles")
        assert r.status_code == 200
        assert captured["corpus"] == ["news_articles"]
        r = c.get(f"{path}?q=deals")
        assert r.status_code == 200
        assert not set(captured["corpus"]) & set(br.NEGATIVE_LESSON_CORPORA)
        assert set(captured["corpus"]) == set(br.PUBLIC_CORPORA)


def test_reindex_tolerates_an_empty_or_missing_negative_corpus(monkeypatch):
    """A corpus whose table is missing/empty must cost its own rows only:
    reindex answers 200 ok, the other corpora are still swept, nothing 5xxs."""
    monkeypatch.setattr(br, "_admin_ok", lambda: True)
    monkeypatch.setattr(br, "_ensure", lambda: True)
    monkeypatch.setattr(br, "_embed", lambda texts, input_type=None: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(br, "_beat_feed", lambda *a, **k: None)
    monkeypatch.setattr(br, "_reindex_chunk_docs", lambda c, cap: 0)
    monkeypatch.setattr(br, "_pending_chunk_count", lambda cur: 0)
    monkeypatch.setattr(br, "_FRESH_COL_CACHE", {})

    class _Missing(Exception):
        pass
    conn = _Conn(script=[
        ("FROM brain_predictions_log t", _Missing('relation "brain_predictions_log" does not exist')),
        ("FROM brain_corpus_embeddings", [(0,)]),
    ])
    monkeypatch.setattr(br, "_db", lambda: conn)
    app = _app()
    with app.test_client() as c:
        r = c.post(_rule(app, "brain_rag.reindex") + "?cap=100")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["embedded"] == 0 and body["done"] is True
    sqls = [s for s, _ in conn.executed]
    assert any("FROM brain_predictions_log t" in s for s in sqls)        # attempted
    assert any("FROM brain_enhancement_proposals t" in s for s in sqls)   # still ran
    assert any("DELETE FROM brain_corpus_embeddings" in s and
               "FROM brain_enhancement_proposals t" in s for s in sqls)   # sweep still ran


# ── 2 · recall ────────────────────────────────────────────────────────
def _mixed_rows():
    return [
        {"source_table": "autopilot_outcomes", "source_id": "1", "kind": "lesson",
         "text": "Action linkedin_post: WORKED — impressions up", "score": 0.9, "cosine": 0.9},
        {"source_table": "claim_lessons", "source_id": "100945", "kind": "claim_lesson",
         "text": "REFUTED: " + SEEDED_STATEMENT, "score": 0.88, "cosine": 0.88},
        {"source_table": "autopilot_outcomes", "source_id": "2", "kind": "lesson",
         "text": "Action sitemap_ping: FAILED — 403 from Bing", "score": 0.87, "cosine": 0.87},
        {"source_table": "claim_lessons", "source_id": "100946", "kind": "claim_lesson",
         "text": "REFUTED: " + SEEDED_STATEMENT, "score": 0.86, "cosine": 0.86},   # dup text
        {"source_table": "proposal_lessons", "source_id": "7", "kind": "proposal_lesson",
         "text": "REJECTED PROPOSAL (duplicate): [reliability] x", "score": 0.85, "cosine": 0.85},
        {"source_table": "brain_finding_outcomes", "source_id": "9", "kind": "lesson",
         "text": "Issue a: auto_pr fix b → success. held", "score": 0.84, "cosine": 0.84},
        {"source_table": "brain_finding_outcomes", "source_id": "10", "kind": "lesson",
         "text": "Issue z: manual fix q → failed. did not hold", "score": 0.83, "cosine": 0.83},
    ]


def test_recall_negative_lessons_filters_dedups_and_caps(monkeypatch):
    captured = {}

    def fake_retrieve(q, k=8, corpus=None):
        captured.update(q=q, k=k, corpus=corpus)
        return _mixed_rows()
    monkeypatch.setattr(br, "retrieve_context", fake_retrieve)
    monkeypatch.delenv("LEARN_STATION_DISABLE", raising=False)
    out = br.recall_negative_lessons("deals", k=3)
    # best-first, positives dropped, the duplicate statement collapsed, capped at k
    assert [r["source_id"] for r in out] == ["100945", "2", "7"]
    assert all(r["negative"] is True for r in out)
    assert set(br.NEGATIVE_LESSON_CORPORA) <= set(captured["corpus"])
    assert captured["k"] >= 8                                 # over-fetch for the filter
    out4 = br.recall_negative_lessons("deals", k=4)
    assert [r["source_id"] for r in out4] == ["100945", "2", "7", "10"]
    # the input rows are not mutated (callers may cache retrieval results)
    assert "negative" not in _mixed_rows()[1]


def test_recall_negative_lessons_failsoft(monkeypatch):
    monkeypatch.delenv("LEARN_STATION_DISABLE", raising=False)
    assert br.recall_negative_lessons("", 4) == []
    assert br.recall_negative_lessons(None, 4) == []

    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(br, "retrieve_context", boom)
    assert br.recall_negative_lessons("deals", 4) == []
    monkeypatch.setattr(br, "retrieve_context", lambda *a, **k: [dict(SEEDED[0])])
    assert len(br.recall_negative_lessons("deals", 4)) == 1
    monkeypatch.setenv("LEARN_STATION_DISABLE", "1")
    assert br.recall_negative_lessons("deals", 4) == []


# ── 3 · the planner renders what we got wrong ─────────────────────────
def _base_ctx():
    return {"funnel": {}, "page_health": {}, "feedback": {}, "backlog": {},
            "competitors": {}, "self_model": {}, "recent_recs": [],
            "pr_outcomes": {}, "self_perception": {}}


def test_planner_prompt_renders_seeded_refuted_claim_and_omits_without(monkeypatch):
    monkeypatch.delenv("BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED", raising=False)
    assert planner._CTX_BUDGET["refuted_claims"] > 0
    with_ = planner._build_prompt(dict(_base_ctx(), refuted_claims=SEEDED))
    assert planner._WRONG_SECTION_TITLE in with_
    assert SEEDED_STATEMENT in with_
    # CONTROL: no key → no section, no statement
    without = planner._build_prompt(_base_ctx())
    assert planner._WRONG_SECTION_TITLE not in without
    assert SEEDED_STATEMENT not in without
    # CONTROL: recalled nothing → still absent
    empty = planner._build_prompt(dict(_base_ctx(), refuted_claims=[]))
    assert planner._WRONG_SECTION_TITLE not in empty


def test_planner_gathers_refuted_claims_end_to_end(monkeypatch):
    """With a seeded refuted claim the FULL gather → prompt path contains its
    statement; with nothing recalled the section is absent (control)."""
    monkeypatch.setenv("BRAIN_RAG_ENABLED", "1")
    monkeypatch.delenv("BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED", raising=False)
    monkeypatch.setattr(planner, "_http_get_json", lambda path, timeout=8: {})
    monkeypatch.setattr(planner, "_gather_competitor_context", lambda: {})
    monkeypatch.setattr(planner, "_read_recent_recs", lambda weeks_back=4: [])
    monkeypatch.setattr(planner, "_read_recidivism", lambda *a, **k: [])
    monkeypatch.setattr(planner, "_gather_outcomes_context", lambda window_days=30: {})
    for mod in ("routes.brain_self_perception", "routes.brain_code_scanner",
                "routes.brain_architecture_proposer"):
        m = types.ModuleType(mod)
        m.gather_self_perception_context = lambda window_days=14: {}
        m.gather_code_inventory_context = lambda: {}
        m.gather_recent_proposals = lambda window_days=30: []
        monkeypatch.setitem(sys.modules, mod, m)
    monkeypatch.setattr(br, "retrieve_context", lambda *a, **k: [])
    monkeypatch.setattr(br, "retrieve_lessons", lambda *a, **k: [])
    calls = {}

    def fake_neg(q, k=4):
        calls["q"], calls["k"] = q, k
        return [dict(SEEDED[0])]
    monkeypatch.setattr(br, "recall_negative_lessons", fake_neg)

    ctx = planner._gather_strategic_context()
    assert ctx["refuted_claims"][0]["text"] == SEEDED[0]["text"]
    assert calls["k"] == 4 and "DC Hub" in calls["q"]
    prompt = planner._build_prompt(ctx)
    assert planner._WRONG_SECTION_TITLE in prompt and SEEDED_STATEMENT in prompt

    monkeypatch.setattr(br, "recall_negative_lessons", lambda q, k=4: [])
    ctx2 = planner._gather_strategic_context()
    assert ctx2.get("refuted_claims") == []
    assert planner._WRONG_SECTION_TITLE not in planner._build_prompt(ctx2)


def test_prompt_key_and_title_are_wired_by_ast():
    """Comments satisfy grep; this reads the executable AST. _build_prompt
    must name the ctx key AND the title; _gather_strategic_context must assign
    ctx["refuted_claims"] from recall_negative_lessons(...)."""
    src = open(os.path.join(ROOT, "routes", "brain_strategic_planner.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    bp = fns["_build_prompt"]
    # Pin the load-bearing STATEMENT, not the vocabulary: a first draft
    # accepted any Constant "refuted_claims" inside _build_prompt, and a
    # mutation that renamed the key in the `if` survived because the dead
    # _truncate(ctx.get("refuted_claims")) in the body still carried it.
    guarded = False
    for n in ast.walk(bp):
        if not isinstance(n, ast.If):
            continue
        test_keys = {c.value for c in ast.walk(n.test) if isinstance(c, ast.Constant)}
        body_names = {x.id for stmt in n.body for x in ast.walk(stmt)
                      if isinstance(x, ast.Name)}
        if "refuted_claims" in test_keys and "_WRONG_SECTION_TITLE" in body_names:
            guarded = True
    assert guarded, ("the section must be emitted under `if ctx.get('refuted_claims')` "
                     "and name _WRONG_SECTION_TITLE in that body")
    gs = fns["_gather_strategic_context"]
    wired = False
    for n in ast.walk(gs):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Subscript)
                and isinstance(n.targets[0].slice, ast.Constant)
                and n.targets[0].slice.value == "refuted_claims"
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", "") == "recall_negative_lessons"):
            wired = True
    assert wired
    # one title, two modules
    assert planner._WRONG_SECTION_TITLE == br.PLANNER_WRONG_SECTION_TITLE
    assert planner._WRONG_SECTION_TITLE == "WHAT WE GOT WRONG (do not repeat)"


# ── 4 · the lane driver recalls it first ──────────────────────────────
def test_lane_driver_recall_puts_refuted_first(monkeypatch):
    monkeypatch.setattr(br, "recall_negative_lessons",
                        lambda q, k=2: [dict(SEEDED[0]), dict(SEEDED[0])])
    monkeypatch.setattr(br, "retrieve_lessons",
                        lambda q, k=4: [{"text": "lesson A"}, {"text": SEEDED[0]["text"]}])
    monkeypatch.setattr(br, "retrieve_context",
                        lambda q, k=3, corpus=None: [{"text": "finding F"}])
    out = lane_driver._recall("funnel", {"kpi_main": 1.0, "claims": 3})
    assert out[0]["src"] == "refuted" and SEEDED_STATEMENT in out[0]["text"]
    # duplicates collapse (the negative twin and the lesson carrying the same text)
    assert [o["src"] for o in out] == ["refuted", "lesson", "finding"]
    assert lane_driver._RECALL_CAP >= 9
    assert "[refuted]" in lane_driver._USER_TMPL
    # fail-soft: a raising negative helper never costs the lane its other recall
    monkeypatch.setattr(br, "recall_negative_lessons",
                        lambda q, k=2: (_ for _ in ()).throw(RuntimeError("x")))
    out2 = lane_driver._recall("funnel", {"claims": 3})
    assert [o["src"] for o in out2] == ["lesson", "lesson", "finding"]


def test_lane_driver_recall_is_wired_by_ast():
    src = open(os.path.join(ROOT, "routes", "brain_lane_driver.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_recall")
    assert any(isinstance(n, ast.Name) and n.id == "recall_negative_lessons" for n in ast.walk(fn))
    assert any(isinstance(n, ast.Constant) and n.value == "refuted" for n in ast.walk(fn))


# ── 5 · the effect bandit is fed ──────────────────────────────────────
def test_read_class_rate_third_source_is_the_action_class_ledger(monkeypatch):
    conn = _Conn(script=[("FROM brain_fix_outcomes", [(0, 0)]),
                         ("FROM autopilot_outcomes", [(0, 0)]),
                         ("FROM brain_action_class_runs r", [(6, 6)])])
    monkeypatch.setattr(ws, "_conn", lambda: conn)
    assert ws._read_class_rate("facility_dedup_apply") == (1.0, 6)
    assert ws.class_success_weight("facility_dedup_apply") > ws.WORK_NEUTRAL
    sql, params = next(e for e in conn.executed if "brain_action_class_runs" in e[0])
    assert params == ("facility_dedup_apply", ws.WORK_WINDOW_DAYS)
    assert "brain_predictions_log p" in sql and "'refuted'" in sql
    assert "NOT r.dry_run" in sql and "r.executed" in sql
    # precedence untouched: the primary source still answers first
    conn2 = _Conn(script=[("FROM brain_fix_outcomes", [(10, 2)]),
                          ("FROM brain_action_class_runs r", [(6, 6)])])
    monkeypatch.setattr(ws, "_conn", lambda: conn2)
    assert ws._read_class_rate("k") == (0.2, 10)
    # and a class with no outcomes anywhere stays neutral
    conn3 = _Conn(script=[("FROM brain_fix_outcomes", [(0, 0)]),
                          ("FROM autopilot_outcomes", [(0, 0)]),
                          ("FROM brain_action_class_runs r", [(0, 0)])])
    monkeypatch.setattr(ws, "_conn", lambda: conn3)
    assert ws._read_class_rate("nobody") == (None, 0)
    assert ws.class_success_weight("nobody") == ws.WORK_NEUTRAL


def test_claim_verdict_outranks_the_runs_own_read():
    """`refuted` at horizon is a failure even when the drain saw the count
    drop; `confirmed` is a success; an open/unobserved/retracted claim falls
    back to the run's verified flag. Pinned on the SQL predicates."""
    ok, failed = ws._RUN_OK, ws._RUN_FAILED
    assert "p.outcome = 'confirmed'" in ok and "AND r.verified" in ok
    assert "p.outcome = 'refuted'" in failed and "AND NOT r.verified" in failed
    assert "NOT IN ('confirmed','refuted')" in ok and "NOT IN ('confirmed','refuted')" in failed


def test_learned_outcome_weights_applies_the_floor(monkeypatch):
    conn = _Conn(script=[
        ("FROM brain_fix_outcomes", [("brain_spec_pr", 44, 36), ("brain_code_pr", 6, 6)]),
        ("FROM brain_action_class_runs r", [("facility_dedup_apply", 6, 6, 0, 2),
                                            ("news_entity_reresolve", 2, 1, 1, 0)]),
    ])
    monkeypatch.setattr(ws, "_conn", lambda: conn)
    w = ws.learned_outcome_weights()
    assert w["measured"] is True and w["non_empty"] is True
    assert set(w["learned_class_weights"]) == {"brain_spec_pr", "brain_code_pr", "facility_dedup_apply"}
    assert w["below_floor"] == {"news_entity_reresolve": 2}
    f = w["learned_class_weights"]["facility_dedup_apply"]
    assert f["samples"] == 6 and f["weight"] > ws.WORK_NEUTRAL
    assert f["verifier_judged"] == 2 and f["in_plan"] is False
    assert f["source"] == "brain_action_class_runs+claim_verifier"
    assert w["learned_class_weights"]["brain_spec_pr"]["succeeded_rate"] == round(36 / 44, 3)
    assert w["sample_counts"]["news_entity_reresolve"]["failed"] == 1
    json.dumps(w)                                             # JSON-safe
    assert set(ws.learned_outcome_weights(min_outcomes=10)["learned_class_weights"]) == {"brain_spec_pr"}
    assert ws.LEARN_MIN_OUTCOMES == 5
    # unreadable ≠ empty: an unmeasured bandit says so
    monkeypatch.setattr(ws, "_conn", lambda: None)
    u = ws.learned_outcome_weights()
    assert u["measured"] is False and u["non_empty"] is False
    assert u["error"] == "db_unavailable" and u["learned_class_weights"] == {}
    # readable but empty ledgers: measured, honestly empty
    monkeypatch.setattr(ws, "_conn", lambda: _Conn())
    e = ws.learned_outcome_weights()
    assert e["measured"] is True and e["non_empty"] is False and "error" not in e


def test_build_work_plan_surfaces_the_earned_vocabulary(monkeypatch):
    # ★Resolve the classifier through sys.modules, the way build_work_plan's
    # own `from routes.brain_mechanical_classifier import _fetch_open_proposals`
    # does. `import routes.brain_mechanical_classifier as clf` does NOT: since
    # 3.7 that form binds the PARENT-PACKAGE ATTRIBUTE (getattr(routes, ...)),
    # while `from a.b import c` reads sys.modules['a.b']. The two diverge for
    # the rest of the session once any test pops the module out of sys.modules,
    # lets it re-import (parent attr := the fresh module) and then restores
    # only sys.modules — which tests/test_brain_work_selector.py's autouse
    # fixture does. Patching the parent-attr copy then silently patched
    # NOTHING: the real fetch ran, returned ('', 'no_db_url') on a DB-less
    # runner, build_work_plan early-returned ok=False, and this test died with
    # a bare KeyError in CI while passing alone (unit-tests run 32601396524).
    fetched = []
    clf = importlib.import_module("routes.brain_mechanical_classifier")
    assert clf is sys.modules["routes.brain_mechanical_classifier"]

    def _stub_fetch(include_resolved=False, limit=200):
        fetched.append(True)
        return ([{"id": 1, "klass": "now_text_cast", "confidence": 0.9,
                  "rationale": "[MED] x", "file_path": "routes/a.py"}], "")

    monkeypatch.setattr(clf, "_fetch_open_proposals", _stub_fetch)
    monkeypatch.setattr(ws, "_read_class_rate", lambda k: (None, 0))
    monkeypatch.setattr(ws, "learned_outcome_weights", lambda *a, **k: {
        "measured": True, "floor": 5, "window_days": 45,
        "learned_class_weights": {"facility_dedup_apply": {
            "weight": 1.3, "succeeded_rate": 1.0, "samples": 6,
            "status": "boosted", "in_plan": False}},
        "sample_counts": {"facility_dedup_apply": {"settled": 6, "ok": 6, "failed": 0}},
        "below_floor": {}})
    plan = ws.build_work_plan(limit=10)
    # Prove the stub was the thing that answered before reading the result —
    # otherwise an unpatched fetch turns this test into a bare KeyError whose
    # message says nothing about WHY (the 08-22 CI failure above).
    assert fetched, "the stubbed _fetch_open_proposals was never called"
    assert plan["ok"] is True, plan.get("error")
    lcw = plan["learned_class_weights"]
    assert lcw["now_text_cast"]["in_plan"] is True
    assert lcw["facility_dedup_apply"]["in_plan"] is False
    assert lcw["facility_dedup_apply"]["weight"] == 1.3
    assert plan["earned_vocabulary"]["sample_counts"]["facility_dedup_apply"]["settled"] == 6
    assert "in_plan" in plan["learned_class_weights_basis"]
    json.dumps(plan)


# ── 6 · learn_station_status + the self-test endpoint ─────────────────
def test_learn_station_status_reads_lag_and_is_json_safe(monkeypatch):
    now = _dt.datetime.now(_dt.timezone.utc)
    old = now - _dt.timedelta(hours=9)
    newer = now - _dt.timedelta(hours=1)
    conn = _Conn(script=[
        ("max(t.outcome_at) FROM brain_predictions_log t", [(1, old)]),
        ("max(t.created_at) FROM brain_enhancement_proposals t", [(24, old)]),
        ("FROM brain_corpus_embeddings WHERE source_table = %s",
         lambda sql, p: [(1, newer)] if p[0] == "claim_lessons" else [(0, None)]),
        ("e.source_table='claim_lessons'", [(0,)]),
        ("e.source_table='proposal_lessons'", [(24,)]),
    ])
    monkeypatch.setattr(br, "_db", lambda: conn)
    monkeypatch.setattr(ws, "learned_outcome_weights",
                        lambda *a, **k: {"measured": True, "non_empty": False,
                                         "learned_class_weights": {}, "sample_counts": {}})
    monkeypatch.delenv("LEARN_STATION_DISABLE", raising=False)
    st = br.learn_station_status()
    json.dumps(st)
    assert st["ok"] is True and st["errors"] == [] and st["leak"] is False
    assert st["planner_section"] == br.PLANNER_WRONG_SECTION_TITLE
    cl = st["corpora"]["claim_lessons"]
    assert cl["registered"] and cl["lesson_corpus"] and cl["public"] is False
    assert cl["table"] == "brain_predictions_log" and cl["fresh_col"] == "outcome_at"
    assert cl["rows"] == 1 and cl["embedded"] == 1 and cl["pending"] == 0
    assert cl["embedded_within_cycle"] is True
    assert cl["newest_source_at"] == old.isoformat()
    pl = st["corpora"]["proposal_lessons"]
    assert pl["rows"] == 24 and pl["embedded"] == 0 and pl["pending"] == 24
    assert pl["embedded_within_cycle"] is False            # 9h old, one cycle is 4h
    assert pl["fresh_col"] is None and "created_at" in pl["newest_source_basis"]
    assert st["weights"]["non_empty"] is False
    assert conn.closed is True
    # the judgement itself: nothing to judge / not due yet read as None, never PASS
    assert br._within_cycle({"rows": 0}, now) is None
    assert br._within_cycle({"rows": 3, "_newest_source": newer, "_newest_embedding": None,
                             "pending": 3}, now) is None
    assert br._within_cycle({"rows": 3, "_newest_source": old, "_newest_embedding": None,
                             "pending": 3}, now) is False
    assert br._within_cycle({"rows": 3, "_newest_source": old, "_newest_embedding": newer,
                             "pending": 0}, now) is True


def test_learn_station_status_without_db_is_honest_and_json_safe(monkeypatch):
    monkeypatch.setattr(br, "_db", lambda: None)
    monkeypatch.setattr(ws, "learned_outcome_weights",
                        lambda *a, **k: {"measured": False, "non_empty": False,
                                         "learned_class_weights": {}, "sample_counts": {},
                                         "error": "db_unavailable"})
    st = br.learn_station_status()
    json.dumps(st)
    assert st["ok"] is False and "db_unavailable" in st["errors"]
    assert st["corpora"]["claim_lessons"]["registered"] is True
    assert st["corpora"]["claim_lessons"]["rows"] is None
    assert st["corpora"]["claim_lessons"]["embedded_within_cycle"] is None
    assert st["weights"]["measured"] is False
    # a raising weights helper is reported, not raised
    monkeypatch.setattr(ws, "learned_outcome_weights",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    st2 = br.learn_station_status()
    assert st2["weights"]["measured"] is False and "RuntimeError" in st2["weights"]["error"]
    json.dumps(st2)


def test_learn_recall_endpoint_gated_killable_and_reports(monkeypatch):
    app = _app()
    path = _rule(app, "brain_rag.learn_recall")
    assert path == "/api/v1/brain/learn/recall"        # /api/v1/brain/ = CF bypass
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test-65c")
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("LEARN_STATION_DISABLE", raising=False)
    monkeypatch.setattr(br, "recall_negative_lessons", lambda q, k=4: [dict(SEEDED[0])])
    monkeypatch.setattr(br, "learn_station_status",
                        lambda: {"ok": True, "corpora": {}, "weights": {"non_empty": False}})
    hdr = {"X-Admin-Key": "k-test-65c"}
    with app.test_client() as c:
        assert c.get(f"{path}?q=deals").status_code == 401
        assert c.get(f"{path}?q=deals", headers={"X-Admin-Key": "wrong"}).status_code == 401
        assert c.get(path, headers=hdr).status_code == 400
        r = c.get(f"{path}?q=deals&k=3", headers=hdr)
        assert r.status_code == 200
        b = r.get_json()
        assert b["ok"] is True and b["count"] == 1 and b["k"] == 3
        assert SEEDED_STATEMENT in b["lessons"][0]["text"]
        assert b["planner_section"] == br.PLANNER_WRONG_SECTION_TITLE
        assert b["status"]["ok"] is True
        assert r.headers["Cache-Control"] == "no-store"
        monkeypatch.setattr(br, "recall_negative_lessons", lambda q, k=4: [])
        assert c.get(f"{path}?q=deals", headers=hdr).get_json()["planner_section"] is None
        monkeypatch.setenv("LEARN_STATION_DISABLE", "1")
        assert c.get(f"{path}?q=deals", headers=hdr).status_code == 404
        # the kill switch beats auth: a keyless probe sees the same 404, never 5xx
        assert c.get(f"{path}?q=deals").status_code == 404


def test_admin_gate_reads_env_at_request_time_not_import(monkeypatch):
    """The 2026-08-15 class: a gate that snapshots the key at import disables
    itself on any process whose env lacks it. brain_rag's gate re-reads env
    per request — with NO key configured the endpoint must refuse, not open."""
    app = _app()
    path = _rule(app, "brain_rag.learn_recall")
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("LEARN_STATION_DISABLE", raising=False)
    with app.test_client() as c:
        assert c.get(f"{path}?q=deals").status_code == 401
        assert c.get(f"{path}?q=deals", headers={"X-Admin-Key": ""}).status_code == 401
