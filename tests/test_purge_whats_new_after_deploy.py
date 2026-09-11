"""scripts/purge_whats_new_after_deploy.py — the post-deploy purge must purge in
the right order, read the way the page reads, and fail loudly.

2026-09-11: after dchub-backend#4405 deployed, a zone purge of
/api/v1/whats-new?d=<date> was answered from the Pages worker's still-fresh KV
copy of the OLD cards and stored again. These tests drive run() against a model
of the two copies, so the ordering rule (purge only once the worker's copy is
current) and every loud failure are shown to hold.
"""
import datetime
import importlib.util
import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "whats-new-post-deploy-purge.yml"
_spec = importlib.util.spec_from_file_location(
    "purge_whats_new_after_deploy", REPO / "scripts" / "purge_whats_new_after_deploy.py")
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

DAY = "/api/v1/whats-new?d=2026-09-11"
BARE = "/api/v1/whats-new"
PATHS = [DAY, BARE]
OLD = ["capabilities-live", "playground"]
NEW = ["mcp-pack-siting", "capabilities-live", "playground"]


def body(ids):
    return None if ids is None else {"platform": [{"id": i} for i in ids]}


class Model:
    """Scripted answers per seam. A list is consumed one entry per call and its
    last entry repeats. Every call and every sleep lands in one event log."""

    def __init__(self, origin, worker, page, purge_ok=True):
        self.origin, self.worker, self.page = origin, worker, page
        self.purge_ok = purge_ok
        self.events = []
        self.t = 0.0

    @staticmethod
    def _next(table, path):
        seq = table[path]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def get_json(self, url, cookie=None):
        if url.startswith(purge.ORIGIN):
            path = url[len(purge.ORIGIN):]
            ids = self._next(self.origin, path)
            self.events.append(("origin", path, ids))
            return 200, {}, body(ids)
        assert url.startswith(purge.EDGE + "/"), url
        path = url[len(purge.EDGE):]
        if cookie is not None:
            zone, ids = self._next(self.worker, path)
            self.events.append(("worker", path, ids, cookie))
            return 200, {"cf-cache-status": zone}, body(ids)
        ids = self._next(self.page, path)
        self.events.append(("page", url, ids))
        return 200, {"cf-cache-status": "HIT"}, body(ids)

    def purge(self, zone, token, urls):
        self.events.append(("purge", tuple(urls)))
        return self.purge_ok, "modelled"

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))
        self.t += seconds

    def monotonic(self):
        return self.t

    def run(self, token="t", zone="z", rounds=3, budget_s=None):
        # ★ No `budget_s=purge.BUDGET_S` default: a default is evaluated in the class
        # body, where `purge` is the method above, not the script module.
        if budget_s is None:
            budget_s = purge.BUDGET_S
        return purge.run(self, token, zone, list(PATHS), rounds=rounds, budget_s=budget_s,
                         sleep=self.sleep, monotonic=self.monotonic, log=lambda *_: None)

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


def healthy(**over):
    kw = dict(origin={DAY: [NEW], BARE: [NEW]},
              worker={DAY: [("DYNAMIC", NEW)], BARE: [("DYNAMIC", NEW)]},
              page={DAY: [NEW], BARE: [NEW]})
    kw.update(over)
    return Model(**kw)


def test_the_page_key_is_the_utc_date_the_page_builds_plus_the_bare_feed():
    evening_in_chicago = datetime.datetime(
        2026, 9, 11, 20, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
    assert purge.page_paths(evening_in_chicago) == [
        "/api/v1/whats-new?d=2026-09-12", "/api/v1/whats-new"]


def test_a_healthy_deploy_purges_once_and_passes():
    m = healthy()
    assert m.run() == 0
    assert m.kinds("purge") == [("purge", tuple(purge.EDGE + p for p in PATHS))]


def test_it_purges_only_after_the_workers_copy_carries_the_new_cards():
    m = healthy(worker={DAY: [("DYNAMIC", OLD), ("DYNAMIC", OLD), ("DYNAMIC", NEW)],
                        BARE: [("MISS", OLD), ("DYNAMIC", NEW)]})
    assert m.run() == 0
    first_purge = next(i for i, e in enumerate(m.events) if e[0] == "purge")
    for path in PATHS:
        reads = [e for e in m.events[:first_purge] if e[0] == "worker" and e[1] == path]
        assert reads, f"no worker-copy read for {path} before the purge"
        assert reads[-1][2] == NEW, f"purged while the worker still held the old cards for {path}"


def test_both_urls_are_watched_in_the_same_pass():
    m = healthy(worker={DAY: [("DYNAMIC", OLD), ("DYNAMIC", OLD), ("DYNAMIC", NEW)],
                        BARE: [("DYNAMIC", NEW)]})
    assert m.run() == 0
    assert len([e for e in m.kinds("worker") if e[1] == BARE]) == 1, \
        "a URL already current was probed again"
    first_purge = next(i for i, e in enumerate(m.events) if e[0] == "purge")
    waited = sum(e[1] for e in m.events[:first_purge] if e[0] == "sleep")
    origin_sleeps = 2 * 2 * purge.VERIFY_INTERVAL_S
    assert waited - origin_sleeps == 2 * purge.PROBE_INTERVAL_S, \
        "the two URLs were waited on one after the other"


def test_the_worker_probe_skips_the_zone_but_not_the_worker():
    m = healthy()
    m.run()
    assert {e[3] for e in m.kinds("worker")} == {purge.ZONE_BYPASS_COOKIE}
    name = purge.ZONE_BYPASS_COOKIE.split("=", 1)[0]
    assert "sid" in name, "zone rule 24 bypasses /api/ for cookies containing 'sid'"
    assert not re.fullmatch(r"auth_token|token|dchub_token", name), \
        "the worker's credential check would skip its KV lane for this cookie"


def test_the_verification_reads_like_the_page_no_cookie_no_cache_buster():
    m = healthy()
    m.run()
    assert {e[1] for e in m.kinds("page")} == {purge.EDGE + p for p in PATHS}


def test_a_straggler_that_restores_the_old_body_gets_another_round():
    m = healthy(page={DAY: [OLD, NEW], BARE: [NEW]})
    assert m.run() == 0
    assert len(m.kinds("purge")) == 2


def test_still_stale_after_every_round_is_red():
    m = healthy(page={DAY: [OLD], BARE: [NEW]})
    assert m.run(rounds=3) == 1
    assert len(m.kinds("purge")) == 3


def test_it_stops_with_a_verdict_when_the_budget_runs_out():
    m = healthy(worker={DAY: [("DYNAMIC", OLD)], BARE: [("DYNAMIC", OLD)]},
                page={DAY: [OLD], BARE: [OLD]})
    assert m.run(rounds=10, budget_s=900) == 1
    assert len(m.kinds("purge")) < 10
    assert m.t <= 900 + purge.PROBE_INTERVAL_S + 3 * purge.VERIFY_INTERVAL_S, \
        "a round started after the budget had run out"


def test_a_missing_token_is_red_before_anything_is_touched():
    m = healthy()
    assert m.run(token="") == 2
    assert m.events == []


def test_a_missing_zone_id_is_red_before_anything_is_touched():
    m = healthy()
    assert m.run(zone="") == 2
    assert m.events == []


def test_cloudflare_refusing_the_purge_is_red_and_nothing_is_verified():
    m = healthy(purge_ok=False)
    assert m.run() == 2
    assert m.kinds("page") == []


def test_the_origin_is_read_until_its_replicas_agree():
    m = healthy(origin={DAY: [OLD, NEW, NEW, NEW], BARE: [NEW]})
    assert m.run() == 0
    assert [e[2] for e in m.kinds("origin") if e[1] == DAY] == [OLD, NEW, NEW, NEW]


def test_an_origin_that_never_answers_is_blind_and_purges_nothing():
    m = healthy(origin={DAY: [None], BARE: [NEW]})
    assert m.run() == 3
    assert m.kinds("purge") == []


def test_a_probe_the_zone_answers_waits_out_the_worker_freshness_before_purging():
    m = healthy(worker={DAY: [("HIT", OLD)], BARE: [("DYNAMIC", NEW)]})
    assert m.run() == 0
    first_purge = next(i for i, e in enumerate(m.events) if e[0] == "purge")
    waited = sum(e[1] for e in m.events[:first_purge] if e[0] == "sleep")
    assert waited >= purge.KV_FRESH_TTL_S


def test_the_zone_id_is_read_from_the_pinned_canon():
    assert re.fullmatch(r"[0-9a-f]{32}", purge.zone_id_from_canon())


def test_the_workflow_waits_for_the_deploy_then_runs_the_purge_with_the_token():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))            # PyYAML reads a bare `on:` key as True
    paths = on["push"]["paths"]
    for trigger in ("data/platform_updates.json", "routes/platform_updates.py",
                    "routes/infra_growth.py"):
        assert trigger in paths, f"a change to {trigger} does not run the purge"
    job = wf["jobs"]["purge"]
    runs = [s.get("run", "") for s in job["steps"]]
    i_wait = next(i for i, r in enumerate(runs) if "scripts/wait_for_deployed_commit.py" in r)
    i_purge = next(i for i, r in enumerate(runs) if "scripts/purge_whats_new_after_deploy.py" in r)
    assert i_wait < i_purge, "the purge step runs before the origin serves the commit"
    step = job["steps"][i_purge]
    assert step.get("env", {}).get("CLOUDFLARE_API_TOKEN") == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    assert "if" not in step, "a condition could let the purge run after a failed deploy wait"
    assert not any(s.get("continue-on-error") for s in job["steps"])
    assert job["timeout-minutes"] * 60 >= 600 + purge.BUDGET_S, \
        "the job can be killed before the script reaches its own verdict"


def test_the_workflow_command_line_is_one_the_script_accepts():
    """Parse the workflow's own command with the script's real parser: a typo'd
    flag would otherwise first fail after a merge, on the deploy it was for."""
    import shlex
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["purge"]["steps"]
                if "scripts/purge_whats_new_after_deploy.py" in s.get("run", ""))
    argv = shlex.split(step["run"])
    assert argv[:2] == ["python3", "scripts/purge_whats_new_after_deploy.py"]
    args = purge.build_parser().parse_args(argv[2:])
    assert (args.edge, args.origin) == (purge.EDGE, purge.ORIGIN), \
        "the workflow points the purge at a host other than the page's"
