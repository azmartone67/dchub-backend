"""tests/test_squasher_open_identity.py — ONE open row per finding (2026-08-22).

THE DEFECT, AS MEASURED (GET /api/v1/brain/squasher/inbox, 2026-08-22 05:25Z):
19 rows of action_class=facility_dedup_apply for 7 countries. ids 241 and 256
are both finding_key='/api/v1/admin/facility-dedup/analyze?country=FR', both
awaiting_ops, filed 04:25Z and 23:36Z — the key is byte-identical across
re-files. The open-row guard (partial unique index + enqueue()'s "already"
lookup) defined OPEN as queued|running, so the moment a row was handed to a
human (awaiting_ops / awaiting_decision) its finding was invisible to the
guard and the next re-file inserted a fresh row.

What is proved here, as BEHAVIOUR against a params-aware cursor stub:
  · a re-file of a finding with an open row in ANY open status REFRESHES
    that row (seen_count, last_seen) and inserts nothing — and is not
    budget-gated, because it costs nothing;
  · CONTROL: a different country (a different key) still inserts its own
    row, and a brand-new finding is still budget-gated;
  · the v2 index names every open status and is SAVEPOINT-guarded, so the
    live duplicates cannot abort a caller's transaction before the collapse;
  · collapse_duplicate_open_rows keeps the OLDEST open row per finding,
    marks the rest 'superseded' with a note naming the keeper, never touches
    a 'running' row, is idempotent, isolates each group in a savepoint, and
    writes nothing on dry_run;
  · the drain collapses before it reclaims and reports the count; the admin
    endpoint is gated, one-shot, and honours the kill switch;
  · the Step-2 action-class drain treats a verifier pre-count of 0 as
    noop_clean (ledger row executed=False, breaker untouched, row resolved),
    never as a failure.

House rules: no DB, never import main, nothing runs at module scope.
Mutation-verified — the PR description names each mutation and the test it
failed.

Run:  python3 -m pytest tests/test_squasher_open_identity.py -v
"""
from __future__ import annotations

import datetime as _dt
import inspect

import pytest

from routes import squasher_action_classes as sac
from routes import squasher_queue as sq
from tests.test_squasher_action_classes import (  # noqa: F401  (claim_ledger is a fixture)
    _Fetch, _class_update, _harness, _queue_updates, _resolved_ids, claim_ledger)

FR_KEY = "/api/v1/admin/facility-dedup/analyze?country=FR"
AU_KEY = "/api/v1/admin/facility-dedup/analyze?country=AU"
GB_KEY = "/api/v1/admin/facility-dedup/analyze?country=GB"
APPLY_FR = "/api/v1/admin/facility-dedup/apply?country=FR&confirm=1"
UTC = _dt.timezone.utc
T1 = _dt.datetime(2026, 8, 21, 4, 25, 15, tzinfo=UTC)     # id 241, live
T2 = _dt.datetime(2026, 8, 21, 23, 36, 25, tzinfo=UTC)    # id 256, live
T3 = _dt.datetime(2026, 8, 21, 23, 36, 22, tzinfo=UTC)    # id 251, live

LOOKUP = "WHERE finding_key = %s AND status IN"
COLLAPSE_SELECT = "COALESCE(NULLIF(action_url, ''), finding_key)"
SUPERSEDE = "UPDATE squasher_work_queue SET status = 'superseded'"
KEEPER = "UPDATE squasher_work_queue SET seen_count = COALESCE(seen_count, 1) + %s"
REFRESH = "UPDATE squasher_work_queue SET seen_count = COALESCE(seen_count, 1) + 1"


# ── stubs ─────────────────────────────────────────────────────────────────

class _Cur:
    """Records every execute; answers fetches from a script of
    (sql_substring, params_or_None, rows). Params-aware ON PURPOSE: the
    control case below is decided by the VALUE the guard looked up, not by
    which statement ran."""

    def __init__(self, answers=(), raise_when=None):
        self.answers = list(answers)
        self.calls = []
        self.rowcount = 1
        self.raise_when = raise_when
        self._last = ("", None)

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        self._last = (flat, params)
        if self.raise_when and self.raise_when(flat, params):
            raise RuntimeError("boom")

    def _rows(self):
        flat, params = self._last
        for sub, p, rows in self.answers:
            if sub in flat and (p is None or p == params):
                return rows
        return []

    def fetchone(self):
        r = self._rows()
        return r[0] if r else None

    def fetchall(self):
        return list(self._rows())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sqls(self, prefix=""):
        return [s for s, _ in self.calls if s.startswith(prefix)]

    def find(self, sub):
        return [(s, p) for s, p in self.calls if sub in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, cur):
    conn = _Conn(cur)
    monkeypatch.setattr(sq, "_conn", lambda: conn)
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    # the claim ledger is a fail-soft side effect of a NEW row; keep it out
    monkeypatch.setattr(sq, "_register_fix_claim", lambda *a, **k: None)
    return conn


def _open_row(id_, key, status, ts, seen=1, action_url=None, last_seen=None):
    # the collapse SELECT's shape: id, finding_key, status, requested_at,
    # seen_count, last_seen-or-requested_at, action_url-or-finding_key
    return (id_, key, status, ts, seen, last_seen or ts, action_url or key)


def _collapse_cur(rows, **kw):
    return _Cur([(COLLAPSE_SELECT, None, rows)], **kw)


def _superseded(cur):
    return [(p[0], p[1]) for s, p in cur.calls if s.startswith(SUPERSEDE)]


def _keeper_updates(cur):
    return [p for s, p in cur.calls if s.startswith(KEEPER)]


def _app():
    import flask
    app = flask.Flask("t")
    app.register_blueprint(sq.squasher_queue_bp)
    return app


# ══════════════════════════════════════════════════════════════════════════
#  1 · the guard: a re-file refreshes, it does not insert
# ══════════════════════════════════════════════════════════════════════════

def test_a_refile_of_a_finding_waiting_on_a_human_refreshes_its_open_row(monkeypatch):
    """ids 241/256: the second filing must land ON 241, not beside it."""
    cur = _Cur([(LOOKUP, (FR_KEY,), [(241, "awaiting_ops")]),
                ("RETURNING COALESCE(seen_count, 1)", None, [(2,)]),
                ("SELECT COUNT(*) FILTER", None, [(12, 40)])])   # both caps HIT
    conn = _wire(monkeypatch, cur)
    out = sq.enqueue(FR_KEY, "facility_duplicates_unmarked", "operator")
    assert out == {"ok": True, "already": True, "refreshed": True, "id": 241,
                   "status": "awaiting_ops", "seen_count": 2}
    assert not cur.sqls("INSERT"), "a re-file inserted a second open row"
    ups = [p for s, p in cur.calls if s.startswith(REFRESH)]
    assert len(ups) == 1 and ups[0][-1] == 241
    assert not cur.find("COUNT(*) FILTER"), (
        "a refresh costs no investigation and no PR — it must not be "
        "budget-gated (the caps above are at their limit and it still landed)")
    assert conn.commits == 1


@pytest.mark.parametrize("status", sq._OPEN_STATUSES)
def test_every_open_status_is_refreshed_not_duplicated(monkeypatch, status):
    cur = _Cur([(LOOKUP, (FR_KEY,), [(241, status)]),
                ("RETURNING COALESCE(seen_count, 1)", None, [(3,)])])
    _wire(monkeypatch, cur)
    out = sq.enqueue(FR_KEY, "t")
    assert out["already"] and out["status"] == status and out["seen_count"] == 3
    assert not cur.sqls("INSERT")


def test_control_a_different_country_still_inserts_its_own_row(monkeypatch):
    """The decision is keyed by VALUE: AU has no open row and gets one;
    FR has one and gets refreshed — in the same queue, same stub."""
    cur = _Cur([(LOOKUP, (FR_KEY,), [(241, "awaiting_ops")]),
                (LOOKUP, (AU_KEY,), []),
                ("RETURNING COALESCE(seen_count, 1)", None, [(2,)]),
                ("SELECT COUNT(*) FILTER", None, [(0, 0)]),
                ("RETURNING id", None, [(300,)])])
    _wire(monkeypatch, cur)
    au = sq.enqueue(AU_KEY, "facility_duplicates_unmarked", "operator")
    # ★2026-08-29 lane 7 widened this return ADDITIVELY: `remit` names whether
    # the finding falls in the wiring class this lane can close, and
    # prior_refutations/_known carry what the claim ledger knows about earlier
    # attempts (known=False here — no ledger in a unit test, and an unread
    # history must never read as a clean one). Kept as exact equality rather
    # than relaxed to a subset check: the strictness is the point of this test.
    assert au == {"ok": True, "id": 300, "status": "queued",
                  "remit": "unclassified", "prior_refutations": 0,
                  "prior_refutations_known": False}
    ins = [p for s, p in cur.calls if s.startswith("INSERT INTO squasher_work_queue")]
    assert len(ins) == 1 and ins[0][0] == AU_KEY
    fr = sq.enqueue(FR_KEY, "facility_duplicates_unmarked", "operator")
    assert fr["already"] and fr["id"] == 241
    assert len(cur.sqls("INSERT INTO squasher_work_queue")) == 1, "FR inserted too"


def test_a_new_row_stamps_last_seen_and_a_new_finding_is_still_budget_gated(monkeypatch):
    cur = _Cur([(LOOKUP, None, []), ("SELECT COUNT(*) FILTER", None, [(0, 0)]),
                ("RETURNING id", None, [(301,)])])
    _wire(monkeypatch, cur)
    assert sq.enqueue(GB_KEY, "t")["id"] == 301
    ins = cur.sqls("INSERT INTO squasher_work_queue")[0]
    assert "last_seen" in ins and "NOW()" in ins
    capped = _Cur([(LOOKUP, None, []), ("SELECT COUNT(*) FILTER", None, [(12, 12)])])
    _wire(monkeypatch, capped)
    out = sq.enqueue(GB_KEY, "t")
    assert out["ok"] is False and out["error"] == "daily_cap"
    assert not capped.sqls("INSERT"), "the cap no longer gates a new finding"


def test_the_open_row_lookup_and_the_v2_index_name_every_open_status(monkeypatch):
    """The testable half of a guard that lives in SQL (is_stale_running's
    argument): a stub serves whatever rows it is handed, so the statement
    TEXT is what pins the predicate."""
    assert sq._OPEN_STATUSES == ("queued", "running", "awaiting_ops", "awaiting_decision")
    cur = _Cur([("SELECT COUNT(*) FILTER", None, [(0, 0)]),
                ("RETURNING id", None, [(1,)])])
    _wire(monkeypatch, cur)
    sq.enqueue(FR_KEY, "t")
    lookup = cur.find(LOOKUP)[0][0]
    ddl = [s for s in cur.sqls("CREATE UNIQUE INDEX") if sq._OPEN_INDEX in s]
    assert len(ddl) == 1, "the v2 open-row index is not created on the enqueue path"
    for s in ("queued", "running", "awaiting_ops", "awaiting_decision"):
        assert f"'{s}'" in lookup, f"lookup ignores {s}: a {s} row can be duplicated"
        assert f"'{s}'" in ddl[0], f"index ignores {s}: a {s} row can be duplicated"
    assert "finding_key = %s" in lookup
    assert "ORDER BY requested_at ASC, id ASC" in lookup, "refresh must land on the OLDEST open row"


# ══════════════════════════════════════════════════════════════════════════
#  2 · the index cannot take the queue down while duplicates exist
# ══════════════════════════════════════════════════════════════════════════

def test_a_failing_index_create_rolls_back_to_its_savepoint_and_the_caller_continues():
    cur = _Cur(raise_when=lambda s, p: sq._OPEN_INDEX in s)
    assert sq._ensure_open_index(cur) is False
    i = next(k for k, (s, _) in enumerate(cur.calls) if sq._OPEN_INDEX in s)
    assert cur.calls[i - 1][0] == "SAVEPOINT sq_open_idx"
    assert cur.calls[i + 1][0] == "ROLLBACK TO SAVEPOINT sq_open_idx"
    # _ensure_table — every enqueue and every read calls it — survives it,
    # and the v1 guard is still put in place before the v2 attempt
    cur2 = _Cur(raise_when=lambda s, p: sq._OPEN_INDEX in s)
    sq._ensure_table(cur2)
    v1 = [s for s in cur2.sqls("CREATE UNIQUE INDEX IF NOT EXISTS squasher_queue_open_uniq ")]
    assert v1 and "('queued', 'running')" in v1[0]
    assert "ROLLBACK TO SAVEPOINT sq_open_idx" in [s for s, _ in cur2.calls]


def test_a_successful_index_create_releases_its_savepoint():
    cur = _Cur()
    assert sq._ensure_open_index(cur) is True
    assert [s for s, _ in cur.calls] == [
        "SAVEPOINT sq_open_idx", " ".join(sq._OPEN_INDEX_DDL.split()),
        "RELEASE SAVEPOINT sq_open_idx"]


def test_ensure_table_adds_the_bookkeeping_columns():
    cur = _Cur()
    sq._ensure_table(cur)
    ddl = " ".join(cur.sqls())
    assert "ADD COLUMN IF NOT EXISTS seen_count INTEGER NOT NULL DEFAULT 1" in ddl
    assert "ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ" in ddl


# ══════════════════════════════════════════════════════════════════════════
#  3 · the collapse: keep the oldest, supersede the rest
# ══════════════════════════════════════════════════════════════════════════

def test_collapse_keeps_the_oldest_open_row_per_finding_and_supersedes_the_rest():
    # handed out of order on purpose: oldest is by TIME, not list position
    cur = _collapse_cur([_open_row(256, FR_KEY, "awaiting_ops", T2),
                         _open_row(241, FR_KEY, "awaiting_ops", T1),
                         _open_row(251, AU_KEY, "awaiting_ops", T3)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["groups"] == 1 and out["kept"] == [241] and out["superseded"] == [256]
    sup = _superseded(cur)
    assert [row_id for _, row_id in sup] == [256]
    assert "superseded by #241" in sup[0][0]
    keep = _keeper_updates(cur)
    assert len(keep) == 1
    assert keep[0][0] == 1 and keep[0][1] == T2 and keep[0][-1] == 241, (
        "the keeper must absorb the duplicate's seen_count and last_seen")
    assert not [p for s, p in cur.calls if s.startswith("UPDATE") and p[-1] == 251], (
        "AU has one open row and must not be touched")
    assert out["index_ready"] is True and cur.find(sq._OPEN_INDEX), (
        "the v2 index must be attempted once the way is clear")
    assert out["detail"] == [{"key": FR_KEY, "keep": 241, "keep_status": "awaiting_ops",
                              "supersede": [256]}]


def test_collapse_is_idempotent_on_a_clean_queue():
    cur = _collapse_cur([_open_row(241, FR_KEY, "awaiting_ops", T1),
                         _open_row(251, AU_KEY, "awaiting_ops", T3)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["groups"] == 0 and out["superseded"] == [] and out["kept"] == []
    assert not cur.sqls("UPDATE")


def test_collapse_never_supersedes_a_running_row_but_a_running_row_may_keep():
    cur = _collapse_cur([_open_row(241, FR_KEY, "awaiting_ops", T1),
                         _open_row(300, FR_KEY, "running", T2)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["superseded"] == [] and not cur.sqls("UPDATE"), (
        "a running row is mid-investigate; _finish() would resurrect it")
    cur = _collapse_cur([_open_row(241, FR_KEY, "running", T1),
                         _open_row(256, FR_KEY, "awaiting_ops", T2)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["kept"] == [241] and out["superseded"] == [256]
    # and the UPDATE itself refuses to touch a running row, belt and braces
    assert "AND status <> 'running'" in _superseded_sql(cur)


def _superseded_sql(cur):
    return next(s for s, _ in cur.calls if s.startswith(SUPERSEDE))


def test_collapse_dry_run_reports_the_plan_and_writes_nothing():
    cur = _collapse_cur([_open_row(241, FR_KEY, "awaiting_ops", T1),
                         _open_row(256, FR_KEY, "awaiting_ops", T2)])
    out = sq.collapse_duplicate_open_rows(cur, dry_run=True)
    assert out["dry_run"] and out["kept"] == [241] and out["superseded"] == [256]
    assert out["detail"][0]["supersede"] == [256]
    assert not cur.sqls("UPDATE") and not cur.sqls("CREATE") and out["index_ready"] is None


def test_collapse_groups_by_the_operator_action_when_the_keys_differ():
    """Two keys, one piece of work: the analyze URL the radar emits and the
    apply command an operator pasted both classify to apply?country=FR."""
    cur = _collapse_cur([_open_row(241, FR_KEY, "awaiting_ops", T1, action_url=APPLY_FR),
                         _open_row(270, "POST " + APPLY_FR, "awaiting_ops", T2,
                                   action_url=APPLY_FR)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["kept"] == [241] and out["superseded"] == [270]


def test_collapse_isolates_each_group_in_a_savepoint():
    """The #2866 shape: one bad row must not silently roll back the batch."""
    rows = [_open_row(241, FR_KEY, "awaiting_ops", T1),
            _open_row(256, FR_KEY, "awaiting_ops", T2),
            _open_row(238, GB_KEY, "awaiting_ops", T1),
            _open_row(253, GB_KEY, "awaiting_ops", T2)]
    cur = _collapse_cur(rows, raise_when=lambda s, p: s.startswith(SUPERSEDE) and p[1] == 256)
    out = sq.collapse_duplicate_open_rows(cur)          # must not raise
    assert out["kept"] == [238] and out["superseded"] == [253]
    assert out["errors"] and out["errors"][0].startswith("#241")
    seq = [s for s, _ in cur.calls]
    assert "ROLLBACK TO SAVEPOINT sq_collapse" in seq
    assert seq.count("RELEASE SAVEPOINT sq_collapse") == 1, "the GB group did not commit its savepoint"


def test_collapse_is_fail_soft_when_the_read_itself_fails():
    cur = _Cur(raise_when=lambda s, p: COLLAPSE_SELECT in s)
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["groups"] == 0 and out["error"].startswith("RuntimeError")
    assert "ROLLBACK TO SAVEPOINT sq_collapse_read" in [s for s, _ in cur.calls], (
        "a failed read must not poison the drain's transaction")


def test_a_superseded_row_keeps_its_analysis_and_names_its_keeper():
    cur = _collapse_cur([_open_row(241, FR_KEY, "awaiting_ops", T1),
                         _open_row(256, FR_KEY, "awaiting_ops", T2, seen=2)])
    sq.collapse_duplicate_open_rows(cur)
    sql = _superseded_sql(cur)
    assert "analysis" not in sql and "DELETE" not in sql, "only status/reason/finished_at move"
    assert "COALESCE(reason, '')" in sql, "the old reason is kept behind the note"
    assert _keeper_updates(cur)[0][0] == 2, "the keeper absorbs the duplicate's seen_count"


# ══════════════════════════════════════════════════════════════════════════
#  4 · wiring: the drain, the reclaimer, the statuses, the reads
# ══════════════════════════════════════════════════════════════════════════

def test_the_drain_collapses_before_it_reclaims_and_reports_the_count(monkeypatch):
    cur = _Cur([(COLLAPSE_SELECT, None, [_open_row(241, FR_KEY, "awaiting_ops", T1),
                                         _open_row(256, FR_KEY, "awaiting_ops", T2)])])
    _wire(monkeypatch, cur)
    monkeypatch.setattr(sq, "_action_classes_step",
                        lambda dry_run=False: {"ok": True, "enabled": False})
    out = sq.drain(limit=1)
    assert out["ok"] and out["collapsed"] == 1 and out["processed"] == 0
    seq = [s for s, _ in cur.calls]
    i_sup = next(k for k, s in enumerate(seq) if s.startswith(SUPERSEDE))
    i_rec = next(k for k, s in enumerate(seq) if "q.status = 'refused'" in s)
    i_sel = next(k for k, s in enumerate(seq) if "WHERE status='queued'" in s)
    assert i_sup < i_rec < i_sel, "collapse must run before reclaim, both before selection"


def test_reclaim_will_not_reopen_a_finding_whose_twin_waits_on_a_human():
    cur = _Cur()
    sq.reclaim_misfiled(cur)
    select = cur.find("q.status = 'refused'")[0][0].lower()
    tail = select[select.index("not exists"):]
    for s in ("'queued'", "'running'", "'awaiting_ops'", "'awaiting_decision'"):
        assert s in tail, f"reclaim would re-open a finding with a {s} twin"


def test_superseded_is_declared_and_is_closed_not_open():
    assert "superseded" in sq.STATUSES
    assert "superseded" not in sq._OPEN_STATUSES
    assert "superseded" not in sq._NONMECHANICAL_STATUSES, "it must never show in the inbox"


def test_convergence_does_not_count_a_superseded_row_as_a_closed_verdict():
    closed = sq._CONVERGENCE_SQL[: sq._CONVERGENCE_SQL.index("recurred AS")]
    assert "status <> 'superseded'" in closed


def test_the_stability_shell_terminal_mix_ignores_superseded_rows():
    from routes import stability_master_shell as sms
    src = inspect.getsource(sms._lane_detector_precision)
    assert "status <> 'superseded'" in src, (
        "a merged duplicate would count as 'another terminal outcome' and "
        "green the refusal-only check for free")


def test_queue_and_inbox_reads_carry_seen_count_and_last_seen(monkeypatch):
    row17 = (241, FR_KEY, "facility_duplicates_unmarked", "operator", "awaiting_ops",
             "reason", None, T1, T1, None, None, None,
             "facility_dedup_apply", APPLY_FR, "POST", 3, T2)
    cur = _Cur([("ORDER BY requested_at DESC LIMIT", None, [row17])])
    _wire(monkeypatch, cur)
    rows = sq.queue_rows(5)
    assert rows[0]["seen_count"] == 3 and rows[0]["last_seen"] == T2.isoformat()
    row15 = (241, FR_KEY, "facility_duplicates_unmarked", "awaiting_ops", "reason",
             0.4, "analysis", "decision", T1, T1,
             "facility_dedup_apply", APPLY_FR, "POST", 3, T2)
    cur2 = _Cur([("ORDER BY finished_at DESC NULLS LAST", None, [row15])])
    _wire(monkeypatch, cur2)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    rv = _app().test_client().get("/api/v1/brain/squasher/inbox",
                                  headers={"X-Admin-Key": "adm"})
    d = rv.get_json()
    assert rv.status_code == 200 and d["rows"][0]["seen_count"] == 3
    assert d["rows"][0]["last_seen"] == T2.isoformat()


def test_the_portal_shows_how_many_times_an_open_row_was_seen():
    from routes import squasher_portal as sp
    html = sp._queue_html({"queue": [{"title": "f", "status": "awaiting_ops",
                                      "reason": "r", "seen_count": 3}]})
    assert "×3" in html
    html1 = sp._queue_html({"queue": [{"title": "f", "status": "awaiting_ops",
                                       "reason": "r", "seen_count": 1}]})
    assert "×" not in html1
    html0 = sp._queue_html({"queue": [{"title": "f", "status": "queued", "reason": None}]})
    assert "×" not in html0, "a row without the column renders as before"


# ══════════════════════════════════════════════════════════════════════════
#  5 · the endpoint
# ══════════════════════════════════════════════════════════════════════════

def test_collapse_endpoint_is_admin_gated_and_one_shot(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    cur = _Cur([(COLLAPSE_SELECT, None, [_open_row(241, FR_KEY, "awaiting_ops", T1),
                                         _open_row(256, FR_KEY, "awaiting_ops", T2)])])
    conn = _wire(monkeypatch, cur)
    c = _app().test_client()
    assert c.post("/api/v1/brain/squasher/collapse-duplicates").status_code == 401
    assert not cur.calls, "an unauthenticated call touched the database"
    h = {"X-Admin-Key": "adm"}
    rv = c.post("/api/v1/brain/squasher/collapse-duplicates?dry_run=1", headers=h)
    d = rv.get_json()
    assert rv.status_code == 200 and d["ok"] and d["dry_run"] and d["superseded"] == [256]
    assert not _superseded(cur), "dry_run wrote"
    rv = c.post("/api/v1/brain/squasher/collapse-duplicates", headers=h)
    d = rv.get_json()
    assert rv.status_code == 200 and d["ok"] and not d["dry_run"]
    assert d["superseded"] == [256] and d["kept"] == [241] and d["index_ready"] is True
    assert [i for _, i in _superseded(cur)] == [256] and conn.commits >= 1
    assert rv.headers["Cache-Control"].startswith("no-store")


def test_collapse_endpoint_honours_the_kill_switch(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.setenv("SQUASHER_QUEUE_DISABLE", "1")
    cur = _Cur()
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    rv = _app().test_client().post("/api/v1/brain/squasher/collapse-duplicates",
                                   headers={"X-Admin-Key": "adm"})
    assert rv.status_code == 200 and rv.get_json().get("skipped") and not cur.calls


def test_collapse_endpoint_reports_a_failure_as_500_not_a_clean_run(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)

    def _broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(sq, "_conn", _broken)
    rv = _app().test_client().post("/api/v1/brain/squasher/collapse-duplicates",
                                   headers={"X-Admin-Key": "adm"})
    assert rv.status_code == 500 and rv.get_json()["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
#  6 · Step-2 drain: a pre-count of 0 is a clean no-op, never a failure
# ══════════════════════════════════════════════════════════════════════════

def test_a_zero_pre_count_is_noop_clean_never_a_failure(monkeypatch, claim_ledger):
    """A collapsed/already-applied finding reads duplicate_rows=0 before any
    action. That is 'nothing to do' — the row resolves, the ledger records
    executed=False/noop_clean, and neither runs_failed nor the breaker moves.
    The failure branch would be reached only by mutating `pre <= 0`."""
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(0, 0))
    out = sac.run_granted_actions(fetch=fetch)
    res = out["results"][0]
    assert res["outcome"] == "noop_clean" and res["pre_count"] == 0
    assert res["executed"] is False and not str(res["outcome"]).startswith("failed")
    assert fetch.posts == [] and out["ran"] == 0
    ledger = [p for s, p in cur.calls if s.startswith("INSERT INTO brain_action_class_runs")]
    assert len(ledger) == 1
    # (class, queue_id, params, action_url, verifier_url, pre_count, executed, outcome, …)
    assert ledger[0][5] == 0 and ledger[0][6] is False and ledger[0][7] == "noop_clean"
    assert _class_update(cur) is None, "a clean no-op must not move runs_failed or the breaker"
    assert _resolved_ids(cur) == [218]
    note = [p[0] for s, p in _queue_updates(cur) if "'resolved'" in s][0]
    assert "no longer holds" in note
    assert claim_ledger["register"] == [], "no claim for a run that never started"
