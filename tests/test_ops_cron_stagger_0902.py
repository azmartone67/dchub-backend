"""D15 + finding 5 (2026-09-02): seven cron collisions staggered; bug-squash beats
the deadman as `degraded` when the frontend was not scanned.

Reads the workflow YAML, never a comment. The seven expressions the radar's
check_cron_collisions flagged on 09-02 must each be owned by ONE backend
workflow, and no backend cron expression may be shared at all.
"""
from __future__ import annotations

import collections
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")

# the 09-02 collisions (radar cron_schedule_collision)
_WERE_SHARED = ("7 */6 * * *", "*/30 * * * *", "40 5 * * *", "29 */2 * * *",
                "23 */6 * * *", "11 14 * * 1", "17 */6 * * *")


def _crons():
    out = collections.defaultdict(list)
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WF_DIR, fn), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh.read()) or {}
        trig = doc.get(True) or doc.get("on") or {}
        for e in (trig.get("schedule") or []) if isinstance(trig, dict) else []:
            if isinstance(e, dict) and e.get("cron"):
                out[" ".join(str(e["cron"]).split())].append(fn)
    return out


def test_no_two_backend_workflows_share_a_cron_expression():
    # Kills: re-introducing any exact-minute collision.
    shared = {k: v for k, v in _crons().items() if len(v) > 1}
    assert not shared, f"colliding crons: {shared}"


def test_the_seven_staggered_expressions_have_one_owner_each():
    crons = _crons()
    for expr in _WERE_SHARED:
        assert len(crons.get(expr, [])) <= 1, (expr, crons.get(expr))


def test_bug_squash_beats_the_deadman_degraded_when_frontend_is_absent():
    with open(os.path.join(WF_DIR, "bug-squash-nightly.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh.read())
    steps = doc["jobs"]["squash"]["steps"] if "squash" in doc["jobs"] else \
        next(iter(doc["jobs"].values()))["steps"]
    beat = [s for s in steps if "ingest-runs/beat" in (s.get("run") or "")]
    assert len(beat) == 1, "exactly one dead-man beat step"
    run = beat[0]["run"]
    assert beat[0].get("if") == "always()"
    assert '"feed\\":\\"bug-squash-nightly' in run or "bug-squash-nightly" in run
    # degraded on a failed frontend checkout, success otherwise — read the shell
    assert re.search(r'STATUS="degraded"', run)
    assert re.search(r'STATUS="success"', run)
    assert "FRONTEND_OUTCOME" in run and "steps.frontend.outcome" in str(beat[0].get("env"))
