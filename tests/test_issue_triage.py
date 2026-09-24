#!/usr/bin/env python3
"""tests/test_issue_triage.py — tools/issue_triage.py, no network.

MEASURED 2026-09-23: 88 open "[watchdog] DC Hub health check tripped — HH:MM"
issues, every one `crm_export` red. These pin the two behaviours that stop it
recurring: the watchdog keeps ONE tracker, and the daily sweep closes exact
re-files while leaving distinct findings and human decisions alone.
"""
import datetime as dt
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "issue_triage", os.path.join(ROOT, "tools", "issue_triage.py"))
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
BOT = {"login": "github-actions[bot]"}
HUMAN = {"login": "azmartone67"}


def issue(n, title, labels=(), user=BOT, created="2026-09-23T10:00:00Z",
          updated=None, body=""):
    return {"number": n, "title": title, "labels": [{"name": l} for l in labels],
            "user": user, "created_at": created, "updated_at": updated or created,
            "body": body}


class FakeGh:
    def __init__(self, issues, comments=None):
        self.issues = {i["number"]: i for i in issues}
        self._comments = comments or {}
        self.log = []
        self.next = 9000

    def open_issues(self):
        return [i for i in self.issues.values() if i.get("state", "open") == "open"]

    def comments(self, n):
        return [{"body": b} for b in self._comments.get(n, [])]

    def create(self, title, body, labels, assignees=None):
        self.next += 1
        self.log.append(("create", self.next, title, tuple(labels), tuple(assignees or ())))
        self.issues[self.next] = issue(self.next, title, labels, body=body)
        return self.next

    def comment(self, n, body):
        self.log.append(("comment", n, body))
        self._comments.setdefault(n, []).append(body)

    def edit(self, n, **fields):
        self.log.append(("edit", n, fields))
        self.issues[n].update(fields)

    def close(self, n, comment, reason="not_planned"):
        self.comment(n, comment)
        self.edit(n, state="closed", state_reason=reason)

    def ops(self, kind):
        return [e for e in self.log if e[0] == kind]


RED = {"status": "red", "checks": {"crm_export": {"status": "red"},
                                    "database": {"status": "green"}}}
GREEN = {"status": "green", "checks": {"crm_export": {"status": "green"}}}


# ── watchdog ────────────────────────────────────────────────────────────────
def test_red_with_no_tracker_opens_exactly_one():
    gh = FakeGh([])
    it.watchdog(gh, RED, 0)
    creates = gh.ops("create")
    assert len(creates) == 1 and creates[0][2] == it.WATCHDOG_TITLE
    assert "watchdog-sig: crm_export -->" in gh.issues[creates[0][1]]["body"]


def test_repeated_identical_red_is_silent():
    """The 88-issue bug: hour after hour of the same red must add nothing."""
    gh = FakeGh([])
    for _ in range(24):
        it.watchdog(gh, RED, 0)
    assert len(gh.ops("create")) == 1
    assert gh.ops("comment") == []


def test_existing_legacy_tracker_is_reused_not_duplicated():
    # The HH:MM-titled issue left open by the old step, with no sig marker yet.
    gh = FakeGh([issue(5375, "[watchdog] DC Hub health check tripped — 00:58", ["watchdog"],
                       body='{"status":"red"}')])
    it.watchdog(gh, RED, 0)
    it.watchdog(gh, RED, 0)
    assert gh.ops("create") == []
    assert len(gh.ops("comment")) == 1          # one note recording the signature, then quiet


def test_changed_red_set_comments():
    gh = FakeGh([])
    it.watchdog(gh, RED, 0)
    worse = {"status": "red", "checks": {"crm_export": {"status": "red"},
                                          "telemetry": {"status": "red"}}}
    it.watchdog(gh, worse, 0)
    (c,) = gh.ops("comment")
    assert "**crm_export** → **crm_export,telemetry**" in c[2]


def test_green_closes_tracker_as_completed():
    gh = FakeGh([])
    it.watchdog(gh, RED, 0)
    n = gh.ops("create")[0][1]
    it.watchdog(gh, GREEN, 0)
    assert gh.issues[n]["state"] == "closed" and gh.issues[n]["state_reason"] == "completed"


def test_unreachable_is_red_not_green():
    gh = FakeGh([])
    it.watchdog(gh, None, 7)
    assert "health endpoint unreachable" in gh.issues[gh.ops("create")[0][1]]["body"]
    assert it.failing_signature({"status": "green"}, 22) == "health endpoint unreachable"
    assert it.failing_signature(["not", "a", "dict"], 0) == "health endpoint unreachable"


def test_yellow_is_not_an_alarm():
    assert it.failing_signature({"status": "yellow", "checks": {}}, 0) == ""


def test_other_watchdog_labelled_issue_is_not_touched():
    """#5340 [OSM refresh failed] carries the watchdog label — a green probe must
    not close it (the manual cleanup on 2026-09-23 did, and had to reopen it)."""
    osm = issue(5340, "[OSM refresh failed] 2026-09-23", ["watchdog"])
    gh = FakeGh([osm])
    it.watchdog(gh, GREEN, 0)
    assert gh.log == []


def test_extra_trackers_fold_into_newest():
    gh = FakeGh([issue(10, "[watchdog] DC Hub health check tripped — 01:00", ["watchdog"]),
                 issue(11, "[watchdog] DC Hub health check tripped — 02:00", ["watchdog"])])
    it.watchdog(gh, RED, 0)
    assert gh.issues[10]["state"] == "closed"
    assert gh.issues[11].get("state", "open") == "open"


# ── sweep ───────────────────────────────────────────────────────────────────
def test_title_key_strips_times_keeps_ids():
    assert it.title_key("[watchdog] X tripped — 13:26") == it.title_key("[watchdog] X tripped — 00:58")
    assert it.title_key("[OSM refresh failed] 2026-09-23") == it.title_key("[OSM refresh failed] 2026-09-22")
    assert it.title_key("[spec-debt] inv #100694: a") != it.title_key("[spec-debt] inv #100681: a")


def test_sweep_closes_older_bot_copies_only():
    issues = [
        issue(1, "[OSM refresh failed] 2026-09-21", ["osm"]),
        issue(2, "[OSM refresh failed] 2026-09-22", ["osm"]),
        issue(3, "[OSM refresh failed] 2026-09-23", ["osm"]),
        issue(4, "[spec-debt] inv #100694: pages", ["spec-debt"]),
        issue(5, "[spec-debt] inv #100681: pages", ["spec-debt"]),
        # Same title, but a human filed it: never auto-closed.
        issue(6, "[OSM refresh failed] 2026-09-20", ["osm"], user=HUMAN),
        # Same title, protected label.
        issue(7, "[OSM refresh failed] 2026-09-19", ["osm", "keep"]),
    ]
    gh = FakeGh(issues)
    res = it.sweep(gh, NOW)
    assert res["duplicates_closed"] == [1, 2]
    for n in (3, 4, 5, 6, 7):
        assert gh.issues[n].get("state", "open") == "open"


def test_same_title_different_labels_not_folded():
    gh = FakeGh([issue(1, "Registry drift", ["registry-drift"]),
                 issue(2, "Registry drift", ["mcp-registry-watch"])])
    assert it.sweep(gh, NOW)["duplicates_closed"] == []


def test_digest_created_once_assigned_then_edited_in_place():
    base = [issue(20, "Decide pricing", [], user=HUMAN),
            issue(21, "[watchdog] DC Hub health check red", ["watchdog"]),
            issue(22, "[brain-l15] old finding", ["brain-l15-auto"],
                  created="2026-08-01T00:00:00Z", updated="2026-08-02T00:00:00Z")]
    gh = FakeGh(base)
    it.sweep(gh, NOW)
    (c,) = gh.ops("create")
    assert c[3] == (it.TRIAGE_LABEL,) and c[4] == (it.OWNER_LOGIN,)
    body = gh.issues[c[1]]["body"]
    assert "Decisions waiting on you (1)\n" in body and "#20 " in body
    assert "Alarms still red (1)\n" in body and "#21 " in body
    assert "untouched for 14+ days (1)\n" in body and "#22 " in body
    # Second run with nothing new: no second issue, no rewrite.
    it.sweep(gh, NOW + dt.timedelta(hours=1))
    assert len(gh.ops("create")) == 1 and gh.ops("edit") == []
    # Something changes: same issue is rewritten.
    gh.issues[30] = issue(30, "Another call", [], user=HUMAN)
    it.sweep(gh, NOW)
    assert len(gh.ops("create")) == 1 and len(gh.ops("edit")) == 1


def test_digest_never_lists_or_closes_itself():
    gh = FakeGh([issue(1, "x", ["spec-debt"])])
    it.sweep(gh, NOW)
    it.sweep(gh, NOW)
    n = gh.ops("create")[0][1]
    assert gh.issues[n].get("state", "open") == "open"
    assert f"#{n} " not in gh.issues[n]["body"]


def test_empty_issue_list_refuses():
    with pytest.raises(RuntimeError):
        it.sweep(FakeGh([]), NOW)


def test_max_closes_caps_a_run():
    gh = FakeGh([issue(n, f"[OSM refresh failed] 2026-09-{n:02d}", ["osm"]) for n in range(1, 11)])
    assert len(it.sweep(gh, NOW, max_closes=3)["duplicates_closed"]) == 3


def test_long_section_is_capped_with_a_count():
    old = [issue(n, f"[spec-debt] inv #{n}", ["spec-debt"], created="2026-08-01T00:00:00Z")
           for n in range(100, 140)]
    body = it.render_digest(old, NOW)
    assert "(40)\n" in body and "… and 10 more" in body and body.count("\n- #") == 30
