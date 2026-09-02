"""Squasher classification closes the gap the 2026-09-02 brain-agents sweep
measured (finding 4): 0/46 inbox rows carried an action_class, so the two
autonomy-shell classes could never be selected — news_entity_reresolve
stood at runs_ok=0 while its verifier read blindspot=7.

 (a) enqueue classifies with the ONE rule (classify_row): endpoint first,
     then the shell check ids the key/title carry;
 (b) match_keys on the two class-scoped classes (actuator id + graph-spine
     check id), whole-token, key/title only — never prose;
 (c) the drain runs a bounded (50) classify-all BEFORE the action step, as a
     pure tag; the grant gate stays the first read of the action step;
 (d) GET /verifier/<class> for a non-actuator class reads the class row's
     verifier_url instead of answering 404.
"""
from __future__ import annotations

import inspect

import pytest

sac = pytest.importorskip("routes.squasher_action_classes")
sq = pytest.importorskip("routes.squasher_queue")

NEWS = "news_entity_reresolve"
DEALS = "deals_exact_dupe_quarantine"
FAC = "facility_dedup_apply"


class _Cur:
    def __init__(self, answers=None, rowcount=1):
        self.answers = answers or {}
        self.calls = []
        self.rowcount = rowcount
        self._last = ""

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        self._last = flat

    def _rows(self):
        for key, rows in self.answers.items():
            if key in self._last:
                return rows() if callable(rows) else rows
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

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sqls(cur, needle):
    return [(s, p) for s, p in cur.calls if needle in s]


# ── (b) the key rule ────────────────────────────────────────────────────────
def test_the_shell_check_ids_map_to_their_classes():
    assert sac.classify_key("graph_spine:es_blindspot", None)["action_class"] == NEWS
    assert sac.classify_key("graph_spine:deal_dupes", "")["action_class"] == DEALS
    assert sac.classify_key(None, "news_entity_reresolve — defect present, not fired")["action_class"] == NEWS
    assert sac.classify_key("brain_autonomy:deals_exact_dupe_quarantine", None)["action_class"] == DEALS


def test_the_key_rule_builds_the_registry_url_never_the_text():
    c = sac.classify_key("graph_spine:es_blindspot", None)
    assert c["action_url"] == sac.build_action_url(NEWS, {})
    assert c["action_method"] == "POST" and c["params"] == {}


def test_a_row_parameter_class_is_never_key_matched():
    assert sac.classify_key("facility_dedup_apply", "facility_dedup_apply US") is None
    assert FAC not in sac._KEY_TO_CLASS


def test_whole_tokens_only_and_never_prose():
    assert sac.classify_key("deal_dupes_v2", None) is None
    assert sac.classify_key("es_blindspots", None) is None
    prose = "the analysis mentions deal_dupes and es_blindspot in passing"
    assert sac.classify_row("heal:something_else", "some title", prose) is None


def test_classify_row_prefers_the_endpoint_rule():
    c = sac.classify_row("graph_spine:es_blindspot", "t",
                         "POST /api/v1/admin/facility-dedup/apply?country=SG")
    assert c["action_class"] == FAC and c["params"] == {"country": "SG"}


def test_every_match_key_is_on_a_class_scoped_class():
    for name, spec in sac.ACTION_CLASSES.items():
        if spec.get("match_keys"):
            assert spec["row_param"] is None, name
            for k in spec["match_keys"]:
                assert sac._KEY_TO_CLASS[k] == name


# ── (a) enqueue classifies with the ONE rule ────────────────────────────────
def _enqueue_harness(monkeypatch):
    cur = _Cur({
        "RETURNING id": [(900,)],
        "COUNT(*) FILTER": [(0, 0)],
    })
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    monkeypatch.setattr(sq, "_ensure_table", lambda cur: None)
    monkeypatch.setattr(sq, "_prior_refutations", lambda fk: {"known": False, "refuted": 0})
    monkeypatch.setattr(sq, "_register_fix_claim", lambda *a, **k: None)
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    return cur


def test_enqueue_tags_a_shell_finding_by_its_key(monkeypatch):
    """★ The measured gap: a row filed from the graph-spine lane names no
    endpoint, only its check id — it stayed unclassified forever."""
    cur = _enqueue_harness(monkeypatch)
    out = sq.enqueue("graph_spine:es_blindspot",
                     "es_blindspot — resolver has no known blind spot", "heal")
    assert out["ok"] is True and out["id"] == 900
    assert out["remit"] == "wiring", out
    ups = _sqls(cur, "SET action_class = %s")
    assert len(ups) == 1
    assert ups[0][1][0] == NEWS and ups[0][1][3] == 900


def test_enqueue_still_tags_by_endpoint(monkeypatch):
    cur = _enqueue_harness(monkeypatch)
    out = sq.enqueue("heal:facility_duplicates_unmarked:SG",
                     "POST /api/v1/admin/facility-dedup/apply?country=SG", "heal")
    assert out["remit"] == "wiring"
    assert _sqls(cur, "SET action_class = %s")[0][1][0] == FAC


def test_enqueue_leaves_an_unknown_finding_unclassified(monkeypatch):
    cur = _enqueue_harness(monkeypatch)
    out = sq.enqueue("heal:something_unregistered", "no endpoint here", "heal")
    assert out["remit"] == "unclassified"
    assert _sqls(cur, "SET action_class = %s") == []


def test_enqueue_uses_the_key_aware_path():
    src = inspect.getsource(sq.enqueue)
    assert "_classify_row_in_tx(cur, new_id, finding_key, title)" in src


# ── (c) the classify-all pass ───────────────────────────────────────────────
def test_the_drain_classifies_before_the_action_step_and_the_step_stays_first_gate():
    src = inspect.getsource(sq.drain)
    i_cls = src.index("_classify_all_open(50)")
    i_step = src.index("_action_classes_step()")
    i_sel = src.index("WHERE status='queued'")
    assert i_cls < i_step < i_sel
    step = inspect.getsource(sac.run_granted_actions)
    assert step.index("on = enabled()") < step.index("_conn()"), "grant gate is the first read"


def test_classify_all_is_bounded_commits_and_tags(monkeypatch):
    cur = _Cur({"action_class IS NULL ORDER BY id DESC": [
        (211, "graph_spine:deal_dupes", "deal_dupes", None, None, None),
        (212, "heal:unknown", "nothing", None, None, None),
    ]})
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    out = sac.classify_all_open(limit=50)
    assert out["scanned"] == 2 and out["classified"] == 1
    assert out["by_class"] == {DEALS: 1}
    (sel, params), = _sqls(cur, "action_class IS NULL ORDER BY id DESC LIMIT %s")
    assert params[1] == 50
    assert conn.commits == 1


def test_classify_all_clamps_the_bound(monkeypatch):
    cur = _Cur()
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    sac.classify_all_open(limit=5000)
    assert _sqls(cur, "LIMIT %s")[0][1][1] == 500
    sac.classify_all_open(limit=0)
    assert _sqls(cur, "LIMIT %s")[1][1][1] == 1


def test_classify_all_is_fail_soft(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sac, "_conn", _boom)
    out = sac.classify_all_open()
    assert out["classified"] == 0 and "RuntimeError" in out["error"]
    # and the drain's wrapper survives a broken module too
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "routes.squasher_action_classes":
            raise ImportError("module broken on this deploy")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _blocked)
    out = sq._classify_all_open(50)
    assert out["classified"] == 0 and "ImportError" in out["error"]


def test_the_drain_publishes_what_the_pass_tagged(monkeypatch):
    monkeypatch.setattr(sq, "_classify_all_open",
                        lambda limit=50: {"scanned": 3, "classified": 2, "by_class": {NEWS: 2}})
    monkeypatch.setattr(sq, "_action_classes_step", lambda dry_run=False: {"ok": True})
    cur = _Cur()
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    monkeypatch.setattr(sq, "_ensure_table", lambda cur: None)
    monkeypatch.setattr(sq, "collapse_duplicate_open_rows", lambda cur, dry_run=False: {})
    monkeypatch.setattr(sq, "reclaim_misfiled", lambda cur: 0)
    monkeypatch.setattr(sq, "reclaim_stale_running", lambda cur: 0)
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    out = sq.drain()
    assert out["classified"]["classified"] == 2 and out["classified"]["by_class"] == {NEWS: 2}


# ── (d) the verifier fallback ───────────────────────────────────────────────
def _fetch_ok(method, path):
    assert method == "GET"
    return 200, {"ok": True, "dry_run": True, "duplicate_rows": 7}


def test_a_non_actuator_class_reads_its_class_row_verifier(monkeypatch):
    cur = _Cur({"FROM brain_action_classes WHERE class = %s": [
        tuple({"class": FAC, "verifier_url": "/api/v1/admin/facility-dedup/analyze"}.get(c)
              for c in sac._CLASS_COLS)]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    body, code = sac.read_class_verifier(FAC, {"country": "US"}, fetch=_fetch_ok)
    assert code == 200 and body["ok"] is True
    assert body["duplicate_rows"] == 7
    assert body["verifier_url"] == "/api/v1/admin/facility-dedup/analyze?country=US"


def test_the_registry_verifier_is_the_fallback_when_the_row_is_unreadable(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sac, "_conn", _boom)
    body, code = sac.read_class_verifier(FAC, {"country": "US"}, fetch=_fetch_ok)
    assert code == 200 and body["duplicate_rows"] == 7


def test_a_missing_or_bad_row_parameter_is_400_not_a_blind_read(monkeypatch):
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(_Cur()))
    calls = []
    body, code = sac.read_class_verifier(FAC, {}, fetch=lambda m, p: calls.append(p) or (200, {}))
    assert code == 400 and body["duplicate_rows"] is None and calls == []
    body, code = sac.read_class_verifier(FAC, {"country": "usa"}, fetch=lambda m, p: calls.append(p) or (200, {}))
    assert code == 400 and calls == []


def test_unreadable_is_unmeasured_not_zero(monkeypatch):
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(_Cur()))
    body, code = sac.read_class_verifier(FAC, {"country": "US"},
                                         fetch=lambda m, p: (500, {"error": "db"}))
    assert code == 200 and body["ok"] is False and body["duplicate_rows"] is None
    assert "UNMEASURED" in body["error"]


def test_an_unknown_class_is_still_404(monkeypatch):
    body, code = sac.read_class_verifier("nope", {}, fetch=_fetch_ok)
    assert code == 404


def test_the_route_no_longer_404s_a_registered_non_actuator_class(monkeypatch):
    """★ Measured 2026-09-02: GET /squasher/verifier/facility_dedup_apply →
    404 'unknown actuator class'."""
    flask = pytest.importorskip("flask")
    app = flask.Flask("t")
    app.register_blueprint(sac.squasher_action_classes_bp)
    monkeypatch.setattr(sac, "_gate", lambda: None)
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(_Cur()))
    monkeypatch.setattr(sac, "_loopback", _fetch_ok)
    r = app.test_client().get("/api/v1/brain/squasher/verifier/facility_dedup_apply?country=US")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["duplicate_rows"] == 7
    r = app.test_client().get("/api/v1/brain/squasher/verifier/not_a_class")
    assert r.status_code == 404
