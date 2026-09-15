"""Every approval says what became of its PR, and a held-back PR can be pushed
past the verdict gate on purpose.

WHY THIS EXISTS
---------------
2026-09-15. The operator approved the top of the innovation board with
"approve + open PR" and nothing reached GitHub. Measured on production
brain_approvals: of the 32 approvals since #4479 merged (2026-09-12 11:56Z),
2 have a PR — the only two items that passed the verdict gate — and 30 carry
no draft attempt at all.

★ 1. THE GATE HELD THEM BACK AND NOTHING SAID SO. #4479's withheld branch
  returned before _record_pr_attempt, and the page painted its reply as a green
  "recorded (no code change)", dropping the reason and `override_with`. After a
  refresh a held-back approval drew exactly like one that never asked for a PR.

★ 2. THE FLOOR SHUT A WHOLE COLUMN. The 0.40 confidence floor also applied to
  items that SURVIVED refutation. The six survivors in the 09-15 self-directed
  agenda stored 0.23–0.37, so 0 of 15 agenda items could open a PR from
  Approve. Owner decision the same day: surviving the refuter skips the floor
  (pinned in tests/test_brain_approval_gate.py; exercised end to end here).

★ 3. THE SPEC FALLBACK'S ANSWER WAS DROPPED. When the drafter refused and the
  spec-PR fallback filed nothing, the attempt persisted as the bare refusal —
  10 of the 45 approvals on that board, reason unrecoverable.

In section 3 the REAL draft_and_open_pr and open_spec_pr run; only the model
call, the budget read and GitHub are stubbed, so the shapes asserted are the
shapes production writes.

Run:  python3 -m pytest tests/test_brain_approve_says_why_no_pr.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import subprocess

import pytest

dash = pytest.importorskip("routes.brain_innovation_dashboard")
flask = pytest.importorskip("flask")


@pytest.fixture()
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(dash.brain_innovation_dashboard_bp)
    return app.test_client()


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.setattr(dash, "_APPROVAL_COLUMNS_ENSURED", False)


class _Cur:
    """Answers each query by a substring of its SQL; records what ran."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.rowcount = 1
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.calls.append((self._last, params))

    def _rows(self):
        for needle, rows in self.answers.items():
            if needle in self._last:
                return rows
        return []

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._rows())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


_COLS = [(c,) for c in ("kind", "item_id", "decision", "note", "approved_at",
                        "pr_attempt", "pr_url", "pr_attempted_at", "pr_redrives")]

# Verdicts as _item_verdict returned them for the live board on 2026-09-15.
_REFUTED = {"confidence": 0.25, "refutation_survived": False,
            "refutation_attempted": True}                        # inv #100652
_SURVIVOR_UNDER_FLOOR = {"confidence": 0.23, "refutation_survived": True,
                         "refutation_attempted": True}           # agenda #100256

_PR_4621 = "https://github.com/azmartone67/dchub-backend/pull/4621"


def _approve(client, monkeypatch, verdict, body_extra=None):
    """POST the page's own approve body with the DB, the verdict read and the
    draft act stubbed. -> (reply json, attempts recorded, drafts run)."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(_Cur()))
    monkeypatch.setattr(dash, "_item_verdict", lambda kind, item_id: dict(verdict))
    recorded, drafted = [], []
    monkeypatch.setattr(dash, "_record_pr_attempt",
                        lambda kind, item_id, a: recorded.append(a) or True)

    def _draft(kind, item_id, operator_directive=""):
        drafted.append((kind, item_id))
        return {"ok": True, "acted": True,
                "note": "filed as draft spec PR for a human",
                "fallback_spec_pr": {"ok": True, "acted": True, "spec_pr": True,
                                     "pr": {"number": 4621, "url": _PR_4621}}}
    monkeypatch.setattr(dash, "_attempt_pr", _draft)
    body = {"kind": "inv", "id": 100652, "decision": "approved", "open_pr": True,
            "directive": "Re-run the press_releases cron and read its log."}
    body.update(body_extra or {})
    resp = client.post("/api/v1/brain/innovation/approve", json=body)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json(), recorded, drafted


# ── 1. a held-back PR is persisted, drafts nothing, and says so ─────────────
def test_a_refuted_approval_persists_the_hold_and_drafts_nothing(client, monkeypatch):
    data, recorded, drafted = _approve(client, monkeypatch, _REFUTED)
    assert drafted == [], "a refuted item must not reach the drafter"
    assert len(recorded) == 1, "the hold must be written to the approval row"
    held = recorded[0]
    assert held["withheld"] is True and "refut" in held["note"].lower()
    assert data["pr_attempt"] == held and data["pr_attempt_persisted"] is True
    assert data["approval"]["pr_state"] == "withheld"
    assert data["approval"]["can_override"] is True
    assert "refut" in data["approval"]["pr_note"].lower()


def test_after_a_refresh_the_card_draws_what_the_click_reply_drew(client, monkeypatch):
    """The row the route WROTE, round-tripped through JSONB and read back by the
    digest's derivation, must equal the reply the page drew after the click."""
    data, recorded, _ = _approve(client, monkeypatch, _REFUTED)
    stored = json.loads(json.dumps(recorded[0]))
    assert dash.approval_view("approved", None, stored) == data["approval"]


def test_a_persisted_hold_is_neither_redriven_nor_reported_stale(client, monkeypatch):
    _, recorded, _ = _approve(client, monkeypatch, _REFUTED)
    held = json.dumps(recorded[0])
    assert dash._redrive_wanted(json.loads(held)) is False
    ts = _dt.datetime(2026, 9, 12, 22, 29, tzinfo=_dt.timezone.utc)
    cur = _Cur({"FROM brain_approvals WHERE decision = 'approved'": [
        ("agenda", 100261, ts, held, 0),
        ("agenda", 100264, ts, None, 0),   # control: how the 30 rows read today
    ]})
    out = dash.stale_approvals_without_pr(cur)
    assert [r["key"] for r in out] == ["agenda:100264"], (
        "the NULL-attempt control must still be reported, or the skip of the "
        "held row proves nothing")


# ── 2. the override goes past the gate on purpose; survivors go through ─────
def test_override_drafts_a_refuted_item_and_names_what_it_overrode(client, monkeypatch):
    data, recorded, drafted = _approve(client, monkeypatch, _REFUTED,
                                       body_extra={"override": True})
    assert drafted == [("inv", 100652)]
    assert "refut" in data["pr_override"].lower()
    assert "pr_blocked" not in data
    assert data["approval"]["pr_state"] == "opened"
    assert data["approval"]["pr_url"] == _PR_4621


def test_a_survivor_under_the_floor_now_drafts(client, monkeypatch):
    """agenda #100256 as served on 09-15: survived refutation at 0.23."""
    data, _, drafted = _approve(client, monkeypatch, _SURVIVOR_UNDER_FLOOR,
                                body_extra={"kind": "agenda", "id": 100256})
    assert drafted == [("agenda", 100256)]
    assert "pr_blocked" not in data and "pr_override" not in data


# ── 3. the spec fallback's answer is kept — real drafter, real opener ───────
@pytest.fixture()
def producers(monkeypatch):
    """The real draft_and_open_pr and open_spec_pr. Stubbed: the model call
    (it refuses, as it does for a plan), the budget read, the dedup lookups
    and GitHub. Knobs: `gate` (successive can_open_pr answers), `merged_pr`."""
    g = pytest.importorskip("routes.brain_guardrails")
    o = pytest.importorskip("routes.brain_pr_opener")
    l4 = pytest.importorskip("routes.brain_v2_layer4")
    knobs = {"gate": [(True, "ok (0/8 used today)")], "merged_pr": None}
    monkeypatch.setattr(dash, "_item_directive", lambda kind, item_id: (
        "Build a regeneration step that renders every AI surface from canon.",
        "[developer_ux] Brain finding: ai_surface_drift:llms_full:stale_value"))
    monkeypatch.setattr(l4, "_call_claude", lambda prompt, system: (
        '{"refuse": true, "rationale": "a plan, not a single-file edit"}', None))

    def _gate():
        seq = knobs["gate"]
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(g, "can_open_pr", _gate)
    monkeypatch.setattr(o, "open_pr_exists", lambda title: False)
    monkeypatch.setattr(o, "spec_condition_fingerprint",
                        lambda heading, directive="": "0123456789abcdef")
    monkeypatch.setattr(o, "open_spec_pr_with_fingerprint", lambda fp: None)
    monkeypatch.setattr(o, "landed_spec_with_fingerprint", lambda fp: None)
    monkeypatch.setattr(o, "merged_spec_pr_with_fingerprint",
                        lambda fp: knobs["merged_pr"])

    def _no_github(*a, **k):
        raise AssertionError("this path must not reach GitHub")
    monkeypatch.setattr(o, "_gh", _no_github)
    return knobs


def test_a_declined_fallback_keeps_its_answer(producers):
    producers["merged_pr"] = 4246
    att = dash._attempt_pr("agenda", 100266)
    assert att.get("refused") is True and not att.get("acted")
    spec = att["fallback_spec_pr"]
    assert spec["landed_spec_pr"] == 4246 and "#4246" in spec["note"]
    assert dash._redrive_wanted(att) is False, "a dedup answer is not a failure"
    v = dash.approval_view("approved", None, att)
    assert v["pr_state"] == "declined" and "#4246" in v["pr_note"]
    assert v["pr_ref_url"].endswith("/pull/4246")
    assert v["can_request_pr"] is False


def test_a_failed_fallback_is_a_failure_the_redrive_retries(producers):
    producers["gate"] = [(True, "ok (7/8 used today)"),
                         (False, "daily_budget_exhausted (8/8 auto-PRs today)")]
    att = dash._attempt_pr("agenda", 100266)
    assert att["ok"] is False
    assert "daily_budget_exhausted" in att["error"]
    assert dash._redrive_wanted(att) is True
    v = dash.approval_view("approved", None, att)
    assert v["pr_state"] == "failed" and v["can_request_pr"] is True
    assert "daily_budget_exhausted" in v["pr_note"]


def test_a_filed_fallback_reads_as_before(producers, monkeypatch):
    """Control: when the fallback DOES file, the attempt is unchanged."""
    o = pytest.importorskip("routes.brain_pr_opener")
    monkeypatch.setattr(o, "_get_default_branch_sha", lambda: "abc123")
    monkeypatch.setattr(o, "_create_branch", lambda branch, sha: True)
    monkeypatch.setattr(o, "_commit_file", lambda *a, **k: True)

    class _Resp:
        status_code = 201

        def json(self):
            return {"number": 4621, "html_url": _PR_4621}
    monkeypatch.setattr(o, "_gh", lambda *a, **k: _Resp())
    att = dash._attempt_pr("inv", 100654)
    assert att["acted"] is True and att["note"] == "filed as draft spec PR for a human"
    assert dash._pr_url_of(att) == _PR_4621
    assert dash.approval_view("approved", _PR_4621, att)["pr_state"] == "opened"


# ── 4. approval_view — the states the page draws ────────────────────────────
def test_no_attempt_on_record_may_ask_for_a_pr():
    """How the 30 held-back rows since #4479 read: pr_attempt NULL."""
    v = dash.approval_view("approved", None, None)
    assert (v["pr_state"], v["can_request_pr"], v["can_override"]) == ("none", True, False)


def test_a_landed_pr_wins_over_a_later_attempt_record():
    v = dash.approval_view("approved", _PR_4621,
                           {"ok": False, "error": "a later redrive failed"})
    assert v["pr_state"] == "opened" and v["pr_url"] == _PR_4621
    assert v["can_request_pr"] is False


def test_a_refusal_persisted_before_this_change_still_reads_as_one():
    """10 of the 45 approvals on the 09-15 board are stored exactly so."""
    v = dash.approval_view("approved", None, {"ok": True, "acted": False,
                                              "refused": True, "rationale": "a plan"})
    assert (v["pr_state"], v["pr_note"], v["can_request_pr"]) == ("refused", "a plan", True)


def test_a_drafter_failure_reads_failed():
    v = dash.approval_view("approved", None,
                           {"ok": False, "error": "claude call failed: http_429"})
    assert v["pr_state"] == "failed" and "http_429" in v["pr_note"]


# ── 5. the digest carries each item's approval, read for the board only ─────
def _board(monkeypatch):
    monkeypatch.setattr(dash, "_recent_agenda",
                        lambda limit=15: [{"id": 100267, "title": "a"}])
    monkeypatch.setattr(dash, "_recent_investigations",
                        lambda limit=15: [{"id": 100267, "question": "same id, other table"}])
    monkeypatch.setattr(dash, "_recent_proposals",
                        lambda limit=15: [{"id": 100079, "title": "p"}])


def test_the_digest_carries_each_items_approval(client, monkeypatch):
    _board(monkeypatch)
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    held = json.dumps({"ok": True, "acted": False, "withheld": True, "note": "refuted"})
    cur = _Cur({"information_schema.columns": _COLS,
                "FROM brain_approvals WHERE item_id = ANY": [
                    ("agenda", 100267, "approved", None, held),
                    ("prop", 100079, "approved", None, None)]})
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(cur))
    d = client.get("/api/v1/brain/innovation/digest").get_json()
    assert d["approvals_read"] is True
    assert d["agenda"][0]["approval"]["pr_state"] == "withheld"
    assert d["investigations"][0]["approval"] is None, (
        "inv:100267 is not approved — an id shared across tables must not "
        "borrow the agenda row")
    # prop #100079 was approved 2026-08-15 and had fallen off /approvals' LIMIT 500
    assert d["proposals"][0]["approval"]["pr_state"] == "none"
    (sql, params), = [c for c in cur.calls if "FROM brain_approvals" in c[0]]
    assert "LIMIT" not in sql.upper()
    assert sorted(params[0]) == [100079, 100267]


def test_an_unreadable_ledger_is_unknown_not_unapproved(client, monkeypatch):
    _board(monkeypatch)
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: None)
    d = client.get("/api/v1/brain/innovation/digest").get_json()
    assert d["approvals_read"] is False
    assert all(it["approval"] is None
               for s in ("agenda", "investigations", "proposals") for it in d[s])


def test_an_empty_board_reads_no_ledger(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "build_digest", lambda limit=15: {
        "ok": True, "agenda": [], "investigations": [], "proposals": []})

    def _boom():
        raise AssertionError("no items on the board, so no ledger read")
    monkeypatch.setattr(dash, "_conn", _boom)
    d = client.get("/api/v1/brain/innovation/digest").get_json()
    assert d["approvals_read"] is True


# ── 6. the redrive's window holds failures only ─────────────────────────────
def test_the_redrive_selects_failures_in_sql(monkeypatch):
    """Held-back approvals are ANSWERED rows. Filtered only in Python, a burst of
    them fills the SELECT's LIMIT (4x the per-tick cap) and starves the real
    failures behind them. Asserted on the SQL actually executed."""
    g = pytest.importorskip("routes.brain_guardrails")
    monkeypatch.setattr(g, "can_open_pr", lambda: (True, "ok"))
    cur = _Cur({"information_schema.columns": _COLS})
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(cur))
    dash.redrive_approved_without_pr()
    (sql, _), = [c for c in cur.calls
                 if c[0].startswith("SELECT kind, item_id, pr_attempt")]
    assert "pr_attempt->>'ok' = 'false'" in sql


# ── 7. the page ─────────────────────────────────────────────────────────────
def _page(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    return client.get("/api/v1/brain/innovation/dashboard").get_data(as_text=True)


def test_the_page_no_longer_paints_a_hold_as_success(client, monkeypatch):
    body = _page(client, monkeypatch)
    assert "recorded (no code change)" not in body
    assert body.count("payload.override = true") == 1
    assert 'data-pr-act="override"' in body
    assert "open PR anyway" in body


def test_the_page_draws_the_servers_state_and_does_not_rederive_it(client, monkeypatch):
    body = _page(client, monkeypatch)
    assert "BOARD[kind+':'+it.id] = it.approval" in body
    assert "res.j.approval" in body
    for rederived in ("pr_attempt", "pa.refused", "pa.note", "pa.acted"):
        assert rederived not in body, f"the page re-derives the outcome from {rederived}"


# ── 8. the served script draws each state it is handed ──────────────────────
_NODE_PRELUDE = r"""
const els = {};
function el(id){
  if(!els[id]){ els[id] = {id: id, textContent: '', innerHTML: '', addEventListener: function(){}}; }
  return els[id];
}
globalThis.document = {getElementById: el, addEventListener: function(){},
                       querySelector: function(){ return null; }, cookie: ''};
globalThis.location = {search: ''};
globalThis.window = globalThis;
globalThis.setInterval = function(){ return 0; };
globalThis.fetch = function(){
  return Promise.resolve({ok: true, status: 200,
                          json: function(){ return Promise.resolve(DIGEST); }});
};
"""
_NODE_POSTLUDE = r"""
setTimeout(function(){
  process.stdout.write(JSON.stringify({status: el('status').textContent,
                                       agenda: el('col-agenda').innerHTML}));
}, 50);
"""


def test_the_served_script_draws_each_state_it_is_handed(client, monkeypatch, tmp_path):
    """The page's REAL script, run in node against a digest carrying each
    approval state, with the cards it draws read back. A state that stops being
    drawn — no override on a hold, no link on a landed PR, a live button on a
    PR that exists — fails here, not only in a browser."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    body = _page(client, monkeypatch)
    js = body[body.index("<script>") + len("<script>"):body.rindex("</script>")]
    held = {"ok": True, "acted": False, "withheld": True,
            "note": "the adversarial refuter REFUTED this item"}
    specced = {"ok": True, "acted": False, "refused": True,
               "fallback_spec_pr": {"ok": True, "acted": False, "landed_spec_pr": 4246,
                                    "note": "condition already specced and MERGED as PR #4246"}}
    items = [
        {"id": 1, "title": "not approved", "approval": None},
        {"id": 2, "title": "held back", "approval": dash.approval_view("approved", None, held)},
        {"id": 3, "title": "nothing on record", "approval": dash.approval_view("approved", None, None)},
        {"id": 4, "title": "PR landed", "approval": dash.approval_view("approved", _PR_4621, None)},
        {"id": 5, "title": "already specced", "approval": dash.approval_view("approved", None, specced)},
    ]
    digest = {"ok": True, "generated_at": "2026-09-15T21:30:27", "approvals_read": True,
              "counts": {"agenda": len(items), "investigations": 0, "proposals": 0},
              "agenda": items, "investigations": [], "proposals": []}
    script = tmp_path / "page.js"
    script.write_text("const DIGEST = " + json.dumps(digest) + ";\n"
                      + _NODE_PRELUDE + js + _NODE_POSTLUDE, encoding="utf-8")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "Live"
    cards = {int(m.group(1)): m.group(0) for m in re.finditer(
        r'<div class="card" data-kind="agenda" data-id="(\d+)">.*?(?=<div class="card" |\Z)',
        out["agenda"], re.S)}
    assert sorted(cards) == [1, 2, 3, 4, 5]
    assert "data-approve" in cards[1] and "data-pr-state" not in cards[1]
    assert 'data-pr-state="withheld"' in cards[2] and 'data-pr-act="override"' in cards[2]
    assert "REFUTED" in cards[2], "the hold's reason must be on the card"
    assert 'data-pr-state="none"' in cards[3] and 'data-pr-act="request"' in cards[3]
    assert 'data-pr-act="override"' not in cards[3]
    assert 'data-pr-state="opened"' in cards[4] and f'href="{_PR_4621}"' in cards[4]
    assert "data-pr-act" not in cards[4], "a landed PR must not offer another"
    assert 'data-pr-state="declined"' in cards[5] and "/pull/4246" in cards[5]
    assert "data-pr-act" not in cards[5]
