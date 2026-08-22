"""tests/test_squasher_graduation_and_queues.py — Agentic Loop #65, part B
(2026-08-22): the candidate registry in DATA, the track record, the
graduation PROPOSAL, one decision → N rows, and the queue's ages.

Every clause that keeps the mechanism safe is exercised as BEHAVIOUR: the
real functions run against a cursor stub and a loopback stub, and the
assertions are on what was called, with what, in what order. Each guard has
a CONTROL that must stay green, and the PR body names the mutation each
guard failed on.

  · seeding      every class's path / verifier / alias is a routable rule
                 (no phantom endpoints); seeds are granted=FALSE and carry
                 candidate_reason + track_record_required; re-running the
                 seed issues the same DO-NOTHING inserts and never a grant.
  · execution    an UNGRANTED class never executes — it is only PROBED
                 (control: a granted class runs, verifies, resolves); an
                 endpoint's 409 refusal is not a class failure (control: a
                 409 without `refused` still is).
  · probes       real drains only; clean = readable + 2xx + metric still;
                 a dry call that MOVED the metric trips the breaker; probes
                 are spaced by the ledger and never a budget unit.
  · graduation   the code rule, pure; the report proposes ONE row per
                 eligible class and never grants — no SQL in this module
                 sets granted except grant_post (AST).
  · resolve-class closes every waiting row of a class, runs nothing (a
                 fetch stub that raises is never reached; AST: no loopback).
  · queue_ages   per status × class, JSON-safe, UNKNOWN when unreadable.
  · wrappers     THE GRANT IS THE FIRST GATE — /actuate refuses 409 and
                 mutates nothing for a class brain_action_classes does not
                 say a human granted, and the refusal is the drain's own
                 eligible() (property-tested: fires iff eligible). A tripped
                 breaker is refused on the dry path too; the dry path stays
                 open for an ungranted class (that is how it earns its
                 record). With confirm it honours the autonomy shell's
                 budget and ledgers the rollback — and for the fire that
                 COMMITS mid-flight the run row is durable BEFORE it.
                 /rollback-run applies it in one statement and its ledger
                 row never spends the actuation budget.
  · kill switch  every new endpoint answers 404, never 5xx — and the scan
                 that says so is pinned to a non-zero floor of guards.

House rules: no DB, never import main, nothing runs at module scope.
Run:  python3 -m pytest tests/test_squasher_graduation_and_queues.py -v
"""
from __future__ import annotations

import ast
import datetime as _dt
import inspect
import json
import pathlib
import re
import textwrap
import types

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from routes import squasher_action_classes as sac
from routes import squasher_portal as sp
from routes import squasher_queue as sq
from tests.test_squasher_action_classes import (  # noqa: F401  (claim_ledger is a fixture)
    _Conn, _Cur, _app, _class_update, _cls, _cls_tuple, _queue_updates,
    _resolved_ids, _row, _row_tuple, claim_ledger)

ROOT = pathlib.Path(__file__).resolve().parent.parent
UTC = _dt.timezone.utc
NEWS = "news_entity_reresolve"
DEALS = "deals_exact_dupe_quarantine"
FAC = "facility_dedup_apply"
NEWS_ACT = "/api/v1/brain/squasher/actuate/news_entity_reresolve"
NEWS_VER = "/api/v1/brain/squasher/verifier/news_entity_reresolve"

# answer keys (substrings of the SQL each function issues)
K_CLASSES = "FROM brain_action_classes ORDER BY class"
K_PLAN = "JOIN brain_action_classes c ON"
K_RUN_INSERT = "INSERT INTO brain_action_class_runs"
K_LAST_PROBE = "WHERE class = %s AND dry_run"
K_OLDEST_ROW = "WHERE action_class = %s AND status = 'awaiting_ops'"
K_RECORD = "GROUP BY class"
K_PROPOSALS = "WHERE finding_key = ANY(%s)"
K_LOOKUP = "WHERE finding_key = %s AND status IN"
K_Q_INSERT = "INSERT INTO squasher_work_queue"
K_REFRESH = "SET seen_count = COALESCE(seen_count, 1) + 1"
K_AGES = "GROUP BY 1, 2 ORDER BY 1, 2"
K_ACT_RUN = "FROM brain_actuator_runs WHERE id = %s"
K_PRE_IMAGE = "WHERE in_facilities = FALSE"
K_POST_IMAGE = "AND in_facilities = TRUE"
K_CLASS_ROW = "FROM brain_action_classes WHERE class = %s"
K_ACT_INSERT = "INSERT INTO brain_actuator_runs"
K_ACT_UPDATE = "UPDATE brain_actuator_runs SET rows_affected"


# ── stubs ─────────────────────────────────────────────────────────────────

def _metric_for(path: str) -> str:
    if "/verifier/" in path:
        cls = path.split("/verifier/", 1)[1].split("?", 1)[0]
        return sac.ACTION_CLASSES[cls]["metric"]
    return "duplicate_rows"


class _Fetch2:
    """Loopback stub that serves BOTH class shapes by URL. A verifier GET
    answers the class's metric from a per-metric reading queue (None =
    unreadable); a POST without confirm=1 is answered as a dry run; a
    confirmed POST answers the scripted status/body."""

    def __init__(self, readings=None, post_status=200, post_body=None):
        self.readings = {k: list(v) for k, v in (readings or {}).items()}
        self.post_status, self.post_body = post_status, post_body
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        if method == "GET":
            m = _metric_for(path)
            q = self.readings.get(m) or []
            v = q.pop(0) if q else None
            if v is None:
                return 500, {"ok": False, "error": "db_unavailable"}
            return 200, {"ok": True, m: v}
        if "confirm=1" not in path:
            return 200, {"ok": True, "dry_run": True}
        body = self.post_body if self.post_body is not None else {
            "ok": True, "rows_affected": 2, "actuator_run_id": 42}
        return self.post_status, body

    @property
    def confirmed_posts(self):
        return [c for c in self.calls if c[0] == "POST" and "confirm=1" in c[1]]

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def _news_cls(**kw):
    d = _cls(**{"class": NEWS, "granted": False, "granted_by": None,
                "verifier_url": NEWS_VER, "bound_params": {"confirm": "1"},
                "reversible": True})
    d.update(kw)
    return d


def _deals_cls(**kw):
    d = _cls(**{"class": DEALS, "granted": False, "granted_by": None,
                "verifier_url": "/api/v1/brain/squasher/verifier/" + DEALS,
                "bound_params": {"confirm": "1"}, "reversible": True})
    d.update(kw)
    return d


def _registry(cls=NEWS, **kw):
    """The brain_action_classes row actuate() must READ before it may fire —
    granted by default, because a grant is the precondition, not a detail."""
    mk = {NEWS: _news_cls, DEALS: _deals_cls}[cls]
    row = mk(**{"granted": True, "granted_by": "op", **kw})
    return row, {K_CLASS_ROW: [_cls_tuple(row)]}


def _granted(cls=NEWS, **kw):
    return _registry(cls, **kw)[1]


def _news_row(**kw):
    d = _row(id=300, finding_key="graph-spine:es_blindspot",
             title="resolver blind spot", action_class=NEWS,
             action_url=NEWS_ACT + "?confirm=1", action_method="POST")
    d.update(kw)
    return d


def _harness2(monkeypatch, *, classes, rows=(), day_used=0, enabled="1",
              extra=None):
    """A drain step wired to stubs for ANY mix of classes. -> (conn, cur)."""
    classes = list(classes)
    cur = _Cur({
        K_CLASSES: [_cls_tuple(c) for c in classes],
        "FROM brain_action_classes WHERE class = %s": [_cls_tuple(classes[0])],
        "WHERE executed AND NOT dry_run": [(day_used,)],
        "WHERE verified AND NOT dry_run": [(0,)],
        K_PLAN: [_row_tuple(r) for r in rows],
        K_RUN_INSERT: [(501,)],
        "action_class IS NULL ORDER BY id DESC": [],
        **(extra or {}),
    })
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    if enabled is None:
        monkeypatch.delenv("ACTION_CLASSES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ACTION_CLASSES_ENABLED", enabled)
    for k in ("ACTION_CLASS_MAX_PER_DRAIN", "ACTION_CLASS_MAX_PER_DAY",
              "SQUASHER_QUEUE_DISABLE", "BRAIN_ACTUATORS_DISABLE"):
        monkeypatch.delenv(k, raising=False)
    return conn, cur


def _sqls(cur, prefix):
    return [(s, p) for s, p in cur.calls if s.startswith(prefix)]


def _run_inserts(cur):
    return [p for s, p in cur.calls if s.startswith(K_RUN_INSERT)]


def _run_finishes(cur):
    return [p for s, p in cur.calls
            if s.startswith("UPDATE brain_action_class_runs SET post_count")]


def _never(*a, **k):
    raise AssertionError("an action path was called: %r %r" % (a, k))


def _fake_autonomy(monkeypatch, *, trigger=7, fire=None, budget=(True, True),
                   fire_result=None):
    """Stand-in for routes.brain_autonomy_master_shell: the wrapper must use
    the actuator's OWN trigger/fire and the shell's OWN budget read."""
    calls = {"fire": [], "budget": [], "ensure": 0}

    def _trigger(cur):
        return trigger() if callable(trigger) else trigger

    def _fire(conn, cur, n):
        calls["fire"].append(n)
        if fire is not None:
            return fire(conn, cur, n)
        return fire_result if fire_result is not None else {
            "rows_affected": 3, "rollback": [{"id": "ext_a", "prior": None}],
            "result": {"victims": 3}, "ok": True}

    act = {"id": NEWS, "heals": "test", "trigger": _trigger, "fire": _fire}
    bam = types.SimpleNamespace(
        ACTUATORS=[act], ACTUATOR_DAILY_CAP_EACH=1, ACTUATOR_DAILY_CAP_GLOBAL=3,
        _ensure_tables=lambda conn: calls.__setitem__("ensure", calls["ensure"] + 1),
        _budget_ok=lambda cur, aid: (calls["budget"].append(aid) or budget))

    def _for(cls):
        spec = sac.ACTION_CLASSES.get(cls) or {}
        if not spec.get("actuator"):
            return None, None
        act["id"] = spec["actuator"]
        return act, bam

    monkeypatch.setattr(sac, "_actuator_for", _for)
    return calls


# ══════════════════════════════════════════════════════════════════════════
#  1 · the candidate registry: real endpoints, seeded granted=FALSE
# ══════════════════════════════════════════════════════════════════════════

def _full_app():
    import flask
    from routes.facility_dedup import facility_dedup_bp
    from routes.news_entity_extraction import news_ner_bp
    app = flask.Flask("t65")
    app.register_blueprint(sq.squasher_queue_bp)   # record_once wires sac
    app.register_blueprint(facility_dedup_bp)
    app.register_blueprint(news_ner_bp)
    return app


def _routable(app, path, method):
    try:
        app.url_map.bind("localhost").match(path, method=method)
        return True
    except (NotFound, MethodNotAllowed):
        return False


def test_every_class_names_only_endpoints_that_exist():
    """★ No phantom endpoints: the action path, the verifier and every
    classification alias of every class must be a routable Flask rule with
    the right verb — on the blueprints that ship today."""
    app = _full_app()
    for name, spec in sac.ACTION_CLASSES.items():
        assert _routable(app, spec["path"], spec["method"]), (name, spec["path"])
        assert _routable(app, spec["verifier_url"], "GET"), (name, spec["verifier_url"])
        for alias in spec.get("match_paths") or ():
            assert _routable(app, alias, spec["method"]), (name, alias)


def test_the_expected_candidates_are_seeded_and_gsc_is_not():
    assert set(sac.ACTION_CLASSES) == {FAC, NEWS, DEALS}
    assert not any("gsc" in s["path"] for s in sac.ACTION_CLASSES.values()), (
        "a GSC-proven refresh class may only be added once POST .../gsc/proven/"
        "refresh and a count verifier exist — they do not on 2026-08-22")


def test_the_wrapped_classes_point_at_registered_autonomy_actuators():
    from routes import brain_autonomy_master_shell as bam
    ids = {a["id"] for a in bam.ACTUATORS}
    for name in (NEWS, DEALS):
        spec = sac.ACTION_CLASSES[name]
        assert spec["actuator"] in ids, name
        a, mod = sac._actuator_for(name)
        assert a is not None and callable(a["trigger"]) and callable(a["fire"])
        assert mod is bam
    assert sac._actuator_for(FAC) == (None, None)


def test_the_news_pre_image_cap_equals_the_actuators_own_cap():
    """The pre-image must scan exactly the rows the fire scans, or the
    rollback misses flips. Pinned to the literal the actuator passes."""
    from routes import brain_autonomy_master_shell as bam
    src = inspect.getsource(bam._fire_entity_reresolve)
    m = re.search(r"cap\s*=\s*(\d+)", src)
    assert m, "the actuator no longer passes a literal cap — re-derive the pre-image"
    assert int(m.group(1)) == sac._NEWS_PRE_IMAGE_CAP


def _select_clause(fn):
    """The row-selection half (WHERE … ORDER BY … LIMIT) of the single SELECT
    a function issues, off the AST so line breaks and comments cannot matter."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "SELECT" in n.value and "news_discovered_entities" in n.value]
    assert len(lits) == 1, f"{fn.__name__}: expected one SELECT, found {len(lits)}"
    s = " ".join(lits[0].split())
    assert " WHERE " in s, s
    return s.split(" WHERE ", 1)[1]


def test_the_news_pre_image_scans_the_SAME_row_SET_the_actuator_will():
    """★ The cap alone is not enough. Change _reresolve_unmatched's WHERE or
    its ORDER BY and the pre-image silently covers a DIFFERENT 300 rows —
    rows flipped outside it get no rollback entry, and 'reversible' (the
    thing that made this class eligible) quietly stops being true. Both
    queries must agree clause for clause, tiebreaker included: without an id
    tiebreaker a scan pass that stamps many rows with one last_seen_at makes
    the two windows differ on ties even when the text matches."""
    from routes.news_entity_extraction import _reresolve_unmatched
    mine, theirs = _select_clause(sac._pre_image), _select_clause(_reresolve_unmatched)
    assert mine == theirs, f"pre-image: {mine!r}\nactuator:  {theirs!r}"
    assert re.search(r"ORDER BY .*, id (ASC|DESC)\b", mine), \
        f"no id tiebreaker — the two windows can differ on ties: {mine!r}"


def test_seeds_carry_granted_FALSE_a_reason_and_a_record_for_every_class():
    cur = _Cur()
    sac.ensure_tables(cur)
    seeds = _sqls(cur, "INSERT INTO brain_action_classes")
    assert [p[0] for _, p in seeds] == list(sac.ACTION_CLASSES)
    for sql, p in seeds:
        assert ", FALSE," in sql and "ON CONFLICT (class) DO NOTHING" in sql
        assert "candidate_reason, track_record_required" in sql
        assert p[5] and len(p[5]) > 40, "candidate_reason is the WHY, in data"
        req = json.loads(p[6])
        assert req["clean_dry_runs"] >= 1 and req["max_consecutive_failed"] == 0


def test_seeding_twice_issues_the_same_do_nothing_inserts_and_never_a_grant():
    """Idempotent by construction: the second run is byte-identical, the
    ensure path issues no UPDATE at all (it runs on every GET and dry run),
    and nothing in it writes granted."""
    cur = _Cur()
    sac.ensure_tables(cur)
    first = list(cur.calls)
    sac.ensure_tables(cur)
    assert cur.calls[len(first):] == first
    assert not [s for s, _ in cur.calls if "SET granted" in s]
    assert not [s for s, _ in cur.calls if s.startswith("UPDATE")]


def test_a_real_drain_backfills_the_pre_65_row_once_and_a_dry_run_never(monkeypatch, claim_ledger):
    """The facility row was seeded before these columns existed. A REAL
    drain fills candidate_reason / track_record_required from the registry,
    guarded on NULL so it is a no-op afterwards; it touches nothing else."""
    conn, cur = _harness2(monkeypatch, classes=[_cls(granted=True)], rows=[])
    out = sac.run_granted_actions(fetch=_Fetch2())
    backfills = _sqls(cur, "UPDATE brain_action_classes SET candidate_reason")
    assert len(backfills) == len(sac.ACTION_CLASSES)
    assert out["candidate_columns_backfilled"] == len(sac.ACTION_CLASSES)
    for sql, p in backfills:
        assert "WHERE class = %s AND candidate_reason IS NULL" in sql
        assert "granted" not in sql and "notes" not in sql
        assert p[0] == sac.ACTION_CLASSES[p[2]]["candidate_reason"]
    conn, cur = _harness2(monkeypatch, classes=[_cls(granted=True)], rows=[])
    sac.run_granted_actions(dry_run=True, fetch=_Fetch2())
    assert _sqls(cur, "UPDATE") == [], "a dry run is read-only"


def test_ensure_adds_the_registry_and_ledger_columns():
    cur = _Cur()
    sac.ensure_tables(cur)
    ddl = " ".join(s for s, _ in cur.calls)
    assert "ADD COLUMN IF NOT EXISTS candidate_reason TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS track_record_required JSONB" in ddl
    assert "ADD COLUMN IF NOT EXISTS actuator_run_id BIGINT" in ddl


def test_class_rows_parse_the_record_and_the_default_wins_over_garbage():
    d = sac._row_dict(sac._CLASS_COLS, _cls_tuple(_news_cls(
        track_record_required='{"clean_dry_runs": 5, "max_consecutive_failed": "x"}')))
    assert d["track_record_required"] == {"clean_dry_runs": 5, "max_consecutive_failed": "x"}
    req = sac.track_record_required_of(d, sac.ACTION_CLASSES[NEWS])
    assert req == {"clean_dry_runs": 5, "max_consecutive_failed": 0}
    assert sac.track_record_required_of(None, None) == sac._TRACK_RECORD_DEFAULT


# ══════════════════════════════════════════════════════════════════════════
#  2 · class-scoped classification and the dry-run URL contract
# ══════════════════════════════════════════════════════════════════════════

def test_a_finding_naming_the_bare_reresolve_endpoint_classifies_to_the_wrapped_class():
    c = sac.classify_text("operator action required: POST /api/v1/admin/news-ner/"
                          "re-resolve?cap=500 — close the resolver lag")
    assert c["action_class"] == NEWS and c["params"] == {}
    assert c["action_url"] == NEWS_ACT + "?confirm=1", (
        "the registry decides what RUNS — the wrapper, never the bare endpoint")
    assert c["action_method"] == "POST"


def test_the_verb_still_has_to_match_for_a_class_scoped_class():
    assert sac.classify_text("GET /api/v1/admin/news-ner/re-resolve") is None


def test_class_scoped_row_params_are_empty_not_none():
    assert sac.row_params_of(_news_row()) == {}
    assert sac.row_params_of(_news_row(action_class="nope")) is None


def test_urls_have_no_dangling_query_and_the_dry_run_url_drops_the_bound_params():
    assert sac.build_verifier_url(NEWS, {}) == NEWS_VER
    assert sac.build_dry_run_url(NEWS, {}) == NEWS_ACT
    assert sac.build_action_url(NEWS, {}) == NEWS_ACT + "?confirm=1"
    assert sac.build_dry_run_url(FAC, {"country": "FR"}) == "/api/v1/admin/facility-dedup/apply?country=FR"
    for name, spec in sac.ACTION_CLASSES.items():
        dry = sac.build_dry_run_url(name, {spec["row_param"]: "FR"} if spec["row_param"] else {})
        for k in spec["bound_params"]:
            assert f"{k}=" not in dry, (name, dry)


# ══════════════════════════════════════════════════════════════════════════
#  3 · an ungranted class never executes — it is only probed
# ══════════════════════════════════════════════════════════════════════════

def test_an_UNGRANTED_actuator_class_is_probed_but_never_executed(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[_news_row()])
    fetch = _Fetch2(readings={"blindspot": [5, 5, 5]})
    out = sac.run_granted_actions(fetch=fetch)
    assert out["ran"] == 0 and out["results"] == []
    assert out["candidates"][0]["skip"] == "not granted"
    assert fetch.confirmed_posts == [], "the action URL must never be called"
    assert claim_ledger["register"] == []
    assert _class_update(cur) is None and _resolved_ids(cur) == []
    # …and it WAS probed: verifier → dry call (no confirm) → verifier
    assert [c for c in fetch.calls] == [("GET", NEWS_VER), ("POST", NEWS_ACT),
                                        ("GET", NEWS_VER)]
    assert out["probes"]["probed"] == 1
    ins = _run_inserts(cur)
    assert len(ins) == 1 and ins[0][-1] is True and ins[0][6] is False, (
        "a probe is a dry_run=TRUE, executed=FALSE ledger row — never a budget unit")
    assert ins[0][7] == "probe_clean"


def test_CONTROL_a_granted_actuator_class_runs_verifies_and_resolves(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls(granted=True)],
                          rows=[_news_row()])
    fetch = _Fetch2(readings={"blindspot": [5, 0]})
    out = sac.run_granted_actions(fetch=fetch)
    res = out["results"][0]
    assert res["executed"] and res["verified"] and out["ran"] == 1
    assert fetch.confirmed_posts == [("POST", NEWS_ACT + "?confirm=1")]
    assert res["marked"] == 2, "rows_affected is read as the marked count"
    assert _resolved_ids(cur) == [300]
    assert claim_ledger["register"][0]["subject"] == NEWS, "no dangling ':' for a class-scoped claim"
    fin = _run_finishes(cur)[-1]
    assert fin[-2] == 42, "the autonomy ledger row (where the rollback lives) is linked"
    assert out["probes"]["probed"] == 0 and out["probes"]["results"] == []


def test_a_409_refusal_from_the_endpoint_is_NOT_a_class_failure(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls(granted=True)],
                          rows=[_news_row()])
    fetch = _Fetch2(readings={"blindspot": [5, 5]}, post_status=409,
                    post_body={"ok": False, "refused": "actuator budget spent"})
    out = sac.run_granted_actions(fetch=fetch)
    res = out["results"][0]
    assert res["outcome"] == "refused_by_endpoint" and res["executed"] is False
    assert out["ran"] == 0, "a refusal is not a budget unit"
    assert _class_update(cur) is None, "counters untouched — the breaker must not count refusals"
    fin = _run_finishes(cur)[-1]
    assert fin[2] == "refused_by_endpoint" and fin[-3] is False, "ledger row demoted to executed=FALSE"
    assert _resolved_ids(cur) == []


def test_CONTROL_a_409_without_refused_is_still_a_failed_http(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls(granted=True)],
                          rows=[_news_row()])
    fetch = _Fetch2(readings={"blindspot": [5, 5]}, post_status=409,
                    post_body={"ok": False})
    res = sac.run_granted_actions(fetch=fetch)["results"][0]
    assert res["outcome"] == "failed_http" and res["executed"]
    assert _class_update(cur)[1] == 1


# ══════════════════════════════════════════════════════════════════════════
#  4 · probes: the track record, earned by real drains only
# ══════════════════════════════════════════════════════════════════════════

def test_a_dry_run_report_never_probes(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    fetch = _Fetch2(readings={"blindspot": [5, 5, 5]})
    out = sac.run_granted_actions(dry_run=True, fetch=fetch)
    assert "probes" not in out and fetch.calls == []
    assert _run_inserts(cur) == []


def test_a_probe_that_MOVES_the_metric_trips_the_breaker(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    fetch = _Fetch2(readings={"blindspot": [5, 3]})
    out = sac.run_granted_actions(fetch=fetch)
    res = out["probes"]["results"][0]
    assert res["outcome"] == "probe_MUTATED" and res["clean"] is False
    assert res["breaker_tripped"] is True
    trips = _sqls(cur, "UPDATE brain_action_classes SET breaker_tripped = TRUE")
    assert len(trips) == 1 and trips[0][1] == (NEWS,)
    assert _run_finishes(cur)[-1][1] is False


def test_a_probe_whose_metric_ROSE_is_never_labelled_clean(monkeypatch, claim_ledger):
    """The breaker trips on a DROP (only a drop is attributable to the dry
    call — ingestion grows the defect between the two reads). A RISE is still
    not a clean reading, and the outcome column must not say it was."""
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    res = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [3, 5]}))["probes"]["results"][0]
    assert res["outcome"] == "probe_metric_ROSE" and res["clean"] is False
    assert res["breaker_tripped"] is False
    assert _sqls(cur, "UPDATE brain_action_classes SET breaker_tripped") == []
    assert _run_finishes(cur)[-1][1] is False


def test_CONTROL_a_probe_whose_metric_holds_is_clean_and_trips_nothing(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [5, 5]}))
    res = out["probes"]["results"][0]
    assert res["outcome"] == "probe_clean" and res["clean"] is True
    assert _sqls(cur, "UPDATE brain_action_classes SET breaker_tripped") == []
    assert _run_finishes(cur)[-1][1] is True


@pytest.mark.parametrize("readings,outcome", [
    ({"blindspot": [None]}, "probe_verifier_unreadable"),
    ({"blindspot": [5, None]}, "probe_post_read_unreadable"),
])
def test_an_unreadable_probe_is_recorded_as_unreadable_never_clean(monkeypatch, claim_ledger, readings, outcome):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    res = sac.run_granted_actions(fetch=_Fetch2(readings=readings))["probes"]["results"][0]
    assert res["outcome"] == outcome and res["clean"] is False


def test_a_non_2xx_dry_call_is_not_clean(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    fetch = _Fetch2(readings={"blindspot": [5, 5]})
    real = fetch.__call__

    def _call(method, path):
        if method == "POST":
            fetch.calls.append((method, path))
            return 401, {"ok": False, "error": "admin key required"}
        return real(method, path)
    res = sac.run_granted_actions(fetch=_call)["probes"]["results"][0]
    assert res["outcome"] == "probe_http_401" and res["clean"] is False


def test_probes_are_spaced_by_the_ledger(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[],
                          extra={K_LAST_PROBE: [(2.0,)]})
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [5, 5]}))
    assert out["probes"]["probed"] == 0
    assert "probed 2.0h ago" in out["probes"]["skipped"][0]["why"]
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[],
                          extra={K_LAST_PROBE: [(7.5,)]})
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [5, 5]}))
    assert out["probes"]["probed"] == 1, "control: older than the spacing → probed"


def test_probes_skip_a_tripped_class_and_a_granted_class(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls(breaker_tripped=True),
                                                _deals_cls(granted=True)], rows=[])
    fetch = _Fetch2(readings={"blindspot": [5, 5], "excess": [9, 9]})
    out = sac.run_granted_actions(fetch=fetch)
    assert out["probes"]["probed"] == 0 and fetch.calls == []
    assert out["probes"]["skipped"] == [{"class": NEWS, "why": "breaker tripped"}]


def test_probes_are_capped_per_drain(monkeypatch, claim_ledger):
    monkeypatch.setattr(sac, "_PROBES_PER_DRAIN", 1)
    conn, cur = _harness2(monkeypatch, classes=[_deals_cls(), _news_cls()], rows=[])
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [5, 5], "excess": [9, 9]}))
    assert out["probes"]["probed"] == 1
    assert out["probes"]["skipped"][0]["why"] == "probes-per-drain cap"


def test_a_per_row_class_is_probed_with_its_oldest_open_row_or_skipped(monkeypatch, claim_ledger):
    fac = _cls(granted=False)
    fr = _row()
    conn, cur = _harness2(monkeypatch, classes=[fac], rows=[],
                          extra={K_OLDEST_ROW: [_row_tuple(fr)]})
    fetch = _Fetch2(readings={"duplicate_rows": [4, 4]})
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [("GET", "/api/v1/admin/facility-dedup/analyze?country=FR"),
                           ("POST", "/api/v1/admin/facility-dedup/apply?country=FR"),
                           ("GET", "/api/v1/admin/facility-dedup/analyze?country=FR")]
    assert out["probes"]["results"][0]["outcome"] == "probe_clean"
    assert _run_inserts(cur)[0][1] == 218, "the probe is ledgered against the row it used"
    conn, cur = _harness2(monkeypatch, classes=[fac], rows=[])
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"duplicate_rows": [4, 4]}))
    assert out["probes"]["probed"] == 0
    assert "no open awaiting_ops row" in out["probes"]["skipped"][0]["why"]


def test_the_probe_step_runs_after_the_granted_plan_and_inside_the_wall_budget(monkeypatch, claim_ledger):
    conn, cur = _harness2(monkeypatch, classes=[_news_cls()], rows=[])
    t = [0.0]

    def clock():
        t[0] += 30.0          # every tick burns more than the budget
        return t[0]
    out = sac.run_granted_actions(fetch=_Fetch2(readings={"blindspot": [5, 5]}), clock=clock)
    assert out["probes"]["probed"] == 0
    assert out["probes"]["skipped"][0]["why"] == "wall budget spent"


# ══════════════════════════════════════════════════════════════════════════
#  5 · graduation: the code rule, the proposal, never a grant
# ══════════════════════════════════════════════════════════════════════════

def _rec(clean=3, reads=4, ok=0, failed=0):
    return {"dry_run_reads_7d": reads, "clean_dry_runs_7d": clean,
            "runs_ok_7d": ok, "runs_failed_7d": failed, "last_dry_run_at": None}


@pytest.mark.parametrize("row,record,eligible,needle", [
    (_news_cls(), _rec(), True, None),
    (_news_cls(granted=True), _rec(), False, "already granted"),
    (_news_cls(breaker_tripped=True), _rec(), False, "breaker tripped"),
    (_news_cls(), _rec(clean=2), False, "2/3 clean dry runs"),
    (_news_cls(), None, False, "0/3 clean dry runs"),
    (_news_cls(consecutive_failed=1), _rec(), False, "1 consecutive failed"),
    (_news_cls(verifier_url=""), _rec(), False, "no verifier_url"),
    (_news_cls(reversible=False), _rec(), False, "not marked reversible"),
    (_news_cls(bound_params={}), _rec(), False, "no bound_params"),
    (_news_cls(**{"class": "nope"}), _rec(), False, "not in the code registry"),
])
def test_eligible_for_grant_is_the_code_rule(row, record, eligible, needle):
    ok, why = sac.eligible_for_grant(row, record)
    assert ok is eligible, why
    if needle:
        assert any(needle in w for w in why), why
    else:
        assert why == []


def test_a_higher_N_on_the_data_row_is_honoured():
    row = _news_cls(track_record_required={"clean_dry_runs": 5})
    assert sac.eligible_for_grant(row, _rec(clean=4))[0] is False
    assert sac.eligible_for_grant(row, _rec(clean=5))[0] is True


def _grad_cur(classes, record_rows, proposals=(), lookup=(), extra=None):
    return _Cur({
        K_CLASSES: [_cls_tuple(c) for c in classes],
        K_RECORD: list(record_rows),
        K_PROPOSALS: list(proposals),
        K_LOOKUP: list(lookup),
        K_Q_INSERT: [(901,)],
        K_REFRESH: [(2,)],
        **(extra or {}),
    })


def test_graduation_proposes_ONE_row_for_an_eligible_class_and_grants_nothing(monkeypatch):
    monkeypatch.setattr(sac, "_loopback", _never)
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)])
    out = sac._graduation(cur, file=True)
    assert out["eligible"] == [NEWS] and out["filed"] == [901]
    e = out["classes"][0]
    assert e["eligible_for_grant"] is True and e["proposal"] == {
        "id": 901, "status": "awaiting_decision", "created": True}
    ins = _sqls(cur, K_Q_INSERT)
    assert len(ins) == 1
    p = ins[0][1]
    assert p[0] == "action-class-grant:" + NEWS and p[3] == "awaiting_decision"
    assert p[2] == "graduation" and p[7] == NEWS, "the row carries the class — resolve-class groups it"
    assert "ON CONFLICT DO NOTHING" in ins[0][0]
    assert '"granted": true' in p[5] and "/api/v1/brain/squasher/grant" in p[5], "the one-click payload"
    assert not [s for s, _ in cur.calls if "granted = " in s or "SET granted" in s]
    assert not [s for s, _ in cur.calls if s.startswith("UPDATE brain_action_classes")]


def test_CONTROL_graduation_files_nothing_for_an_ineligible_class(monkeypatch):
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 2, 0, 0, None)])
    out = sac._graduation(cur, file=True)
    assert out["eligible"] == [] and out["filed"] == []
    assert _sqls(cur, K_Q_INSERT) == []
    assert out["classes"][0]["not_eligible_because"] == ["2/3 clean dry runs in 7d"]


def test_graduation_read_mode_files_nothing_even_when_eligible():
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)])
    out = sac._graduation(cur, file=False)
    assert out["eligible"] == [NEWS] and out["filed"] == []
    assert _sqls(cur, K_Q_INSERT) == [] and _sqls(cur, "UPDATE") == []


def test_re_running_graduation_refreshes_the_open_row_and_never_duplicates_it():
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)],
                    proposals=[("action-class-grant:" + NEWS, 901, "awaiting_decision")],
                    lookup=[(901, "awaiting_decision")])
    out = sac._graduation(cur, file=True)
    assert out["filed"] == [] and out["refreshed"] == [901]
    assert _sqls(cur, K_Q_INSERT) == [], "open-row identity = class: refresh, never insert"
    assert len([s for s, _ in cur.calls if K_REFRESH in s]) == 1
    assert out["classes"][0]["proposal"] == {"id": 901, "status": "awaiting_decision", "created": False}
    # ★ the identity ITSELF, not just the branch it took: both the open-proposal
    #   sweep and the open-row lookup ask for exactly action-class-grant:<class>.
    #   A key carrying anything time-varying would look up a row that cannot
    #   exist yet, and every re-run would file another proposal.
    key = "action-class-grant:" + NEWS
    anys = [p for s, p in cur.calls if K_PROPOSALS in s]
    assert anys and list(anys[0][0]) == [key], anys
    looks = [p for s, p in cur.calls if K_LOOKUP in s]
    assert looks and tuple(looks[0]) == (key,), looks


def test_an_open_proposal_is_reported_without_filing():
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)],
                    proposals=[("action-class-grant:" + NEWS, 901, "awaiting_decision")])
    out = sac._graduation(cur, file=False)
    assert out["classes"][0]["proposal"] == {"id": 901, "status": "awaiting_decision"}


def test_filing_is_bounded_per_call():
    cur = _grad_cur([_deals_cls(), _news_cls()],
                    [(NEWS, 4, 3, 0, 0, None), (DEALS, 3, 3, 0, 0, None)])
    out = sac._graduation(cur, file=True, max_file=1)
    assert out["eligible"] == [DEALS, NEWS]
    assert len(out["filed"]) == 1 and len(_sqls(cur, K_Q_INSERT)) == 1


def test_a_granted_class_is_reported_as_granted_not_eligible():
    cur = _grad_cur([_cls(granted=True)], [(FAC, 0, 0, 6, 0, None)])
    e = sac._graduation(cur, file=True)["classes"][0]
    assert e["granted"] and e["candidate"] is False and e["eligible_for_grant"] is False
    assert "already granted" in e["not_eligible_because"]
    assert e["runs_ok_7d"] == 6 and e["candidate_reason"]


def test_graduation_report_is_json_safe_and_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sac, "_conn", _boom)
    out = sac.graduation_report()
    assert out == {"known": False, "error": "no db", "file": False}
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, _dt.datetime(2026, 8, 22, tzinfo=UTC))])
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    out = sac.graduation_report(file=False)
    assert out["known"] and json.dumps(out)
    assert out["classes"][0]["last_dry_run_at"] == "2026-08-22T00:00:00+00:00"
    assert sac.file_graduation_proposals()["filed"] == [901]


def test_the_summary_carries_graduation_under_a_savepoint(monkeypatch):
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)], extra={
        "WHERE executed AND NOT dry_run": [(0,)], "WHERE verified AND NOT dry_run": [(0,)],
        "WHERE status IN ('awaiting_ops', 'awaiting_decision')": []})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    out = sac.summary()
    assert out["known"] and out["graduation"]["known"] and out["graduation"]["eligible"] == [NEWS]
    assert _sqls(cur, K_Q_INSERT) == [], "a summary is a READ"
    sqls = [s for s, _ in cur.calls]
    assert "SAVEPOINT graduation_read" in sqls and "RELEASE SAVEPOINT graduation_read" in sqls

    def _boom(cur, **k):
        raise RuntimeError("ledger gone")
    monkeypatch.setattr(sac, "_graduation", _boom)
    cur2 = _grad_cur([_news_cls()], [], extra={
        "WHERE executed AND NOT dry_run": [(0,)], "WHERE verified AND NOT dry_run": [(0,)]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur2))
    out = sac.summary()
    assert out["known"] is True and out["graduation"]["known"] is False
    assert "ROLLBACK TO SAVEPOINT graduation_read" in [s for s, _ in cur2.calls]


_GRANT_SQL_RE = re.compile(r"SET\s+granted|granted\s*=\s*TRUE", re.I)


def _prose_constant_ids(fn):
    """String constants that are a statement by themselves — docstrings and
    the module's own prose. They are NOT executable text: nothing passes them
    to a cursor, so a promise ABOUT granting must not read as a grant. Every
    other string constant (an execute() argument, a name in a dict, an f-string
    piece) still counts."""
    out = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)):
            out.add(id(n.value))
    return out


_HOLE = "\uffff"   # a piece the scanner cannot read statically


def _rendered_strings(fn, prose):
    """Every string this function can PRODUCE, not merely every literal it
    contains — concatenation and f-strings are flattened first, with the
    unreadable pieces replaced by a hole. Scanning literals one at a time is
    how `"UPDATE … SET " + "granted" + " = TRUE"` walks straight past."""
    def render(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return "" if id(n) in prose else n.value
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            left, right = render(n.left), render(n.right)
            if left is None or right is None:
                return None
            return left + right
        if isinstance(n, ast.JoinedStr):
            parts = [render(v) for v in n.values]
            return "".join(_HOLE if p is None else p for p in parts)
        if isinstance(n, ast.FormattedValue):
            return None
        return None

    out = []
    for n in ast.walk(fn):
        if isinstance(n, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            s = render(n)
            if s:
                out.append(s)
    return out


def _funcs_matching(path, rx):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = set()
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        prose = _prose_constant_ids(fn)
        if any(rx.search(s) for s in _rendered_strings(fn, prose)):
            hits.add(fn.name)
    return hits


def _funcs_with_grant_sql(path):
    return _funcs_matching(path, _GRANT_SQL_RE)


# Every function allowed to write to the registry AT ALL, and what it writes.
# A grant assembled from pieces the scanner cannot read still has to name the
# table in a literal to reach it — so this arm catches what the SET arm can't.
_WRITE_TABLE_RE = re.compile(r"UPDATE\s+brain_action_classes", re.I)
_MAY_WRITE_THE_REGISTRY = {
    "grant_post",                  # the human's endpoint — the ONLY grant
    "probe_one",                   # breaker_tripped = TRUE
    "_update_class",               # run counters
    "backfill_candidate_columns",  # candidate_reason / track_record_required
}


def test_nothing_but_grant_post_can_write_a_grant():
    """★ By construction: the only function in the module whose SQL sets
    granted is the human's endpoint. graduation / probes / actuate cannot."""
    path = ROOT / "routes" / "squasher_action_classes.py"
    assert _funcs_with_grant_sql(path) == {"grant_post"}
    writers = _funcs_matching(path, _WRITE_TABLE_RE)
    assert writers == _MAY_WRITE_THE_REGISTRY, (
        "a function outside the allow-list now writes brain_action_classes: "
        f"{writers ^ _MAY_WRITE_THE_REGISTRY}")


@pytest.mark.parametrize("stmt,caught_by", [
    # the plain literal
    ('cur.execute("UPDATE brain_action_classes SET granted = TRUE")', "both"),
    # ★ the mutation the per-literal scanner survived: assembled from pieces
    ('cur.execute("UPDATE brain_action_classes SET " + "granted" + " = TRUE")', "both"),
    # the column name in a variable — unreadable SET, readable table
    ('col = "granted"\n    cur.execute(f"UPDATE brain_action_classes SET {col} = TRUE")',
     "table"),
])
def test_CONTROL_the_grant_scanner_still_sees_a_grant_hidden_in_any_function(
        tmp_path, stmt, caught_by):
    """The control for the exclusion above: prose is skipped, an execute() is
    not — however the statement is spelled."""
    src = (ROOT / "routes" / "squasher_action_classes.py").read_text(encoding="utf-8")
    planted = src.replace(
        "def file_graduation_proposals(",
        "def _planted_grant(cur):\n    " + stmt + "\n\n\n"
        "def file_graduation_proposals(", 1)
    assert planted != src, "anchor moved — the control never planted anything"
    tmp = tmp_path / "planted.py"
    tmp.write_text(planted, encoding="utf-8")
    if caught_by == "both":
        assert "_planted_grant" in _funcs_with_grant_sql(tmp)
    assert "_planted_grant" in _funcs_matching(tmp, _WRITE_TABLE_RE)


def test_CONTROL_the_grant_scanner_still_skips_prose():
    """The exclusion it exists for: a docstring that PROMISES never to set
    granted must not read as a grant."""
    fn = ast.parse('def f():\n    """never sets granted = TRUE"""\n    return 1').body[0]
    assert _rendered_strings(fn, _prose_constant_ids(fn)) == []


# ══════════════════════════════════════════════════════════════════════════
#  6 · resolve-class: one decision → N rows, and it runs NOTHING
# ══════════════════════════════════════════════════════════════════════════

def _q_cur(returning=(), extra=None):
    return _Cur({"RETURNING id, finding_key": list(returning), **(extra or {})})


def test_resolve_class_closes_every_waiting_row_of_the_class_and_runs_nothing(monkeypatch):
    cur = _q_cur([(241, "k:FR"), (256, "k:FR2"), (252, "k:CA")])
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    monkeypatch.setattr(sac, "_loopback", _never)
    monkeypatch.setattr(sq, "_investigate", _never)
    monkeypatch.setattr(sq, "_open_pr", _never)
    out = sq.resolve_class(FAC, "granted-class handles it", "", "owner")
    assert out["ok"] and out["count"] == 3 and out["status"] == "resolved"
    assert [r["id"] for r in out["resolved"]] == [241, 256, 252]
    assert out["executed_anything"] is False
    ups = _sqls(cur, "UPDATE squasher_work_queue")
    assert len(ups) == 1
    sql, p = ups[0]
    assert "WHERE action_class = %s AND status IN %s" in sql
    assert p[0] == "resolved" and p[2] == FAC
    assert p[3] == ("awaiting_ops", "awaiting_decision"), "only rows waiting on a human"
    assert "granted-class handles it" in p[1] and "by owner" in p[1]


def test_resolve_class_rejected_closes_as_refused_and_needs_a_note(monkeypatch):
    cur = _q_cur([(241, "k")])
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    out = sq.resolve_class(FAC, "not now", "", "op", outcome="rejected")
    assert not out["ok"] and "note" in out["error"] and _sqls(cur, "UPDATE") == []
    out = sq.resolve_class(FAC, "not now", "PR #3006 covers it", "op", outcome="rejected")
    assert out["ok"] and out["status"] == "refused"
    assert _sqls(cur, "UPDATE")[0][1][0] == "refused"


def test_resolve_class_refuses_bad_input_without_touching_the_queue(monkeypatch):
    cur = _q_cur()
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    assert sq.resolve_class("nope", "x")["error"] == "unknown class"
    assert sq.resolve_class(FAC, "")["error"] == "decision required"
    assert sq.resolve_class("", "x")["error"] == "class required"
    assert "outcome" in sq.resolve_class(FAC, "x", outcome="maybe")["error"]
    assert cur.calls == []


def test_resolve_class_reports_an_unreadable_queue_never_zero_rows(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sq, "_conn", _boom)
    out = sq.resolve_class(FAC, "x")
    assert out["ok"] is False and out["db_error"] and "count" not in out


_DENY_CALLS = {"_loopback", "test_client", "_investigate", "_open_pr", "open",
               "run_granted_actions", "execute_one", "actuate", "urlopen",
               "_action_classes_step", "drain"}


def _calls_in(fn):
    names = set()
    for stmt in fn.body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                f = n.func
                names.add(getattr(f, "attr", None) or getattr(f, "id", None))
    return names


def test_resolve_class_has_no_loopback_by_construction():
    tree = ast.parse((ROOT / "routes" / "squasher_queue.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("resolve_class", "resolve_class_post", "queue_ages", "file_decision_row"):
        assert name in fns, name
        bad = _calls_in(fns[name]) & _DENY_CALLS
        assert not bad, (name, bad)


def test_resolve_class_endpoint_is_gated_and_404_on_kill(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    c = _app().test_client()
    h = {"X-Admin-Key": "adm"}
    assert c.post("/api/v1/brain/squasher/resolve-class", json={}).status_code == 401
    assert c.get("/api/v1/brain/squasher/queue-ages").status_code == 401
    cur = _q_cur([(241, "k")])
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    rv = c.post("/api/v1/brain/squasher/resolve-class", headers=h,
                json={"class": "nope", "decision": "x"})
    assert rv.status_code == 404
    rv = c.post("/api/v1/brain/squasher/resolve-class", headers=h,
                json={"class": FAC, "decision": ""})
    assert rv.status_code == 400
    rv = c.post("/api/v1/brain/squasher/resolve-class", headers=h,
                json={"class": FAC, "decision": "handled", "by": "portal"})
    assert rv.status_code == 200 and rv.get_json()["count"] == 1
    monkeypatch.setenv("SQUASHER_QUEUE_DISABLE", "1")
    assert c.post("/api/v1/brain/squasher/resolve-class", headers=h,
                  json={"class": FAC, "decision": "x"}).status_code == 404
    assert c.get("/api/v1/brain/squasher/queue-ages", headers=h).status_code == 404


# ══════════════════════════════════════════════════════════════════════════
#  7 · queue_ages
# ══════════════════════════════════════════════════════════════════════════

def test_queue_ages_groups_by_status_and_class_with_the_oldest_age(monkeypatch):
    now = _dt.datetime.now(UTC)
    h = _dt.timedelta(hours=1)
    cur = _Cur({K_AGES: [
        ("awaiting_decision", "unclassified", 23, 14, now - 50 * h, now - 49 * h, 211),
        ("awaiting_ops", FAC, 12, 7, now - 30 * h, now - 29 * h, 235),
        ("queued", "unclassified", 1, 1, now - 0.5 * h, now - 0.5 * h, 263),
    ]})
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    out = sq.queue_ages()
    assert out["known"] and json.dumps(out)
    assert out["open_rows"] == 36 and out["distinct_classes"] == 1
    assert out["classified_classes"] == [FAC] and out["unclassified_rows"] == 24
    assert out["by_status"]["awaiting_decision"] == {"count": 23, "oldest_age_hours": 50.0}
    assert out["by_class"][FAC]["oldest_age_hours"] == 30.0
    assert out["by_class"][FAC]["statuses"] == {"awaiting_ops": 12}
    assert out["oldest_awaiting_decision_hours"] == 50.0
    assert out["oldest_awaiting_ops_hours"] == 30.0
    g = out["groups"][1]
    assert g["oldest_id"] == 235 and g["oldest_waiting_hours"] == 29.0
    assert "WHERE status IN ('queued', 'running', 'awaiting_ops', 'awaiting_decision')" in cur.calls[-1][0]


def test_queue_ages_is_UNKNOWN_when_unreadable(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sq, "_conn", _boom)
    out = sq.queue_ages()
    assert out["known"] is False and "open_rows" not in out


def test_file_decision_row_never_files_queued_and_is_idempotent_on_the_open_row():
    cur = _Cur({K_LOOKUP: [], K_Q_INSERT: [(901,)]})
    r = sq.file_decision_row(cur, finding_key="action-class-grant:x", title="t",
                             reason="r", decision="d", action_class="x")
    assert r == {"ok": True, "id": 901, "status": "awaiting_decision", "created": True, "by": ""}
    sql, p = _sqls(cur, K_Q_INSERT)[0]
    assert "'queued'" not in sql and p[3] == "awaiting_decision"
    assert "finished_at, last_seen" in sql, "handed off at birth — nothing to investigate"
    cur2 = _Cur({K_LOOKUP: [(901, "awaiting_decision")], K_REFRESH: [(3,)]})
    r = sq.file_decision_row(cur2, finding_key="action-class-grant:x", title="t",
                             reason="r", decision="d2")
    assert r["created"] is False and r["refreshed"] and r["seen_count"] == 3
    assert _sqls(cur2, K_Q_INSERT) == []
    assert any("SET decision = %s" in s for s, _ in cur2.calls)


# ══════════════════════════════════════════════════════════════════════════
#  8 · the actuator wrappers
# ══════════════════════════════════════════════════════════════════════════

def test_actuate_without_confirm_is_a_dry_run_that_writes_nothing(monkeypatch):
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur(_granted())
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=False)
    assert code == 200 and body["dry_run"] and body["executed"] is False
    assert body["blindspot"] == 7 and calls["fire"] == []
    assert not [s for s, _ in cur.calls if s.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_CONTROL_the_dry_path_stays_open_for_an_UNGRANTED_class(monkeypatch):
    """The grant gates the FIRE, not the probe: an ungranted class earns its
    track record through exactly this call (probe_one's dry_run_url), so
    closing it here would make graduation unreachable forever."""
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur(_granted(granted=False))
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=False)
    assert code == 200 and body["dry_run"] and body["granted"] is False
    assert calls["fire"] == []
    assert not [s for s, _ in cur.calls if s.startswith(("INSERT", "UPDATE", "DELETE"))]


# ── the grant gate: nothing fires that a human has not granted ────────────

_UNGRANTED = [
    ("no registry row at all", None, "no registry row"),
    ("granted = FALSE", dict(granted=False), "not granted"),
    ("breaker tripped", dict(breaker_tripped=True), "breaker tripped"),
    ("not reversible", dict(reversible=False), "not marked reversible"),
    ("no verifier_url", dict(verifier_url=""), "no verifier_url"),
    ("no bound_params", dict(bound_params={}), "no bound_params"),
]


@pytest.mark.parametrize("label,over,needle", _UNGRANTED,
                         ids=[c[0] for c in _UNGRANTED])
def test_actuate_REFUSES_an_ungranted_class_even_with_confirm(
        monkeypatch, label, over, needle):
    """★★★ THE control. `POST /actuate/<cls>?confirm=1` with a valid admin
    key must refuse and mutate NOTHING unless brain_action_classes says a
    human granted the class. Every shape grant_allowed()/eligible() rejects
    is driven through the real actuate(), with the real fire wired in."""
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur({} if over is None else _granted(NEWS, **over))
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("BRAIN_ACTUATORS_DISABLE", raising=False)
    body, code = sac.actuate(NEWS, confirm=True, by="attacker")
    assert code == 409, (code, body)
    assert body["ok"] is False and body["executed"] is False
    assert needle in body["refused"], body["refused"]
    assert calls["fire"] == [], "an ungranted class FIRED"
    assert calls["budget"] == [], "it did not even reach the budget read"
    assert not [s for s, _ in cur.calls
                if s.startswith(("INSERT", "UPDATE", "DELETE"))], cur.calls
    assert conn.commits == 0


def test_actuate_refuses_a_TRIPPED_class_on_the_dry_path_too(monkeypatch):
    """A tripped breaker means THIS endpoint's own 'dry' call was observed to
    mutate. Until a human clears it the class does not run at all."""
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur(_granted(breaker_tripped=True))
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=False)
    assert code == 409 and "breaker tripped" in body["refused"]
    assert body["breaker_tripped"] is True and calls["fire"] == []


def test_an_unreadable_class_registry_refuses_rather_than_fires(monkeypatch):
    """Fail CLOSED: no grant read, no fire — never 'assume granted'."""
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur(_granted(), raise_on=K_CLASS_ROW)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 409 and "registry unreadable" in body["refused"]
    assert calls["fire"] == []


@pytest.mark.parametrize("over", [None, dict(granted=False),
                                  dict(breaker_tripped=True), dict(reversible=False),
                                  dict(verifier_url=""), dict(bound_params={}),
                                  dict(), dict(notes="whatever")])
def test_the_actuate_gate_IS_the_drains_own_eligible_rule(monkeypatch, over):
    """★ ONE copy of the rule. For every registry shape, actuate() fires if
    and ONLY if sac.eligible() says the drain may run it — so a second,
    drifting copy of the grant test cannot be introduced here."""
    row, answers = (None, {}) if over is None else _registry(NEWS, **over)
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur({**answers, K_ACT_INSERT: [(9,)]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("BRAIN_ACTUATORS_DISABLE", raising=False)
    body, code = sac.actuate(NEWS, confirm=True)
    fired = bool(calls["fire"])
    assert fired is sac.eligible(row)[0], (over, code, body.get("refused"))
    assert fired is (code == 200)


def test_actuate_reads_the_grant_BEFORE_the_trigger_and_the_fire(monkeypatch):
    """Order is the finding: the grant is consulted first, so an ungranted
    class never even runs the (correlated, 18.5k-row) trigger query."""
    order = []
    _fake_autonomy(monkeypatch, trigger=lambda: order.append("trigger") or 7,
                   fire=lambda c, cu, n: order.append("fire") or {
                       "rows_affected": 1, "rollback": [], "result": {}, "ok": True})
    cur = _Cur({**_granted(DEALS), K_ACT_INSERT: [(9,)]})
    real_execute = cur.execute

    def _exec(sql, params=None):
        if K_CLASS_ROW in " ".join(str(sql).split()):
            order.append("grant")
        return real_execute(sql, params)
    cur.execute = _exec
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("BRAIN_ACTUATORS_DISABLE", raising=False)
    body, code = sac.actuate(DEALS, confirm=True)
    assert code == 200 and body["executed"]
    assert order == ["grant", "trigger", "fire"], order


def test_actuate_with_confirm_fires_under_the_shells_budget_and_ledgers_the_rollback(monkeypatch):
    calls = _fake_autonomy(monkeypatch, trigger=7)
    cur = _Cur({**_granted(DEALS), K_ACT_INSERT: [(77,)]})
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("BRAIN_ACTUATORS_DISABLE", raising=False)
    body, code = sac.actuate(DEALS, confirm=True, by="test")
    assert code == 200 and body["executed"] and body["ok"] and body["granted"]
    assert calls["fire"] == [7] and calls["budget"] == [DEALS] and calls["ensure"] == 1
    assert body["rows_affected"] == 3 and body["actuator_run_id"] == 77
    assert body["rollback_rows"] == 1
    ins = _sqls(cur, K_ACT_INSERT)
    assert len(ins) == 1 and " live, " in ins[0][0]
    p = ins[0][1]
    assert p[0] == DEALS and p[1] == 7 and p[2] == 3 and p[3] is True
    assert json.loads(p[5]) == [{"id": "ext_a", "prior": None}]
    assert json.loads(p[4])["via"] == "squasher_action_classes.actuate"
    assert conn.commits >= 1
    # a fire that shares our transaction needs no pre-fire row: ONE statement
    assert _sqls(cur, K_ACT_UPDATE) == []


@pytest.mark.parametrize("setup,needle", [
    (dict(budget=(False, True)), "budget spent"),
    (dict(budget=(True, False)), "budget spent"),
    (dict(trigger=None), "unreadable"),
    (dict(env={"BRAIN_ACTUATORS_DISABLE": "1"}), "BRAIN_ACTUATORS_DISABLE"),
    (dict(env={"ACTION_CLASSES_ENABLED": "0"}), "ACTION_CLASSES_ENABLED"),
])
def test_actuate_refuses_409_and_never_fires_when_a_gate_is_shut(monkeypatch, setup, needle):
    env = setup.pop("env", {})
    calls = _fake_autonomy(monkeypatch, **setup)
    cur = _Cur(_granted())
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("BRAIN_ACTUATORS_DISABLE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 409 and body["ok"] is False and needle in body["refused"]
    assert calls["fire"] == [] and body["executed"] is False
    assert _sqls(cur, "INSERT") == []


def test_actuate_does_not_fire_when_the_defect_is_absent(monkeypatch):
    calls = _fake_autonomy(monkeypatch, trigger=0)
    cur = _Cur(_granted())
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 200 and body["executed"] is False and "absent" in body["note"]
    assert calls["fire"] == []


def test_actuate_captures_the_news_pre_image_as_the_rollback(monkeypatch):
    _fake_autonomy(monkeypatch, trigger=3, fire_result={
        "rows_affected": 2, "rollback": None, "result": {"resolved": 2}, "ok": True})
    cur = _Cur({K_PRE_IMAGE: [(1, "unknown"), (2, "rejected"), (3, None)],
                K_POST_IMAGE: [(1,), (3,)], **_granted(), K_ACT_INSERT: [(78,)]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 200 and body["executed"] and body["rollback_rows"] == 2
    assert body["actuator_run_id"] == 78
    pre = [p for s, p in cur.calls if K_PRE_IMAGE in s]
    assert pre and pre[0] == (sac._NEWS_PRE_IMAGE_CAP,)
    post = [p for s, p in cur.calls if K_POST_IMAGE in s]
    assert post[0] == ([1, 2, 3],)
    sqls = [s for s, _ in cur.calls]
    assert sqls.index(next(s for s in sqls if K_PRE_IMAGE in s)) < sqls.index(
        next(s for s in sqls if K_POST_IMAGE in s)), "pre-image BEFORE the fire"
    # the pre-fire row carries the WHOLE pre-image; the finish narrows it to
    # the rows the fire actually flipped
    p = _sqls(cur, K_ACT_INSERT)[0][1]
    assert json.loads(p[5]) == [{"id": 1, "prior": "unknown"},
                                {"id": 2, "prior": "rejected"},
                                {"id": 3, "prior": None}]
    u = _sqls(cur, K_ACT_UPDATE)
    assert len(u) == 1 and u[0][1][-1] == 78
    assert json.loads(u[0][1][3]) == [{"id": 1, "prior": "unknown"},
                                      {"id": 3, "prior": None}]


def test_the_news_run_row_is_DURABLE_before_a_self_committing_fire(monkeypatch):
    """★★★ _fire_entity_reresolve → _reresolve_unmatched calls c.commit() on
    OUR connection mid-fire, so its FALSE→TRUE flips are durable before the
    wrapper reaches its ledger INSERT. If the run row were written after the
    fire, a crash in that window would leave a live mutation with no
    brain_actuator_runs row and no rollback payload — /rollback-run would
    answer 'nothing to reverse'. Reversibility is what made this class
    eligible, so the row goes in and COMMITS first, carrying the pre-image."""
    seen = {}

    def _fire(conn, cur, n):
        seen["inserts"] = _sqls(cur, K_ACT_INSERT)
        seen["commits"] = conn.commits
        conn.commit()                       # exactly what _reresolve_unmatched does
        raise RuntimeError("process died mid-fire")

    _fake_autonomy(monkeypatch, trigger=3, fire=_fire)
    cur = _Cur({K_PRE_IMAGE: [(1, "unknown"), (2, None)],
                **_granted(), K_ACT_INSERT: [(91,)]})
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 200 and body["ok"] is False and "process died" in body["error"]
    assert len(seen["inserts"]) == 1, "the fire started with NO ledger row"
    assert seen["commits"] >= 1, "the ledger row was not COMMITTED before the fire"
    payload = json.loads(seen["inserts"][0][1][5])
    assert payload == [{"id": 1, "prior": "unknown"}, {"id": 2, "prior": None}], \
        "the pre-fire row must carry a usable rollback, not NULL"


def test_actuate_refuses_a_non_actuator_class_and_an_absent_shell(monkeypatch):
    assert sac.actuate(FAC, confirm=True)[1] == 404
    assert sac.actuate("nope", confirm=False)[1] == 404

    def _boom(cls):
        raise ImportError("no shell")
    monkeypatch.setattr(sac, "_actuator_for", _boom)
    body, code = sac.actuate(NEWS, confirm=True)
    assert code == 409 and "unavailable" in body["refused"]


def test_read_trigger_reports_unobserved_not_zero(monkeypatch):
    _fake_autonomy(monkeypatch, trigger=None)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(_Cur()))
    out = sac.read_trigger(NEWS)
    assert out["ok"] is False and out["blindspot"] is None
    _fake_autonomy(monkeypatch, trigger=7)
    out = sac.read_trigger(NEWS)
    assert out["ok"] and out["blindspot"] == 7 and out["actuator"] == NEWS
    assert sac.read_trigger(FAC)["ok"] is False
    # the drain's metric reader accepts exactly this shape
    assert sac._read_metric(lambda m, u: (200, out), NEWS_VER, "blindspot")[0] == 7


def test_rollback_run_applies_the_stored_payload_in_one_statement(monkeypatch):
    cur = _Cur({K_ACT_RUN: [(DEALS, [{"id": "ext_a", "prior": None}, {"id": "ext_b", "prior": "x"}])]})
    cur.rowcount = 2
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    body, code = sac.rollback_run(77, by="op")
    assert code == 200 and body["ok"] and body["rolled_back"] == 2 and body["of_run"] == 77
    ups = _sqls(cur, "UPDATE deals AS d SET data_flag = v.prior")
    assert len(ups) == 1 and ups[0][1] == (["ext_a", "ext_b"], [None, "x"])
    assert "d.data_flag = 'quarantine_duplicate'" in ups[0][0], "only rows the run left quarantined"
    led = _sqls(cur, "INSERT INTO brain_actuator_runs")
    assert led[0][1][0] == DEALS + ":rollback" and led[0][1][1] == 2
    assert conn.commits == 1


def test_rollback_run_for_news_restores_the_prior_status_by_int_id(monkeypatch):
    cur = _Cur({K_ACT_RUN: [(NEWS, json.dumps([{"id": 1, "prior": "unknown"}]))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, code = sac.rollback_run("78")
    assert code == 200 and body["ok"]
    ups = _sqls(cur, "UPDATE news_discovered_entities AS e")
    assert ups[0][1] == ([1], ["unknown"]) and "in_facilities = FALSE" in ups[0][0]


@pytest.mark.parametrize("answer,code,needle", [
    ([], 404, "no such"),
    ([(DEALS, [])], 400, "no rollback payload"),
    ([("something_else", [{"id": 1}])], 400, "no rollback rule"),
])
def test_rollback_run_refuses_when_there_is_nothing_safe_to_reverse(monkeypatch, answer, code, needle):
    cur = _Cur({K_ACT_RUN: answer})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, got = sac.rollback_run(5)
    assert got == code and needle in body["error"]
    assert _sqls(cur, "UPDATE") == [] and _sqls(cur, "INSERT") == []
    assert sac.rollback_run("x")[1] == 400


def test_an_undo_is_ledgered_but_never_SPENDS_the_actuation_budget(monkeypatch):
    """★ An undo is auditable, not an actuation. bam._budget_ok's global arm
    counts every live brain_actuator_runs row with NO actuator filter, so
    three rollbacks would budget-lock the WHOLE fleet for 24h — including the
    actuator you are undoing in order to re-run correctly. ONE definition
    (bam.ROLLBACK_SUFFIX): the writer stamps it, the budget arm drops it."""
    from routes import brain_autonomy_master_shell as bam
    cur = _Cur({K_ACT_RUN: [(DEALS, [{"id": "ext_a", "prior": None}])]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    assert sac.rollback_run(77, by="op")[1] == 200
    led = _sqls(cur, K_ACT_INSERT)[0][1]
    assert led[0] == bam.rollback_actuator_id(DEALS), \
        "the undo was not written under the id the budget arm excludes"

    bcur = _Cur({"FROM brain_actuator_runs": [(0, 0)]})
    bam._budget_ok(bcur, DEALS)
    sql, params = bcur.calls[-1]
    assert "right(actuator, %s) <> %s" in sql, \
        f"the budget arm counts rollback rows as actuations: {sql}"
    assert len(params) >= 3, f"the exclusion is not bound as parameters: {params}"
    n, suffix = params[-2], params[-1]
    assert (n, suffix) == (len(bam.ROLLBACK_SUFFIX), bam.ROLLBACK_SUFFIX)
    assert led[0][-n:] == suffix, "the row the undo writes is not the row it drops"
    assert "%" not in sql.replace("%s", ""), \
        "literal % in a parameterised statement — psycopg2 raises on execute"


def test_wrapper_endpoints_are_gated_and_answer_404_on_kill_and_unknown_class(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    c = _app().test_client()
    h = {"X-Admin-Key": "adm"}
    assert c.get("/api/v1/brain/squasher/verifier/" + NEWS).status_code == 401
    assert c.post("/api/v1/brain/squasher/actuate/" + NEWS).status_code == 401
    assert c.post("/api/v1/brain/squasher/rollback-run", json={}).status_code == 401
    assert c.post("/api/v1/brain/squasher/graduation").status_code == 401
    assert c.get("/api/v1/brain/squasher/verifier/" + FAC, headers=h).status_code == 404
    assert c.post("/api/v1/brain/squasher/actuate/nope", headers=h).status_code == 404
    _fake_autonomy(monkeypatch, trigger=4)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(_Cur()))
    rv = c.get("/api/v1/brain/squasher/verifier/" + NEWS, headers=h)
    assert rv.status_code == 200 and rv.get_json()["blindspot"] == 4
    rv = c.post("/api/v1/brain/squasher/actuate/" + NEWS, headers=h)
    assert rv.status_code == 200 and rv.get_json()["dry_run"] is True
    monkeypatch.setenv("SQUASHER_QUEUE_DISABLE", "1")
    for rv in (c.get("/api/v1/brain/squasher/verifier/" + NEWS, headers=h),
               c.post("/api/v1/brain/squasher/actuate/" + NEWS + "?confirm=1", headers=h),
               c.post("/api/v1/brain/squasher/rollback-run", headers=h, json={"actuator_run_id": 1}),
               c.post("/api/v1/brain/squasher/graduation", headers=h)):
        assert rv.status_code == 404, rv.status_code


def test_the_graduation_endpoint_files_and_the_classes_GET_only_reads(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    cur = _grad_cur([_news_cls()], [(NEWS, 4, 3, 0, 0, None)], extra={
        "WHERE executed AND NOT dry_run": [(0,)], "WHERE verified AND NOT dry_run": [(0,)]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    c = _app().test_client()
    h = {"X-Admin-Key": "adm"}
    rv = c.get("/api/v1/brain/squasher/classes", headers=h)
    assert rv.status_code == 200 and rv.get_json()["graduation"]["eligible"] == [NEWS]
    assert _sqls(cur, K_Q_INSERT) == [], "GET never files"
    rv = c.post("/api/v1/brain/squasher/graduation", headers=h, json={"by": "tick"})
    assert rv.status_code == 200 and rv.get_json()["filed"] == [901]
    assert len(_sqls(cur, K_Q_INSERT)) == 1


def _kill_5xx(path):
    """-> (bad status codes found inside `if _disabled():` blocks, how many
    such blocks the scan actually FOUND). The second number is the point: a
    scan that matched nothing returns [] and reads exactly like 'every guard
    is clean'."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad, blocks = [], 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            blocks += 1
            bad += [n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, int)
                    and n.value >= 500]
    return bad, blocks


# PINNED FLOOR — every `if _disabled():` block each module carries today.
# Not decoration: with no floor this scan PASSES with every kill switch
# deleted, because then it matches no If node at all. Raise a number here
# only when a guard is genuinely added; a DROP is the bug this exists for.
_KILL_SWITCH_FLOOR = {"squasher_action_classes.py": 1, "squasher_queue.py": 6}


@pytest.mark.parametrize("name", sorted(_KILL_SWITCH_FLOOR))
def test_no_kill_switch_block_returns_5xx(name):
    bad, blocks = _kill_5xx(ROOT / "routes" / name)
    assert blocks >= _KILL_SWITCH_FLOOR[name], (
        f"{name}: the scan found {blocks} `if _disabled():` blocks, floor is "
        f"{_KILL_SWITCH_FLOOR[name]} — a kill switch was deleted, or the scan "
        f"stopped seeing them. An empty scan is NOT a clean scan.")
    assert bad == [], f"{name}: a kill-switch block returns {bad}"


# ══════════════════════════════════════════════════════════════════════════
#  9 · the portal: grouped by class, one Decide control, graduation shown
# ══════════════════════════════════════════════════════════════════════════

def _page(**over):
    d = {"verdict": {"state": "AMBER", "headline": "h", "detail": "d"},
         "action_classes": {
             "known": True, "enabled": True, "caps": {}, "day_used": 0,
             "verified_7d": 0, "classes": [_cls(granted=True), _news_cls()],
             "inbox_by_class": {
                 FAC: [_row(), _row(id=219, finding_key="k:CA")],
                 "unclassified": [_row(id=247, action_class=None, action_url=None,
                                       action_method=None, status="awaiting_decision")]},
             "graduation": {"known": True, "classes": [{
                 "class": NEWS, "granted": False, "candidate_reason": "why",
                 "track_record_required": {"clean_dry_runs": 3}, "clean_dry_runs_7d": 1,
                 "dry_run_reads_7d": 2, "runs_ok_7d": 0, "runs_failed_7d": 0,
                 "breaker_tripped": False, "eligible_for_grant": False,
                 "not_eligible_because": ["1/3 clean dry runs in 7d"], "proposal": None}]}},
         "queue_ages": {"known": True, "by_class": {FAC: {"oldest_age_hours": 41.5}}}}
    d.update(over)
    return d


def test_the_portal_groups_by_class_with_ONE_decide_control_per_registry_class():
    html = sp.render(_page())
    assert html.count("class='decide-bar'") == 1, "unclassified is not a class"
    assert 'class=\'decide\' data-class="facility_dedup_apply">Decide class (2 rows)' in html
    assert "oldest 41.5 h" in html
    assert "/api/v1/brain/squasher/resolve-class" in html, "the control posts to resolve-class"
    assert "1/3 clean dry runs in 7d" in html and "never an automatic grant" in html


def test_the_portal_renders_an_unreadable_graduation_as_unreadable():
    d = _page()
    d["action_classes"]["graduation"] = {"known": False, "error": "ledger gone"}
    html = sp.render(d)
    assert "UNREADABLE" in html and "ledger gone" in html
    d["action_classes"].pop("graduation")
    assert "UNREADABLE" in sp.render(d)


def test_collect_carries_queue_ages_and_fails_soft(monkeypatch):
    monkeypatch.setattr(sp, "_get", lambda path: {})
    monkeypatch.setattr(sq, "queue_rows", lambda n=12: [])
    monkeypatch.setattr(sac, "summary", lambda: {"known": False})
    monkeypatch.setattr(sq, "queue_ages", lambda: {"known": True, "open_rows": 3})
    out = sp.collect()
    assert out["queue_ages"] == {"known": True, "open_rows": 3}

    def _boom():
        raise RuntimeError("x")
    monkeypatch.setattr(sq, "queue_ages", _boom)
    assert sp.collect()["queue_ages"] == {"known": False}
