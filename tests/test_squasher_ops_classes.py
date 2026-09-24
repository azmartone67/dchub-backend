"""tests/test_squasher_ops_classes.py — the two OPS action classes
(2026-09-23): edge_purge_path and freshness_refresh_job.

Both were measured on the live squasher board as rows a human was asked to
decide although each remedy is one fixed call: row 477
(site_sentinel_unhealthy:/pricing @ https://dchub.cloud/pricing, "human
decision required") and row 483 (data_freshness_sla_breach @
table:news_articles, "operator action required: POST /api/news/refresh").

Every clause is exercised as BEHAVIOUR against the real module: the real drain
step (run_granted_actions → execute_one), the real probe (probe_one) and the
real wrappers run against a cursor stub and a small WORLD — a loopback stub
that routes the action call into the wrapper under test and whose verifier
reading moves only when the purge / the job actually happened. So "the count
dropped" is caused by the action, not scripted beside it.

★ MUTATION CHECKS (in-suite, per the brief): the verifier-drop rule and the
confirm=1 dry-run rule are each re-run against a MUTANT copy of the module
(source edited in memory, never on disk; the edit is asserted to have
applied). A mutant must turn the guarded scenario the wrong way — which is
what proves the assertion guarding it can fail.

House rules: no DB, never import main, nothing runs at module scope.
Run:  python3 -m pytest tests/test_squasher_ops_classes.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from routes import squasher_action_classes as sac
from routes import squasher_queue as sq
from tests.test_squasher_action_classes import (  # noqa: F401  (claim_ledger is a fixture)
    _Conn, _Cur, _cls_tuple, _row_tuple, claim_ledger)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "routes" / "squasher_action_classes.py").read_text(encoding="utf-8")

PURGE = "edge_purge_path"
FRESH = "freshness_refresh_job"
PURGE_ACT = "/api/v1/brain/squasher/ops/edge-purge"
PURGE_VER = "/api/v1/brain/squasher/ops/sentinel-unhealthy"
FRESH_ACT = "/api/v1/brain/squasher/ops/refresh-job"
FRESH_VER = "/api/v1/brain/squasher/ops/freshness-breaches"
REPROBE = "/api/v1/admin/sentinel-inbox/probe"
NEWS_TRIGGER = "/api/jobs/news-refresh"

# The two live queue rows (squasher.json, 2026-09-23).
ROW_477 = ("https://dchub.cloud/pricing", "site_sentinel_unhealthy:/pricing",
           "human decision required: Run `curl -i https://dchub.cloud/pricing` "
           "and capture the HTTP status, headers, and body ...")
ROW_483 = ("table:news_articles", "data_freshness_sla_breach",
           "operator action required: POST /api/news/refresh — Re-enable the "
           "`news-refresh` cron/scheduler entry in the Railway deployment ...")


# ── stubs ─────────────────────────────────────────────────────────────────

def _clsrow(cls, **kw):
    spec = sac.ACTION_CLASSES[cls]
    d = {"class": cls, "granted": True, "granted_by": "owner", "granted_at": None,
         "bound_params": dict(spec["bound_params"]),
         "verifier_url": spec["verifier_url"], "reversible": spec["reversible"],
         "runs_ok": 0, "runs_failed": 0, "consecutive_failed": 0,
         "last_run_at": None, "breaker_tripped": False, "notes": "",
         "candidate_reason": spec["candidate_reason"],
         "track_record_required": dict(spec["track_record_required"])}
    d.update(kw)
    return d


def _qrow(cls, **kw):
    c = (sac.classify_row(*ROW_477[:2]) if cls == PURGE
         else sac.classify_row(*ROW_483[:2]))
    d = {"id": 477 if cls == PURGE else 483,
         "finding_key": ROW_477[0] if cls == PURGE else ROW_483[0],
         "title": ROW_477[1] if cls == PURGE else ROW_483[1],
         "status": "awaiting_ops", "action_class": cls,
         "action_url": c["action_url"], "action_method": "POST",
         "finished_at": None}
    d.update(kw)
    return d


def _db(mod, monkeypatch, cls_row, rows=(), cron=None, day_used=0,
        registry_raises=False):
    """Point `mod._conn` at a cursor stub carrying one class row, the queue
    rows and (for the job class) one cron_last_run row. -> the cursor."""
    answers = {
        "FROM brain_action_classes ORDER BY class": [_cls_tuple(cls_row)],
        "FROM brain_action_classes WHERE class = %s": [_cls_tuple(cls_row)],
        "WHERE executed AND NOT dry_run": [(day_used,)],
        "WHERE verified AND NOT dry_run": [(0,)],
        "JOIN brain_action_classes c ON": [_row_tuple(r) for r in rows],
        "INSERT INTO brain_action_class_runs": [(901,)],
        "action_class IS NULL ORDER BY id DESC": [],
        "FROM cron_last_run": [cron] if cron else [],
        "MAX(started_at)) / 3600.0": [(None,)],
        "WHERE action_class = %s AND status = 'awaiting_ops'":
            [_row_tuple(r) for r in rows],
    }
    cur = _Cur(answers, raise_on=("FROM brain_action_classes WHERE class"
                                  if registry_raises else None))
    conn = _Conn(cur)
    monkeypatch.setattr(mod, "_conn", lambda: conn)
    return cur


def _env(monkeypatch, enabled="1"):
    if enabled is None:
        monkeypatch.delenv("ACTION_CLASSES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ACTION_CLASSES_ENABLED", enabled)
    for k in ("ACTION_CLASS_MAX_PER_DRAIN", "ACTION_CLASS_MAX_PER_DAY",
              "SQUASHER_QUEUE_DISABLE"):
        monkeypatch.delenv(k, raising=False)


def _q(path):
    u = urlsplit(path)
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


class _EdgeWorld:
    """The edge + the sentinel. `unhealthy` is the sentinel's stored reading;
    only a purge FOLLOWED by the sentinel's re-probe can clear it, and only
    when the page is healthy at origin. The action call is routed into
    `mod.edge_purge` — the wrapper under test decides what happens."""

    def __init__(self, mod, *, unhealthy=1, origin_ok=True, purge_ok=True,
                 configured=True):
        self.mod, self.unhealthy, self.origin_ok = mod, unhealthy, origin_ok
        self.purge_ok, self.configured = purge_ok, configured
        self.purged, self.calls, self.stale = [], [], True

    def purge(self, urls):
        self.purged.append(list(urls))
        if self.purge_ok:
            self.stale = False
            return {"ok": True, "status": 200, "purged": urls}
        return {"ok": False, "status": 403, "error": "cf said no"}

    def __call__(self, method, path):
        self.calls.append((method, path))
        p, q = _q(path)
        if method == "GET" and p == PURGE_VER:
            if self.unhealthy is None:
                return 200, {"ok": False, "unhealthy": None}
            return 200, {"ok": True, "unhealthy": self.unhealthy}
        if method == "POST" and p == REPROBE:
            if not self.stale and self.origin_ok:
                self.unhealthy = 0
            return 200, {"ok": True, "scan": {"healthy": self.unhealthy == 0}}
        if method == "POST" and p == PURGE_ACT:
            body, code = self.mod.edge_purge(
                q.get("path", ""), confirm=(q.get("confirm") == "1"),
                fetch=self, purger=(self.purge, self.configured),
                sleep=lambda s: None)
            return code, body
        raise AssertionError(f"unexpected loopback {method} {path}")

    @property
    def action_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].startswith(PURGE_ACT)]


class _NewsWorld:
    """news_articles + its job. `breaches` is the radar's reading; only the
    job's trigger running can clear it, and only when the feeds carried an
    article published inside the SLA (`fresh_news`)."""

    def __init__(self, mod, *, breaches=1, fresh_news=True, trigger_status=200):
        self.mod, self.breaches, self.fresh_news = mod, breaches, fresh_news
        self.trigger_status, self.calls, self.ran = trigger_status, [], 0

    def __call__(self, method, path):
        self.calls.append((method, path))
        p, q = _q(path)
        if method == "GET" and p == FRESH_VER:
            return 200, {"ok": self.breaches is not None, "breaches": self.breaches}
        if method == "POST" and p == NEWS_TRIGGER:
            self.ran += 1
            if self.trigger_status != 200:
                return self.trigger_status, {"success": False, "error": "boom"}
            if self.fresh_news:
                self.breaches = 0
            return 200, {"success": True, "job": "news-refresh",
                         "new_articles": 7 if self.fresh_news else 0}
        if method == "POST" and p == FRESH_ACT:
            body, code = self.mod.refresh_job(
                q.get("job", ""), confirm=(q.get("confirm") == "1"), fetch=self)
            return code, body
        raise AssertionError(f"unexpected loopback {method} {path}")

    @property
    def triggers(self):
        return [c for c in self.calls if c == ("POST", NEWS_TRIGGER)]


_CRON_IDLE = (3 * 3600.0, False)       # started 3h ago, completed after it


def _mutant(*edits):
    """A copy of the module with `edits` [(old, new), ...] applied IN MEMORY.
    Each `old` must occur exactly once and the result must contain `new` —
    a mutation that silently did not apply would make the check vacuous."""
    src = SRC
    for old, new in edits:
        assert src.count(old) == 1, f"mutation anchor not unique/present: {old!r}"
        src = src.replace(old, new)
        assert new in src and src != SRC
    mod = types.ModuleType("sac_mutant")
    mod.__file__ = str(ROOT / "routes" / "squasher_action_classes.py")
    exec(compile(src, "<sac_mutant>", "exec"), mod.__dict__)
    return mod


def _class_update(cur):
    for sql, params in cur.calls:
        if sql.startswith("UPDATE brain_action_classes SET runs_ok"):
            return params
    return None


def _resolved_ids(cur):
    return [p[-1] for s, p in cur.calls
            if s.startswith("UPDATE squasher_work_queue") and "SET status = 'resolved'" in s]


# ══════════════════════════════════════════════════════════════════════════
#  1 · classification: the finding's own key and title, never prose
# ══════════════════════════════════════════════════════════════════════════

def test_the_live_rows_classify_and_the_url_is_rebuilt_from_the_registry():
    c = sac.classify_row(*ROW_477)
    assert c == {"action_class": PURGE, "action_method": "POST",
                 "params": {"path": "/pricing"},
                 "action_url": PURGE_ACT + "?path=/pricing&confirm=1"}
    c = sac.classify_row(*ROW_483)
    assert c == {"action_class": FRESH, "action_method": "POST",
                 "params": {"job": "news_refresh"},
                 "action_url": FRESH_ACT + "?job=news_refresh&confirm=1"}


def test_the_purge_allowlist_IS_the_sentinel_manifest_imported():
    """Every plain manifest path is purgeable and nothing else is. The three
    manifest paths that cannot round-trip as a bare query value are out."""
    from routes.site_sentinel import _MANIFEST
    rx = sac.ACTION_CLASSES[PURGE]["row_param_re"]
    plain = [e["path"] for e in _MANIFEST if sac._PLAIN_PATH_RE.fullmatch(e["path"])]
    odd = [e["path"] for e in _MANIFEST if not sac._PLAIN_PATH_RE.fullmatch(e["path"])]
    assert len(plain) >= 90 and odd, (len(plain), odd)
    assert all(sac._row_param_ok(PURGE, p) for p in plain)
    assert not any(sac._row_param_ok(PURGE, p) for p in odd)
    for bad in ("/evil", "/pricing/../admin", "pricing", "/pricing?x=1",
                "https://dchub.cloud/pricing", "", "/PRICING"):
        assert not sac._row_param_ok(PURGE, bad), bad
    assert sac.classify_row("https://dchub.cloud/evil", "site_sentinel_unhealthy:/evil") is None
    assert rx.startswith("^(?:") and rx.endswith(")$")


def test_an_unimportable_sentinel_leaves_an_allowlist_that_matches_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.site_sentinel", None)
    assert sac._sentinel_purge_paths() == ()
    import re
    assert not re.match(sac._one_of(()), "/pricing")
    assert not re.match(sac._one_of(()), "")


def test_title_and_key_must_BOTH_match_and_name_the_same_path():
    assert sac.classify_key("https://dchub.cloud/news", "site_sentinel_unhealthy:/pricing") is None
    assert sac.classify_key(None, "site_sentinel_unhealthy:/pricing") is None
    assert sac.classify_key("https://dchub.cloud/pricing", None) is None
    assert sac.classify_key("https://evil.example/pricing", "site_sentinel_unhealthy:/pricing") is None
    # other sentinel finding types are not this class's remedy
    assert sac.classify_key("https://dchub.cloud/pricing", "nav_missing:/pricing") is None
    assert sac.classify_key("https://dchub.cloud/pricing", "page_stale:/pricing") is None


def test_a_table_no_job_feeds_and_a_pathless_qa_row_stay_unclassified():
    assert sac.classify_row("table:gas_pipelines", "data_freshness_sla_breach") is None
    assert sac.classify_row("table:news_articles", "sla_column_unmeasurable") is None
    # qa_critical carries only a count + a hashed key — no path to purge
    assert sac.classify_row("dchub://qa-superuser/web::public-pages#21d6c8",
                            "qa_critical 1 public page(s) do not render") is None


def test_prose_is_never_read_by_the_key_rule():
    prose = ("site_sentinel_unhealthy:/pricing at https://dchub.cloud/pricing; "
             "also data_freshness_sla_breach @ table:news_articles")
    assert sac.classify_row("heal:something_else", "some title", prose) is None


def test_the_endpoint_rule_still_outranks_the_key_rule():
    c = sac.classify_row(ROW_477[0], ROW_477[1],
                         "POST /api/v1/admin/facility-dedup/apply?country=SG")
    assert c["action_class"] == "facility_dedup_apply"


def test_row_params_are_rederived_from_the_STORED_url_and_revalidated():
    assert sac.row_params_of(_qrow(PURGE)) == {"path": "/pricing"}
    assert sac.row_params_of(_qrow(FRESH)) == {"job": "news_refresh"}
    tampered = _qrow(PURGE, action_url=PURGE_ACT + "?path=/evil&confirm=1")
    assert sac.row_params_of(tampered) is None
    assert sac.row_params_of(_qrow(FRESH, action_url=FRESH_ACT + "?job=rm_rf&confirm=1")) is None


# ══════════════════════════════════════════════════════════════════════════
#  2 · promotion: a key-rule row is an ops row — awaiting_decision → awaiting_ops
# ══════════════════════════════════════════════════════════════════════════

def _promotions(cur):
    return [(s, p) for s, p in cur.calls
            if s.startswith("UPDATE squasher_work_queue SET status = 'awaiting_ops'")]


def test_a_settled_sentinel_row_is_promoted_inside_the_savepoint():
    cur = _Cur()
    assert sac.classify_in_tx(cur, 477, ROW_477[2], finding_key=ROW_477[0],
                              title=ROW_477[1]) is True
    sqls = [s for s, _ in cur.calls]
    assert sqls[0].startswith("SAVEPOINT") and sqls[-1].startswith("RELEASE SAVEPOINT")
    (sql, params), = _promotions(cur)
    # the SQL is the guard: only FROM awaiting_decision, only this class
    assert "WHERE id = %s AND status = 'awaiting_decision' AND action_class = %s" in sql
    assert params[1:] == (477, PURGE)
    assert params[0].startswith(f"action_class {PURGE}: the registry names")


def test_CONTROL_a_class_without_a_key_rule_is_never_promoted():
    cur = _Cur()
    assert sac.classify_in_tx(cur, 255, "operator action required: POST /api/v1/"
                              "admin/facility-dedup/apply?country=NL&confirm=1") is True
    assert _promotions(cur) == []
    cur = _Cur()
    sac.classify_in_tx(cur, 9, finding_key="graph_spine:es_blindspot", title="t")
    assert _promotions(cur) == [], "granted class-scoped classes keep today's routing"


def test_the_backfill_reports_what_it_promoted():
    cur = _Cur({"action_class IS NULL ORDER BY id DESC": [
        (477, ROW_477[0], ROW_477[1], ROW_477[2], None, None),
        (483, ROW_483[0], ROW_483[1], ROW_483[2], None, None)]})
    out = sac.classify_open_rows(cur)
    assert out["classified"] == 2 and out["promoted"] == 2
    assert out["by_class"] == {PURGE: 1, FRESH: 1}
    assert [p[1] for _, p in _promotions(cur)] == [477, 483]


# ══════════════════════════════════════════════════════════════════════════
#  3 · the drain: execute → verify → the count must DROP
# ══════════════════════════════════════════════════════════════════════════

def _drain_purge(mod, monkeypatch, *, consecutive=0, **world):
    _env(monkeypatch)
    cur = _db(mod, monkeypatch, _clsrow(PURGE, consecutive_failed=consecutive),
              rows=[_qrow(PURGE)])
    w = _EdgeWorld(mod, **world)
    out = mod.run_granted_actions(fetch=w)
    return out, cur, w


def _drain_news(mod, monkeypatch, *, cron=_CRON_IDLE, **world):
    _env(monkeypatch)
    cur = _db(mod, monkeypatch, _clsrow(FRESH), rows=[_qrow(FRESH)], cron=cron)
    w = _NewsWorld(mod, **world)
    out = mod.run_granted_actions(fetch=w)
    return out, cur, w


def test_a_granted_purge_verifies_when_the_purge_clears_the_sentinel(monkeypatch, claim_ledger):
    out, cur, w = _drain_purge(sac, monkeypatch)
    res = out["results"][0]
    assert (res["outcome"], res["verified"], res["pre_count"], res["post_count"]) == (
        "verified", True, 1, 0)
    assert w.purged == [["https://dchub.cloud/pricing"]], "exactly ONE url purged"
    order = [(m, _q(p)[0]) for m, p in w.calls]
    assert order == [("GET", PURGE_VER), ("POST", PURGE_ACT), ("POST", REPROBE),
                     ("GET", PURGE_VER)]
    assert _resolved_ids(cur) == [477]
    rec = claim_ledger["register"][0]
    assert rec["subject"] == "edge_purge_path:/pricing" and rec["expected_op"] == "lt"


def _assert_no_drop_is_a_failure(res, cur):
    assert res["executed"] is True
    assert res["verified"] is False, "a run whose verifier did not DROP was called verified"
    assert res["outcome"] == "failed_no_drop"
    assert _class_update(cur)[:3] == (0, 1, 1)
    assert _resolved_ids(cur) == []


def test_a_purge_that_does_not_fix_the_page_is_a_FAILURE(monkeypatch, claim_ledger):
    """Broken at ORIGIN: the purge ran, the re-probe still fails, the count
    holds. That is exactly the case the verifier exists to refuse."""
    out, cur, w = _drain_purge(sac, monkeypatch, origin_ok=False)
    _assert_no_drop_is_a_failure(out["results"][0], cur)
    assert w.purged, "the purge did run — and still counts as a failure"


def test_a_refresh_that_finds_nothing_new_is_a_FAILURE(monkeypatch, claim_ledger):
    out, cur, w = _drain_news(sac, monkeypatch, fresh_news=False)
    _assert_no_drop_is_a_failure(out["results"][0], cur)
    assert w.ran == 1


@pytest.mark.parametrize("mutation", [
    # the drop comparison weakened to "did not rise"
    ("and post is not None and post < pre)", "and post is not None and post <= pre)"),
    # the post-read dropped from the verdict entirely: 2xx alone "verifies"
    ("and post is not None and post < pre)", ")"),
])
@pytest.mark.parametrize("scenario", ["purge_origin_broken", "news_nothing_new"])
def test_MUTATION_the_verifier_drop_rule_can_fail(monkeypatch, claim_ledger, mutation, scenario):
    """The no-drop guard above, run against a module whose drop rule is
    broken: the mutant calls the no-drop run VERIFIED and resolves the row,
    and the same assertion that passes on the real module now raises."""
    mut = _mutant(mutation)
    if scenario == "purge_origin_broken":
        out, cur, _ = _drain_purge(mut, monkeypatch, origin_ok=False)
    else:
        out, cur, _ = _drain_news(mut, monkeypatch, fresh_news=False)
    res = out["results"][0]
    assert res["verified"] is True and res["outcome"] == "verified", res
    with pytest.raises(AssertionError):
        _assert_no_drop_is_a_failure(res, cur)


def test_a_granted_refresh_verifies_when_the_job_clears_the_breach(monkeypatch, claim_ledger):
    out, cur, w = _drain_news(sac, monkeypatch)
    res = out["results"][0]
    assert (res["outcome"], res["pre_count"], res["post_count"]) == ("verified", 1, 0)
    assert res["marked"] == 7, "rows_affected = the job's own new_articles"
    order = [(m, _q(p)[0]) for m, p in w.calls]
    assert order == [("GET", FRESH_VER), ("POST", FRESH_ACT), ("POST", NEWS_TRIGGER),
                     ("GET", FRESH_VER)]
    assert _resolved_ids(cur) == [483]


def test_three_consecutive_failures_trip_the_new_class_breaker(monkeypatch, claim_ledger):
    out, cur, _ = _drain_purge(sac, monkeypatch, consecutive=2, origin_ok=False)
    res = out["results"][0]
    assert res["consecutive_failed"] == 3 and res["breaker_tripped"] is True
    assert _class_update(cur)[2:4] == (3, True)


def test_a_failed_purge_is_a_failure_and_is_NOT_re_probed(monkeypatch, claim_ledger):
    """424 → failed_http. No re-probe: a probe landing after the copy expired
    on its own must not credit a purge that failed."""
    out, cur, w = _drain_purge(sac, monkeypatch, purge_ok=False)
    res = out["results"][0]
    assert res["outcome"] == "failed_http" and res["http_status"] == 424
    assert not [c for c in w.calls if _q(c[1])[0] == REPROBE]
    assert _class_update(cur)[1] == 1


def test_an_unconfigured_purge_is_a_REFUSAL_not_a_failure(monkeypatch, claim_ledger):
    out, cur, w = _drain_purge(sac, monkeypatch, configured=False)
    res = out["results"][0]
    assert res["outcome"] == "refused_by_endpoint" and res["executed"] is False
    assert w.purged == [] and _class_update(cur) is None


def test_the_cron_s_own_run_in_flight_is_a_REFUSAL_and_the_job_is_not_doubled(monkeypatch, claim_ledger):
    out, cur, w = _drain_news(sac, monkeypatch, cron=(120.0, True))
    res = out["results"][0]
    assert res["outcome"] == "refused_by_endpoint" and "in flight" in res["error"]
    assert w.triggers == [] and _class_update(cur) is None


def test_CONTROL_a_stale_start_with_no_completion_does_not_block_forever(monkeypatch, claim_ledger):
    out, cur, w = _drain_news(sac, monkeypatch, cron=(sac._JOB_IN_FLIGHT_S + 60.0, True))
    assert out["results"][0]["outcome"] == "verified" and len(w.triggers) == 1


def test_a_trigger_that_errors_is_a_failed_http(monkeypatch, claim_ledger):
    out, cur, w = _drain_news(sac, monkeypatch, trigger_status=500)
    res = out["results"][0]
    assert res["outcome"] == "failed_http" and res["http_status"] == 424
    assert _class_update(cur)[1] == 1


def test_an_unreadable_verifier_never_acts(monkeypatch, claim_ledger):
    out, cur, w = _drain_purge(sac, monkeypatch, unhealthy=None)
    assert out["results"][0]["outcome"] == "skipped_verifier_unreadable"
    assert w.action_posts == [] and w.purged == []


def test_an_UNGRANTED_ops_class_never_executes(monkeypatch, claim_ledger):
    _env(monkeypatch)
    cur = _db(sac, monkeypatch, _clsrow(PURGE, granted=False), rows=[_qrow(PURGE)])
    w = _EdgeWorld(sac)
    out = sac.run_granted_actions(fetch=w)
    assert out["ran"] == 0 and out["candidates"][0]["skip"] == "not granted"
    assert w.purged == [], "an ungranted class purged"
    assert not [p for _, p in w.calls if "confirm=1" in p]
    # ...it is PROBED instead (a real drain), through the dry url only
    assert out["probes"]["probed"] == 1
    assert out["probes"]["results"][0]["outcome"] == "probe_clean"


# ══════════════════════════════════════════════════════════════════════════
#  4 · the wrappers: the grant first, and no confirm=1 means a dry run
# ══════════════════════════════════════════════════════════════════════════

def _assert_dry_run_touched_nothing(body, code, world):
    assert code == 200 and body["dry_run"] is True and body["executed"] is False
    if isinstance(world, _EdgeWorld):
        assert world.purged == [], "a call WITHOUT confirm=1 purged"
        assert not [c for c in world.calls if _q(c[1])[0] == REPROBE]
    else:
        assert world.triggers == [], "a call WITHOUT confirm=1 ran the job"


@pytest.mark.parametrize("granted", [True, False])
def test_without_confirm_the_purge_wrapper_is_a_dry_run(monkeypatch, granted):
    _env(monkeypatch)
    _db(sac, monkeypatch, _clsrow(PURGE, granted=granted))
    w = _EdgeWorld(sac)
    body, code = sac.edge_purge("/pricing", confirm=False, fetch=w,
                                purger=(w.purge, True), sleep=lambda s: None)
    _assert_dry_run_touched_nothing(body, code, w)
    assert body["would_purge"] == ["https://dchub.cloud/pricing"]


@pytest.mark.parametrize("granted", [True, False])
def test_without_confirm_the_job_wrapper_is_a_dry_run(monkeypatch, granted):
    _env(monkeypatch)
    _db(sac, monkeypatch, _clsrow(FRESH, granted=granted), cron=_CRON_IDLE)
    w = _NewsWorld(sac)
    body, code = sac.refresh_job("news_refresh", confirm=False, fetch=w)
    _assert_dry_run_touched_nothing(body, code, w)
    assert body["trigger"] == "POST " + NEWS_TRIGGER and body["tables"] == ["news_articles"]


@pytest.mark.parametrize("mutation,which", [
    (("    if not confirm:\n        out.update(would_purge=[url],",
      "    if False:\n        out.update(would_purge=[url],"), "purge"),
    (("    if not confirm:\n        out[\"note\"] = \"dry run — add ?confirm=1 to run the job once\"",
      "    if False:\n        out[\"note\"] = \"dry run — add ?confirm=1 to run the job once\""), "job"),
])
def test_MUTATION_the_confirm_dry_run_rule_can_fail(monkeypatch, mutation, which):
    """The dry-run guard above, against a module whose wrapper no longer
    stops at the missing confirm=1: the mutant acts, and the assertion that
    passes on the real module raises. Ungranted, so the grant cannot mask it."""
    mut = _mutant(mutation)
    _env(monkeypatch)
    if which == "purge":
        _db(mut, monkeypatch, _clsrow(PURGE, granted=False))
        w = _EdgeWorld(mut)
        body, code = mut.edge_purge("/pricing", confirm=False, fetch=w,
                                    purger=(w.purge, True), sleep=lambda s: None)
        assert w.purged == [["https://dchub.cloud/pricing"]]
    else:
        _db(mut, monkeypatch, _clsrow(FRESH, granted=False), cron=_CRON_IDLE)
        w = _NewsWorld(mut)
        body, code = mut.refresh_job("news_refresh", confirm=False, fetch=w)
        assert w.triggers == [("POST", NEWS_TRIGGER)]
    with pytest.raises(AssertionError):
        _assert_dry_run_touched_nothing(body, code, w)


def test_MUTATION_the_endpoint_parsing_confirm_can_fail(monkeypatch):
    """The same rule one layer up: the HTTP handler must read confirm=1 from
    the request, not assume it. Mutant handler → a no-confirm POST purges."""
    import flask
    mut = _mutant(('edge_purge(request.args.get("path") or "",\n'
                   '                           confirm=(request.args.get("confirm") == "1")',
                   'edge_purge(request.args.get("path") or "",\n'
                   '                           confirm=True'))
    purged = []

    def run(mod):
        purged.clear()
        _env(monkeypatch)
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
        _db(mod, monkeypatch, _clsrow(PURGE, granted=True))
        monkeypatch.setattr(mod, "_cf_purge", lambda: (
            lambda urls: purged.append(urls) or {"ok": True}, True))
        monkeypatch.setattr(mod, "_loopback", lambda m, p: (200, {"scan": {}}))
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        app = flask.Flask("t_ops_mut")
        app.register_blueprint(mod.squasher_action_classes_bp)
        return app.test_client().post(PURGE_ACT + "?path=/pricing",
                                      headers={"X-Admin-Key": "adm"})
    rv = run(sac)
    assert rv.status_code == 200 and rv.get_json()["dry_run"] is True and purged == []
    rv = run(mut)
    assert purged == [["https://dchub.cloud/pricing"]], "the mutant must act"


def test_confirm_on_an_UNGRANTED_class_is_refused_and_touches_nothing(monkeypatch):
    _env(monkeypatch)
    _db(sac, monkeypatch, _clsrow(PURGE, granted=False))
    w = _EdgeWorld(sac)
    body, code = sac.edge_purge("/pricing", confirm=True, fetch=w,
                                purger=(w.purge, True), sleep=lambda s: None)
    assert code == 409 and "not granted" in body["refused"] and w.purged == []
    _db(sac, monkeypatch, _clsrow(FRESH, granted=False), cron=_CRON_IDLE)
    nw = _NewsWorld(sac)
    body, code = sac.refresh_job("news_refresh", confirm=True, fetch=nw)
    assert code == 409 and nw.triggers == []


def test_a_TRIPPED_class_is_refused_even_on_the_dry_path(monkeypatch):
    _env(monkeypatch)
    _db(sac, monkeypatch, _clsrow(PURGE, breaker_tripped=True))
    w = _EdgeWorld(sac)
    body, code = sac.edge_purge("/pricing", confirm=False, fetch=w,
                                purger=(w.purge, True), sleep=lambda s: None)
    assert code == 409 and "breaker" in body["refused"] and w.calls == []


def test_the_global_switch_off_refuses_confirm(monkeypatch):
    _env(monkeypatch, enabled=None)
    _db(sac, monkeypatch, _clsrow(PURGE))
    w = _EdgeWorld(sac)
    body, code = sac.edge_purge("/pricing", confirm=True, fetch=w,
                                purger=(w.purge, True), sleep=lambda s: None)
    assert code == 409 and "ACTION_CLASSES_ENABLED" in body["refused"] and w.purged == []


def test_an_unreadable_registry_refuses_both_wrappers(monkeypatch):
    _env(monkeypatch)
    _db(sac, monkeypatch, _clsrow(PURGE), registry_raises=True)
    w = _EdgeWorld(sac)
    body, code = sac.edge_purge("/pricing", confirm=False, fetch=w,
                                purger=(w.purge, True), sleep=lambda s: None)
    assert code == 409 and "registry unreadable" in body["refused"]
    body, code = sac.refresh_job("news_refresh", confirm=False, fetch=_NewsWorld(sac))
    assert code == 409 and "registry unreadable" in body["refused"]


def test_an_unreadable_cron_ledger_refuses_the_job(monkeypatch):
    _env(monkeypatch)
    cur = _db(sac, monkeypatch, _clsrow(FRESH))
    cur.raise_on = "FROM cron_last_run"
    w = _NewsWorld(sac)
    body, code = sac.refresh_job("news_refresh", confirm=True, fetch=w)
    assert code == 409 and "cron_last_run unreadable" in body["refused"]
    assert w.triggers == []


@pytest.mark.parametrize("path", ["/evil", "/api/v1/dcpi/scores?limit=1",
                                  "/mcp#workos-oauth-challenge", ""])
def test_a_path_off_the_allowlist_is_400_before_any_read(monkeypatch, path):
    monkeypatch.setattr(sac, "_conn", lambda: (_ for _ in ()).throw(
        AssertionError("validated before the registry is even read")))
    body, code = sac.edge_purge(path, confirm=True, purger=(lambda u: 1 / 0, True))
    assert code == 400
    body, code = sac.read_sentinel_unhealthy(path)
    assert code == 400 and body["unhealthy"] is None


@pytest.mark.parametrize("job", ["news-refresh", "rm_rf", "", "NEWS_REFRESH"])
def test_a_job_off_the_fixed_set_is_400(monkeypatch, job):
    monkeypatch.setattr(sac, "_conn", lambda: (_ for _ in ()).throw(
        AssertionError("validated before the registry is even read")))
    assert sac.refresh_job(job, confirm=True)[1] == 400
    assert sac.read_freshness_breaches(job)[1] == 400


# ══════════════════════════════════════════════════════════════════════════
#  5 · the probe: the module's OWN guard against a dry run that acts
# ══════════════════════════════════════════════════════════════════════════

def _probe(mod, monkeypatch, cls):
    _env(monkeypatch)
    cur = _db(mod, monkeypatch, _clsrow(cls, granted=False), rows=[_qrow(cls)],
              cron=_CRON_IDLE)
    w = _EdgeWorld(mod) if cls == PURGE else _NewsWorld(mod)
    res = mod.probe_one(_Conn(cur), cur, _clsrow(cls, granted=False), _qrow(cls),
                        fetch=w)
    trips = [s for s, _ in cur.calls if "SET breaker_tripped = TRUE" in s]
    return res, trips, w


@pytest.mark.parametrize("cls", [PURGE, FRESH])
def test_a_probe_of_the_real_wrapper_is_clean(monkeypatch, cls):
    res, trips, w = _probe(sac, monkeypatch, cls)
    assert res["outcome"] == "probe_clean" and res["clean"] is True and trips == []
    posts = [p for m, p in w.calls if m == "POST"]
    assert posts == [(PURGE_ACT + "?path=/pricing") if cls == PURGE
                     else (FRESH_ACT + "?job=news_refresh")], "the probe calls the DRY url"


@pytest.mark.parametrize("cls,mutation", [
    (PURGE, ("    if not confirm:\n        out.update(would_purge=[url],",
             "    if False:\n        out.update(would_purge=[url],")),
    (FRESH, ("    if not confirm:\n        out[\"note\"] = \"dry run — add ?confirm=1 to run the job once\"",
             "    if False:\n        out[\"note\"] = \"dry run — add ?confirm=1 to run the job once\"")),
])
def test_MUTATION_a_wrapper_that_acts_without_confirm_trips_the_breaker_on_its_first_probe(
        monkeypatch, cls, mutation):
    """Defence in depth for the dry-run rule: if it ever breaks, the drain's
    own probe sees the metric DROP across a no-confirm call and trips the
    class breaker before a human has granted anything."""
    res, trips, _ = _probe(_mutant(mutation), monkeypatch, cls)
    assert res["outcome"] == "probe_MUTATED" and trips, res


# ══════════════════════════════════════════════════════════════════════════
#  6 · the verifiers: a top-level int, the owner's own rule, UNMEASURED ≠ 0
# ══════════════════════════════════════════════════════════════════════════

def _sentinel_row(path="/pricing", **kw):
    d = {"path": path, "category": "high", "label": "Pricing", "status_code": 502,
         "bytes": 120, "healthy": False, "reason": "http_status:502",
         "checked_at": "2026-09-23T10:00:00+00:00", "last_healthy_at": None,
         "stale_days": None, "data_age_src": None, "cf_cache_status": "HIT"}
    d.update(kw)
    return d


def _sentinel(monkeypatch, rows):
    from routes import site_sentinel as ss
    if isinstance(rows, Exception):
        def boom():
            raise rows
        monkeypatch.setattr(ss, "latest_results", boom)
    else:
        monkeypatch.setattr(ss, "latest_results", lambda: list(rows))
    return ss


@pytest.mark.parametrize("row,expected", [
    (_sentinel_row(), 1),
    (_sentinel_row(healthy=True, reason="ok", status_code=200), 0),
    # the sentinel's OWN rule, not a restatement: a normal-category thin page
    # is not a finding, and nav/stale rows are other issue types
    (_sentinel_row(category="normal", reason="body_too_small:120"), 0),
    (_sentinel_row(reason="nav_missing"), 0),
    (_sentinel_row(reason="stale:3d"), 0),
])
def test_the_sentinel_verifier_counts_the_sentinel_s_own_finding(monkeypatch, row, expected):
    _sentinel(monkeypatch, [row])
    body, code = sac.read_sentinel_unhealthy("/pricing")
    assert code == 200 and body["ok"] is True and body["unhealthy"] == expected
    assert isinstance(body["unhealthy"], int) and not isinstance(body["unhealthy"], bool)


def test_the_sentinel_verifier_is_not_blinded_by_the_16_cap(monkeypatch):
    """unhealthy_findings() caps at 16; a path sorted past the cap reads as
    healthy there. The verifier judges ONE row, so it still counts it."""
    rows = [_sentinel_row(path=f"/a{i:02d}") for i in range(20)] + [_sentinel_row(path="/pricing")]
    ss = _sentinel(monkeypatch, rows)
    capped = [f["issue"] for f in ss.unhealthy_findings()]
    assert "site_sentinel_unhealthy:/pricing" not in capped, "control: the cap does cut it"
    assert sac.read_sentinel_unhealthy("/pricing")[0]["unhealthy"] == 1


@pytest.mark.parametrize("rows", [[], [_sentinel_row(path="/news")], RuntimeError("db down")])
def test_no_stored_row_or_an_unreadable_sentinel_is_UNMEASURED_not_zero(monkeypatch, rows):
    _sentinel(monkeypatch, rows)
    body, code = sac.read_sentinel_unhealthy("/pricing")
    assert code == 200 and body["ok"] is False and body["unhealthy"] is None
    assert "UNMEASURED" in body["error"]


def test_the_sentinel_verifier_never_scans(monkeypatch):
    from routes import site_sentinel as ss
    _sentinel(monkeypatch, [])
    monkeypatch.setattr(ss, "scan_all", lambda: (_ for _ in ()).throw(
        AssertionError("a verifier read must never run the 100-page sweep")))
    assert sac.read_sentinel_unhealthy("/pricing")[0]["unhealthy"] is None


def _news_cur(age_h=None, dtype="timestamp with time zone", exists=True):
    now = _dt.datetime.now(_dt.timezone.utc)
    last = None if age_h is None else now - _dt.timedelta(hours=age_h)
    return _Cur({
        "to_regclass": [("news_articles" if exists else None,)],
        "information_schema.columns": [(dtype,)],
        "SELECT MAX(published_at) FROM news_articles": [(last,)],
    })


@pytest.mark.parametrize("age_h,expected", [(8.0, 1), (1.0, 0)])
def test_the_freshness_verifier_is_the_radar_s_own_sla_row(monkeypatch, age_h, expected):
    cur = _news_cur(age_h)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, code = sac.read_freshness_breaches("news_refresh")
    assert code == 200 and body["ok"] is True and body["breaches"] == expected
    assert any("SELECT MAX(published_at) FROM news_articles" in s for s, _ in cur.calls)


def test_the_sla_hours_come_from_the_radar_not_from_this_module(monkeypatch):
    """Raise the radar's news SLA and the same 8h-old table reads fresh —
    the verifier restates neither the column nor the hours."""
    from routes import brain_consistency_radar as radar
    monkeypatch.setattr(radar, "SLAS", [
        (t, c, (10 if t == "news_articles" else h), lbl) for t, c, h, lbl in radar.SLAS])
    cur = _news_cur(8.0)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    assert sac.read_freshness_breaches("news_refresh")[0]["breaches"] == 0


@pytest.mark.parametrize("cur_kw", [dict(dtype="text"), dict(dtype=None), dict(exists=False)])
def test_an_unmeasurable_sla_row_is_UNMEASURED_not_zero(monkeypatch, cur_kw):
    cur = _news_cur(8.0, **cur_kw)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, _ = sac.read_freshness_breaches("news_refresh")
    assert body["ok"] is False and body["breaches"] is None and "UNMEASURED" in body["error"]


def test_an_unreadable_database_is_UNMEASURED_not_zero(monkeypatch):
    monkeypatch.setattr(sac, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    body, _ = sac.read_freshness_breaches("news_refresh")
    assert body["breaches"] is None and body["ok"] is False


def test_a_job_table_missing_from_the_radar_is_UNMEASURED(monkeypatch):
    from routes import brain_consistency_radar as radar
    monkeypatch.setattr(radar, "SLAS", [r for r in radar.SLAS if r[0] != "news_articles"])
    body, _ = sac.read_freshness_breaches("news_refresh")
    assert body["breaches"] is None and "no radar SLA row" in body["error"]


def test_every_job_names_real_radar_tables_and_a_real_trigger_route():
    from routes import brain_consistency_radar as radar
    from routes.jobs_routes import jobs_bp
    import flask
    app = flask.Flask("t_jobs")
    app.register_blueprint(jobs_bp)
    sla_tables = {r[0] for r in radar.SLAS}
    for job, j in sac._REFRESH_JOBS.items():
        assert set(j["tables"]) <= sla_tables, job
        method, path = j["trigger"]
        app.url_map.bind("localhost").match(path, method=method)   # raises if absent
        assert path.startswith("/api/jobs/") and j["cron_job"] == path.rsplit("/", 1)[1], (
            "cron_last_run is stamped under the /api/jobs/<name> segment")


# ══════════════════════════════════════════════════════════════════════════
#  7 · endpoints, grant test, graduation
# ══════════════════════════════════════════════════════════════════════════

def _app():
    import flask
    app = flask.Flask("t_ops")
    app.register_blueprint(sq.squasher_queue_bp)   # record_once wires sac
    return app


@pytest.mark.parametrize("method,path", [
    ("POST", PURGE_ACT + "?path=/pricing"), ("GET", PURGE_VER + "?path=/pricing"),
    ("POST", FRESH_ACT + "?job=news_refresh"), ("GET", FRESH_VER + "?job=news_refresh")])
def test_the_ops_endpoints_need_the_key_and_answer_404_on_the_kill_switch(monkeypatch, method, path):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    c = _app().test_client()
    assert c.open(path, method=method).status_code == 401
    monkeypatch.setenv("SQUASHER_QUEUE_DISABLE", "1")
    assert c.open(path, method=method, headers={"X-Admin-Key": "adm"}).status_code == 404


def test_the_verifier_endpoints_return_the_metric_as_a_top_level_int(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    _sentinel(monkeypatch, [_sentinel_row()])
    cur = _news_cur(8.0)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    c = _app().test_client()
    h = {"X-Admin-Key": "adm"}
    d = c.get(PURGE_VER + "?path=/pricing", headers=h).get_json()
    assert d["unhealthy"] == 1 and type(d["unhealthy"]) is int
    d = c.get(FRESH_VER + "?job=news_refresh", headers=h).get_json()
    assert d["breaches"] == 1 and type(d["breaches"]) is int


def test_the_generic_verifier_route_reads_the_ops_verifiers_with_the_row_param(monkeypatch):
    """GET /verifier/<class>?path= (the non-actuator fallback) proxies to the
    class row's verifier_url with the validated row parameter."""
    seen = []
    cur = _Cur({"FROM brain_action_classes WHERE class = %s": [_cls_tuple(_clsrow(PURGE))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, code = sac.read_class_verifier(PURGE, {"path": "/pricing"},
                                         fetch=lambda m, p: (seen.append((m, p)) or
                                                             (200, {"unhealthy": 1})))
    assert code == 200 and body["unhealthy"] == 1
    assert seen == [("GET", PURGE_VER + "?path=/pricing")]
    assert sac.read_class_verifier(PURGE, {"path": "/evil"})[1] == 400


@pytest.mark.parametrize("cls", [PURGE, FRESH])
def test_the_registry_row_passes_the_grant_test_so_the_owner_CAN_grant(cls):
    ok, why = sac.grant_allowed(_clsrow(cls, granted=False))
    assert ok, why


def test_both_new_classes_are_seeded_granted_FALSE():
    cur = _Cur()
    sac.ensure_tables(cur)
    seeds = {p[0]: (s, p) for s, p in cur.calls
             if s.startswith("INSERT INTO brain_action_classes")}
    for cls in (PURGE, FRESH):
        sql, p = seeds[cls]
        assert ", FALSE," in sql and "ON CONFLICT (class) DO NOTHING" in sql
        assert p[1] is True and p[2] == sac.ACTION_CLASSES[cls]["verifier_url"]
        assert json.loads(p[3]) == {"confirm": "1"}


def test_graduation_PROPOSES_an_eligible_ops_class_and_never_grants(monkeypatch):
    filed = []
    fake = types.ModuleType("routes.squasher_queue")
    fake.file_decision_row = lambda cur, **kw: (filed.append(kw) or
                                                {"ok": True, "id": 7, "status": "awaiting_decision",
                                                 "created": True})
    monkeypatch.setitem(sys.modules, "routes.squasher_queue", fake)
    row = _clsrow(PURGE, granted=False)
    cur = _Cur({
        "FROM brain_action_classes ORDER BY class": [_cls_tuple(row)],
        "GROUP BY class": [(PURGE, 3, 3, 0, 0, None)],
        "WHERE finding_key = ANY(%s)": [],
    })
    out = sac._graduation(cur, file=True)
    assert out["eligible"] == [PURGE] and len(filed) == 1
    assert filed[0]["finding_key"] == f"action-class-grant:{PURGE}"
    assert not [s for s, _ in cur.calls if "SET granted" in s], "graduation granted something"
