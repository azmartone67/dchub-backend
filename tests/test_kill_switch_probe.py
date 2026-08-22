"""kill-switch-probe: "config SET" must equal "config IN EFFECT" (2026-08-22).

WHAT WAS MEASURED
=================
BRAIN_REVIEW_LANE_ENABLED=0 was set in Railway while the review lane kept
opening brain/review-* PRs for four more minutes — the env-change redeploy
had FAILED and the old container kept serving the old env. Nothing compared
the switch to the behaviour it was supposed to stop.

THE CONTRACT being guarded (tools/kill_switch_probe.py + the workflow)
======================================================================
- the EXPECTED value comes from the repo-side registry (owner intent), never
  from Railway — the probe needs no Railway credentials
- per switch: agree / violation / unknown; a switch that cannot be observed
  (endpoint 404 — e.g. the Step-2 inbox field not shipped yet) is UNKNOWN,
  never red, and never silently healthy
- aggregate: any violation → `error`; ≥1 agree and no violation → `success`
  (unknowns named in the note); all unknown → no beat at all + exit 2
- the beat is the ONE writer of feed `kill-switch-probe`, cadence_hours=3,
  UA dchub-kill-switch-probe/1.0, worker-proxied reads with timeout > 180 s,
  and it RAISES on non-2xx / non-JSON / ok != true (loud), printing the
  response in full
- the workflow: 2-hourly cron, DCHUB_ADMIN_KEY via `env:` from secrets, no
  curl, no github-script, no `| head -c`, issues:write for the loud path,
  and the feed is NOT in tools/deadman/watch.py WORKFLOWS (one writer)

NO NETWORK: the module is loaded from its path (tools/ is not a package),
observers are swapped for fakes, and requests.post is monkeypatched. Nothing
runs at module scope.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import re
import types

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD = ROOT / "tools" / "kill_switch_probe.py"
WF = ROOT / ".github" / "workflows" / "kill-switch-probe.yml"
WATCH = ROOT / "tools" / "deadman" / "watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("kill_switch_probe_under_test", MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now():
    return dt.datetime(2026, 8, 22, 6, 0, tzinfo=dt.timezone.utc)


def _iso(minutes_ago):
    return (_now() - dt.timedelta(minutes=minutes_ago)).isoformat()


def _reg(mod, **overrides):
    """A copy of the registry with selected expected values overridden."""
    reg = {k: dict(v) for k, v in mod.SWITCHES.items()}
    for k, v in overrides.items():
        reg[k]["expected"] = v
    return reg


# ── 1 · the registry ─────────────────────────────────────────────────────────

class TestRegistry:
    def test_every_switch_is_fully_declared(self):
        mod = _load()
        assert len(mod.SWITCHES) >= 6
        for name, spec in mod.SWITCHES.items():
            assert re.fullmatch(r"[A-Z][A-Z0-9_]+", name), name
            assert spec["expected"] in ("0", "1"), f"{name}: expected must be the exact string 0 or 1"
            assert spec["observe"] in mod.OBSERVERS, f"{name}: no observer for {spec['observe']!r}"
            assert len(spec["rule"]) >= 40, f"{name}: rule too thin to audit"
            dt.date.fromisoformat(spec["since"])
            assert spec["service"] in ("dchub-worker", "dchub-backend")

    def test_the_switches_the_incident_named_are_all_registered(self):
        mod = _load()
        assert {"BRAIN_REVIEW_LANE_ENABLED", "BRAIN_AUTOMERGE_ENABLED", "BRAIN_AUTOMERGE_DRY_RUN",
                "ACTION_CLASSES_ENABLED", "SQUASHER_QUEUE_DISABLE", "MONTHLY_QUOTA_ENFORCE"} <= set(mod.SWITCHES)
        assert mod.SWITCHES["BRAIN_REVIEW_LANE_ENABLED"]["expected"] == "0", "the lane is meant to be OFF"

    def test_feed_contract_constants(self):
        mod = _load()
        assert mod.FEED == "kill-switch-probe"
        assert mod.CADENCE_HOURS == 3
        assert mod.WINDOW_H == 2
        assert mod.UA.startswith("dchub-kill-switch-probe/1.0")
        assert mod.TIMEOUT_WORKER_PROXIED_S > 180, "worker-proxied paths need > 180s"
        assert mod.REVIEW_BRANCH_PREFIX == "brain/review-" and mod.AUTOFIX_BRANCH_PREFIX == "brain/autofix-"

    def test_branch_prefixes_match_the_lanes_own_constants(self):
        mod = _load()
        lane = (ROOT / "routes" / "brain_review_lane.py").read_text()
        am = (ROOT / "routes" / "brain_automerge.py").read_text()
        assert f'REVIEW_BRANCH_PREFIX = "{mod.REVIEW_BRANCH_PREFIX}"' in lane
        assert f'AUTOFIX_BRANCH_PREFIX = "{mod.AUTOFIX_BRANCH_PREFIX}"' in am

    def test_the_feed_has_one_writer(self):
        """watch.py's conclusion registry would overwrite the computed status."""
        src = WATCH.read_text()
        m = re.search(r"^WORKFLOWS = \{(.*?)^\}", src, re.S | re.M)
        assert m and "kill-switch-probe" not in m.group(1)


# ── 2 · pure evaluation ──────────────────────────────────────────────────────

class TestEvaluateReviewLane:
    def test_set_zero_but_a_pr_opened_is_a_violation(self):
        mod = _load()
        v = mod.evaluate("BRAIN_REVIEW_LANE_ENABLED", "0",
                         {"review_prs_opened": 1, "branches": ["brain/review-abc"]})
        assert v["state"] == mod.VIOLATION and "brain/review-abc" in v["detail"]

    def test_set_zero_and_nothing_opened_agrees(self):
        mod = _load()
        assert mod.evaluate("BRAIN_REVIEW_LANE_ENABLED", "0", {"review_prs_opened": 0})["state"] == mod.AGREE

    def test_enabled_lane_may_open_prs(self):
        mod = _load()
        v = mod.evaluate("BRAIN_REVIEW_LANE_ENABLED", "1", {"review_prs_opened": 3})
        assert v["state"] == mod.AGREE and "permitted" in v["detail"]

    def test_unobservable_is_unknown_not_agree_and_not_violation(self):
        mod = _load()
        assert mod.evaluate("BRAIN_REVIEW_LANE_ENABLED", "0", None)["state"] == mod.UNKNOWN
        assert mod.evaluate("BRAIN_REVIEW_LANE_ENABLED", "0", {"review_prs_opened": None})["state"] == mod.UNKNOWN


class TestEvaluateAutomerge:
    def test_enabled_and_live_permits_merges(self):
        mod = _load()
        reg = _reg(mod, BRAIN_AUTOMERGE_ENABLED="1", BRAIN_AUTOMERGE_DRY_RUN="0")
        for sw in ("BRAIN_AUTOMERGE_ENABLED", "BRAIN_AUTOMERGE_DRY_RUN"):
            v = mod.evaluate(sw, reg[sw]["expected"], {"autofix_prs_merged": 2}, reg)
            assert v["state"] == mod.AGREE, (sw, v)

    def test_dry_run_with_a_merge_is_a_violation_of_both_switches(self):
        mod = _load()
        reg = _reg(mod, BRAIN_AUTOMERGE_ENABLED="1", BRAIN_AUTOMERGE_DRY_RUN="1")
        for sw in ("BRAIN_AUTOMERGE_ENABLED", "BRAIN_AUTOMERGE_DRY_RUN"):
            v = mod.evaluate(sw, reg[sw]["expected"], {"autofix_prs_merged": 1}, reg)
            assert v["state"] == mod.VIOLATION and "DRY_RUN=1" in v["detail"], (sw, v)

    def test_disabled_with_a_merge_is_a_violation(self):
        mod = _load()
        reg = _reg(mod, BRAIN_AUTOMERGE_ENABLED="0", BRAIN_AUTOMERGE_DRY_RUN="0")
        v = mod.evaluate("BRAIN_AUTOMERGE_ENABLED", "0", {"autofix_prs_merged": 1}, reg)
        assert v["state"] == mod.VIOLATION and "ENABLED=0" in v["detail"]

    def test_disabled_and_no_merge_agrees(self):
        mod = _load()
        reg = _reg(mod, BRAIN_AUTOMERGE_ENABLED="0")
        assert mod.evaluate("BRAIN_AUTOMERGE_ENABLED", "0", {"autofix_prs_merged": 0}, reg)["state"] == mod.AGREE

    def test_unobservable_is_unknown(self):
        mod = _load()
        assert mod.evaluate("BRAIN_AUTOMERGE_ENABLED", "1", None)["state"] == mod.UNKNOWN
        assert mod.evaluate("BRAIN_AUTOMERGE_DRY_RUN", "0", {"autofix_prs_merged": None})["state"] == mod.UNKNOWN


class TestEvaluateActionClasses:
    def test_endpoint_missing_is_unknown_never_red(self):
        mod = _load()
        assert mod.evaluate("ACTION_CLASSES_ENABLED", "0", None)["state"] == mod.UNKNOWN

    def test_field_absent_is_unknown_never_a_silent_zero(self):
        mod = _load()
        v = mod.evaluate("ACTION_CLASSES_ENABLED", "0", {"resolved_by_action_class": None})
        assert v["state"] == mod.UNKNOWN and "field" in v["detail"]

    def test_set_zero_but_a_row_was_resolved_by_a_class_is_a_violation(self):
        mod = _load()
        assert mod.evaluate("ACTION_CLASSES_ENABLED", "0", {"resolved_by_action_class": 2})["state"] == mod.VIOLATION

    def test_set_zero_and_no_rows_agrees(self):
        mod = _load()
        assert mod.evaluate("ACTION_CLASSES_ENABLED", "0", {"resolved_by_action_class": 0})["state"] == mod.AGREE


class TestEvaluateSquasherQueue:
    def test_disabled_but_rows_enqueued_is_a_violation(self):
        mod = _load()
        assert mod.evaluate("SQUASHER_QUEUE_DISABLE", "1", {"rows_requested": 3})["state"] == mod.VIOLATION

    def test_enabled_queue_may_grow(self):
        mod = _load()
        assert mod.evaluate("SQUASHER_QUEUE_DISABLE", "0", {"rows_requested": 3})["state"] == mod.AGREE

    def test_disabled_and_quiet_agrees(self):
        mod = _load()
        assert mod.evaluate("SQUASHER_QUEUE_DISABLE", "1", {"rows_requested": 0})["state"] == mod.AGREE

    def test_unobservable_is_unknown(self):
        mod = _load()
        assert mod.evaluate("SQUASHER_QUEUE_DISABLE", "1", None)["state"] == mod.UNKNOWN


class TestEvaluateQuotaWall:
    def test_published_enforce_matching_the_registry_agrees(self):
        mod = _load()
        assert mod.evaluate("MONTHLY_QUOTA_ENFORCE", "1", {"enforce": True})["state"] == mod.AGREE
        assert mod.evaluate("MONTHLY_QUOTA_ENFORCE", "0", {"enforce": False})["state"] == mod.AGREE

    def test_published_enforce_contradicting_the_registry_is_a_violation(self):
        mod = _load()
        v = mod.evaluate("MONTHLY_QUOTA_ENFORCE", "1", {"enforce": False})
        assert v["state"] == mod.VIOLATION and "enforce=False" in v["detail"]
        assert mod.evaluate("MONTHLY_QUOTA_ENFORCE", "0", {"enforce": True})["state"] == mod.VIOLATION

    def test_missing_flag_is_unknown(self):
        mod = _load()
        assert mod.evaluate("MONTHLY_QUOTA_ENFORCE", "1", {"enforce": None})["state"] == mod.UNKNOWN
        assert mod.evaluate("MONTHLY_QUOTA_ENFORCE", "1", None)["state"] == mod.UNKNOWN


# ── 3 · aggregation ──────────────────────────────────────────────────────────

def _res(mod, **states):
    return {k: {"state": getattr(mod, v), "detail": ""} for k, v in states.items()}


class TestAggregate:
    def test_any_violation_is_error_and_leads_the_note(self):
        mod = _load()
        status, note = mod.aggregate(_res(mod, A="AGREE", B="VIOLATION", C="UNKNOWN"))
        assert status == "error" and note.startswith("B=violation")

    def test_all_agree_is_success(self):
        mod = _load()
        assert mod.aggregate(_res(mod, A="AGREE", B="AGREE"))[0] == "success"

    def test_unknowns_do_not_redden_a_run_that_observed_something(self):
        mod = _load()
        status, note = mod.aggregate(_res(mod, A="AGREE", B="UNKNOWN"))
        assert status == "success" and "B=unknown" in note

    def test_all_unknown_is_blind_not_green(self):
        """The dangerous direction: a probe that could see nothing must not
        write `success`."""
        mod = _load()
        assert mod.aggregate(_res(mod, A="UNKNOWN", B="UNKNOWN"))[0] is None

    def test_note_fits_the_beat_routes_clamp(self):
        mod = _load()
        big = {f"SWITCH_{i}": {"state": mod.AGREE, "detail": ""} for i in range(40)}
        assert len(mod.aggregate(big)[1]) <= mod.NOTE_MAX == 280


# ── 4 · observers (no network: gh / requests swapped for fakes) ──────────────

class TestObservers:
    def test_review_lane_counts_only_review_branches_inside_the_window(self, monkeypatch):
        mod = _load()
        prs = [{"headRefName": "brain/review-x", "createdAt": _iso(30)},
               {"headRefName": "brain/review-old", "createdAt": _iso(300)},
               {"headRefName": "fix/unrelated", "createdAt": _iso(10)},
               {"headRefName": "brain/autofix-y", "createdAt": _iso(10)}]
        seen = {}

        def fake_gh(args):
            seen["args"] = args
            return prs
        monkeypatch.setattr(mod, "_gh_prs", fake_gh)
        obs = mod.observe_review_lane(_now())
        assert obs == {"review_prs_opened": 1, "branches": ["brain/review-x"]}
        assert seen["args"][:2] == ["--state", "all"] and "created:>=2026-08-22T04:00:00+00:00" in seen["args"][-1]

    def test_review_lane_is_unknown_when_gh_fails(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_gh_prs", lambda args: None)
        assert mod.observe_review_lane(_now()) is None

    def test_automerge_counts_only_autofix_merges_inside_the_window(self, monkeypatch):
        mod = _load()
        prs = [{"headRefName": "brain/autofix-a", "mergedAt": _iso(15)},
               {"headRefName": "brain/autofix-b", "mergedAt": _iso(500)},
               {"headRefName": "brain/review-c", "mergedAt": _iso(15)}]
        seen = {}

        def fake_gh(args):
            seen["args"] = args
            return prs
        monkeypatch.setattr(mod, "_gh_prs", fake_gh)
        assert mod.observe_automerge(_now()) == {"autofix_prs_merged": 1, "branches": ["brain/autofix-a"]}
        assert seen["args"][:2] == ["--state", "merged"] and "merged:>=" in seen["args"][-1]

    def test_gh_failure_is_unknown_not_red(self, monkeypatch):
        mod = _load()

        def boom(cmd, **kw):
            raise FileNotFoundError("gh")
        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod._gh_prs(["--state", "all"]) is None
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502"))
        assert mod._gh_prs(["--state", "all"]) is None

    def test_action_classes_404_is_unknown(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None: (404, {"error": "not found"}))
        assert mod.observe_action_classes(_now()) is None

    def test_action_classes_reads_the_step2_field_in_any_of_its_shapes(self):
        mod = _load()
        start = mod._window_start(_now())
        assert mod.count_resolved_by_action_class(
            {"ok": True, "resolved_by_action_class": [{"id": 1, "resolved_at": _iso(20)},
                                                      {"id": 2, "resolved_at": _iso(400)}]}, start) == 1
        assert mod.count_resolved_by_action_class({"ok": True, "counts": {"resolved_by_action_class": 3}}, start) == 3
        assert mod.count_resolved_by_action_class(
            {"ok": True, "rows": [{"id": 1, "resolved_by_action_class": "restart", "finished_at": _iso(5)},
                                  {"id": 2, "resolved_by_action_class": None, "finished_at": _iso(5)},
                                  {"id": 3, "resolved_by_action_class": "restart", "finished_at": _iso(900)}]},
            start) == 1
        # today's inbox shape — no such field anywhere → unknown, not zero
        assert mod.count_resolved_by_action_class({"ok": True, "counts": {"awaiting_ops": 2},
                                                   "rows": [{"id": 1, "status": "awaiting_ops"}]}, start) is None

    def test_action_classes_200_without_the_field_is_reported_as_none(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None:
                            (200, {"ok": True, "counts": {}, "rows": []}))
        assert mod.observe_action_classes(_now()) == {"resolved_by_action_class": None}

    def test_squasher_queue_counts_rows_requested_inside_the_window(self, monkeypatch):
        mod = _load()
        seen = {}

        def fake_get(path, admin=False, timeout=None):
            seen.update(path=path, admin=admin, timeout=timeout)
            return 200, {"ok": True, "rows": [{"id": 1, "requested_at": _iso(30)},
                                              {"id": 2, "requested_at": _iso(3000)},
                                              {"id": 3, "requested_at": None}]}
        monkeypatch.setattr(mod, "_get", fake_get)
        assert mod.observe_squasher_queue(_now()) == {"rows_requested": 1}
        assert seen == {"path": "/api/v1/brain/squasher/queue", "admin": True, "timeout": mod.TIMEOUT_WORKER_PROXIED_S}

    def test_squasher_queue_401_is_unknown(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None: (401, {"ok": False}))
        assert mod.observe_squasher_queue(_now()) is None

    def test_quota_wall_reads_the_published_flag(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None:
                            (200, {"quota_wall": {"enforce": True, "hits_month": 0}}))
        assert mod.observe_quota_wall(_now()) == {"enforce": True}
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None: (200, {"quota_wall": None}))
        assert mod.observe_quota_wall(_now()) == {"enforce": None}
        monkeypatch.setattr(mod, "_get", lambda path, admin=False, timeout=None: (None, None))
        assert mod.observe_quota_wall(_now()) is None

    def test_get_sends_the_probe_ua_and_the_admin_key_only_when_asked(self, monkeypatch):
        mod = _load()
        calls = []

        class _R:
            status_code = 200

            def json(self):
                return {"ok": True}
        monkeypatch.setattr(mod.requests, "get", lambda url, headers=None, timeout=None: calls.append((url, headers, timeout)) or _R())
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        assert mod._get("/api/v1/mcp/funnel") == (200, {"ok": True})
        assert mod._get("/api/v1/brain/squasher/queue", admin=True, timeout=200) == (200, {"ok": True})
        assert calls[0][1]["User-Agent"] == mod.UA and "X-Admin-Key" not in calls[0][1]
        assert calls[1][1]["X-Admin-Key"] == "k-test" and calls[1][2] == 200
        assert calls[0][0].startswith(mod.API_BASE)

    def test_get_without_an_admin_key_never_dials(self, monkeypatch):
        mod = _load()
        monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
        dialled = []
        monkeypatch.setattr(mod.requests, "get", lambda *a, **k: dialled.append(a))
        assert mod._get("/api/v1/brain/squasher/queue", admin=True) == (None, None)
        assert dialled == []

    def test_transport_failure_is_unknown(self, monkeypatch):
        mod = _load()

        def boom(*a, **k):
            raise mod.requests.ConnectionError("refused")
        monkeypatch.setattr(mod.requests, "get", boom)
        assert mod._get("/api/v1/mcp/funnel") == (None, None)


# ── 5 · the beat: one writer, loud ───────────────────────────────────────────

class _Resp:
    def __init__(self, status, body, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.resp


class TestBeat:
    def test_posts_the_contract_the_ledger_expects(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        http = _Http(_Resp(200, {"ok": True, "feed": "kill-switch-probe"}))
        assert mod.beat("success", "A=agree", http=http)["ok"] is True
        c = http.calls[0]
        assert c["url"] == mod.API_BASE + "/api/v1/admin/ingest-runs/beat"
        assert c["json"] == {"feed": "kill-switch-probe", "status": "success", "cadence_hours": 3, "note": "A=agree"}
        assert c["headers"]["X-Admin-Key"] == "k-test" and c["headers"]["User-Agent"] == mod.UA
        assert c["timeout"] > 180

    def test_non_2xx_raises(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        with pytest.raises(RuntimeError, match="HTTP 401"):
            mod.beat("success", "x", http=_Http(_Resp(401, {"ok": False, "error": "admin key required"})))

    def test_ok_false_raises_even_on_200(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        with pytest.raises(RuntimeError, match="ok!=true"):
            mod.beat("success", "x", http=_Http(_Resp(200, {"ok": False, "error": "no DATABASE_URL"})))

    def test_non_json_raises(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        with pytest.raises(RuntimeError, match="non-JSON"):
            mod.beat("success", "x", http=_Http(_Resp(200, ValueError("no json"), text="<html>")))

    def test_missing_admin_key_raises_before_dialling(self, monkeypatch):
        mod = _load()
        monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
        http = _Http(_Resp(200, {"ok": True}))
        with pytest.raises(RuntimeError, match="DCHUB_ADMIN_KEY"):
            mod.beat("success", "x", http=http)
        assert http.calls == []

    def test_the_response_is_printed_in_full(self, monkeypatch, capsys):
        mod = _load()
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
        text = '{"ok": true, "feed": "kill-switch-probe", "padding": "' + "x" * 600 + '"}'
        mod.beat("success", "x", http=_Http(_Resp(200, {"ok": True}, text=text)))
        assert text in capsys.readouterr().out


# ── 6 · main(): end to end with fake observers ───────────────────────────────

def _main(monkeypatch, mod, observations, beat_resp=None):
    beats = []
    monkeypatch.setattr(mod, "OBSERVERS", {k: (lambda v: (lambda now: v))(v) for k, v in observations.items()})
    monkeypatch.setattr(mod, "beat", lambda status, note, http=None: beats.append((status, note)) or beat_resp)
    return mod.main([]), beats


def _healthy():
    return {"review_lane": {"review_prs_opened": 0}, "automerge": {"autofix_prs_merged": 1},
            "action_classes": {"resolved_by_action_class": 0}, "squasher_queue": {"rows_requested": 2},
            "quota_wall": {"enforce": True}}


class TestMain:
    def test_set_zero_but_the_lane_opened_a_pr_beats_error_and_fails_the_run(self, monkeypatch, capsys):
        mod = _load()
        obs = _healthy()
        obs["review_lane"] = {"review_prs_opened": 1, "branches": ["brain/review-zz"]}
        code, beats = _main(monkeypatch, mod, obs)
        assert code == 1
        assert len(beats) == 1 and beats[0][0] == "error"
        assert beats[0][1].startswith("BRAIN_REVIEW_LANE_ENABLED=violation")
        assert "::error::" in capsys.readouterr().out

    def test_all_agree_beats_success_and_exits_zero(self, monkeypatch):
        mod = _load()
        code, beats = _main(monkeypatch, mod, _healthy())
        assert code == 0 and beats == [("success", "; ".join(f"{n}=agree" for n in mod.SWITCHES))]

    def test_a_404_surface_is_unknown_and_the_run_stays_green(self, monkeypatch):
        mod = _load()
        obs = _healthy()
        obs["action_classes"] = None
        code, beats = _main(monkeypatch, mod, obs)
        assert code == 0 and beats[0][0] == "success" and "ACTION_CLASSES_ENABLED=unknown" in beats[0][1]

    def test_a_blind_run_beats_nothing_and_exits_two(self, monkeypatch, capsys):
        mod = _load()
        code, beats = _main(monkeypatch, mod, {k: None for k in _healthy()})
        assert code == 2 and beats == []
        assert "::error::" in capsys.readouterr().out

    def test_a_crashing_observer_is_unknown_not_a_crash(self, monkeypatch):
        mod = _load()

        def boom(now):
            raise RuntimeError("gh exploded")
        obs = _healthy()
        monkeypatch.setattr(mod, "OBSERVERS", {**{k: (lambda v: (lambda now: v))(v) for k, v in obs.items()},
                                               "review_lane": boom})
        res = mod.run()
        assert res["BRAIN_REVIEW_LANE_ENABLED"]["state"] == mod.UNKNOWN
        assert res["MONTHLY_QUOTA_ENFORCE"]["state"] == mod.AGREE

    def test_a_refused_beat_propagates(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "OBSERVERS", {k: (lambda v: (lambda now: v))(v) for k, v in _healthy().items()})

        def refuse(status, note, http=None):
            raise RuntimeError("beat returned HTTP 500")
        monkeypatch.setattr(mod, "beat", refuse)
        with pytest.raises(RuntimeError, match="HTTP 500"):
            mod.main([])


# ── 7 · the workflow ─────────────────────────────────────────────────────────

def _wf():
    doc = yaml.safe_load(WF.read_text(encoding="utf-8"))
    return doc, (doc.get("on") or doc.get(True))


class TestWorkflow:
    def test_runs_every_two_hours_on_a_real_cron(self):
        doc, on = _wf()
        crons = [s["cron"] for s in on["schedule"]]
        assert len(crons) == 1 and re.fullmatch(r"\d+ \*/2 \* \* \*", crons[0]), crons
        assert "workflow_dispatch" in on

    def test_runs_the_probe_module_with_the_admin_key_from_secrets_via_env(self):
        doc, _ = _wf()
        steps = doc["jobs"]["probe"]["steps"]
        probe = [s for s in steps if "tools/kill_switch_probe.py" in (s.get("run") or "")]
        assert len(probe) == 1
        env = probe[0].get("env") or {}
        assert env.get("DCHUB_ADMIN_KEY") == "${{ secrets.DCHUB_ADMIN_KEY }}"
        assert env.get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}"
        assert env.get("GH_REPO") == "${{ github.repository }}"
        assert "${{" not in probe[0]["run"], "expressions reach the probe through env:, never the script body"

    def test_no_curl_no_github_script_no_truncation(self):
        doc, _ = _wf()
        text = WF.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "| head -c" not in code and "head -c" not in code
        assert "github-script" not in code
        assert not re.search(r"\bcurl\b", code), "curl would move the unguarded-curl ratchet; the probe uses requests"
        for s in doc["jobs"]["probe"]["steps"]:
            assert "github-script" not in str(s.get("uses") or "")

    def test_loud_path_has_the_scope_it_needs_and_does_not_swallow_the_filing(self):
        doc, _ = _wf()
        perms = doc["jobs"]["probe"].get("permissions", doc.get("permissions"))
        assert perms.get("issues") == "write" and perms.get("pull-requests") == "read"
        loud = [s for s in doc["jobs"]["probe"]["steps"] if s.get("if") == "failure()"]
        assert len(loud) == 1 and "gh issue create" in loud[0]["run"]
        for ln in loud[0]["run"].splitlines():
            if "gh issue create" in ln or "gh issue comment" in ln:
                assert "|| true" not in ln and "|| echo" not in ln
        assert "${{" not in loud[0]["run"]

    def test_job_timeout_covers_two_worker_proxied_reads(self):
        doc, _ = _wf()
        mod = _load()
        assert doc["jobs"]["probe"]["timeout-minutes"] * 60 > 2 * mod.TIMEOUT_WORKER_PROXIED_S + 120
