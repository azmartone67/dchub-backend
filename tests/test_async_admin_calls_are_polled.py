"""A 202 means "spawned". It does not mean "finished", and it never means "worked".

★ WHY THIS EXISTS. mcp-registry-watch.yml POSTed a registry scan synchronously
with `curl --max-time 30` and lost 7 of 12 runs to
`curl: (28) Operation timed out after 30002 milliseconds`. The 30s budget was
never the real ceiling: Cloudflare ROUTE_TIMEOUTS kills an admin route at 15s,
so no curl timeout could have made a blocking call survive a scan that probes
10 registries at 8s each. The work always completed at the origin — only the
answer was lost, and with it every run's summary.

The repair is the house pattern from routes/osm_crawler.py: `?async=1` spawns
and returns 202, and the caller polls. But a caller that fires `async=1` and
then just exits 0 has traded a loud timeout for a silent no-op — it would
report success for a scan thread that died on its first line. That is strictly
worse than the timeout it replaced, and it is the shape this fence bans.

★ WHAT IT ASSERTS, on the executable body. It reads each step's parsed `run:`
script — never a comment — and requires any step firing an `async=1` admin call
to also contain a bounded retry loop. Verified against both current callers
(mcp-registry-watch, osm-crawl); both poll.
"""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")

_ASYNC_CALL = re.compile(r"async=1")
# `for i in $(seq 1 N)` or a while-loop — a bounded wait for real evidence.
_POLL_LOOP = re.compile(r"\bfor\s+\w+\s+in\s+\$\(seq\b|\bwhile\b")


def _run_steps():
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WF_DIR, fn), encoding="utf-8") as fh:
            try:
                doc = yaml.safe_load(fh)
            except yaml.YAMLError:
                continue
        if not isinstance(doc, dict):
            continue
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in (job.get("steps") or []):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    yield fn, job_name, step.get("name", "<unnamed>"), step["run"]


def test_a_spawned_admin_job_is_polled_for_proof():
    offenders = []
    for fn, job, name, run in _run_steps():
        if not _ASYNC_CALL.search(run):
            continue
        if not _POLL_LOOP.search(run):
            offenders.append(f"  {fn} :: job {job} :: step {name!r}")
    assert not offenders, (
        "these steps fire an `async=1` admin call and never wait for evidence "
        "that it finished. A 202 says the thread was spawned; it says nothing "
        "about whether the work ran. Poll for a changed timestamp and fail "
        "loudly if it never moves:\n" + "\n".join(offenders))
