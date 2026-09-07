"""A SOFT feed family must not red the pulse — and must not go quiet either.

WHY THIS TIER EXISTS (2026-09-07): ENTSO-E's API went down and data-pulse went
red 27 consecutive times. Measured: web-api.tp.entsoe.eu answers HTTP 599 with
its own gateway body {"uuAppErrorMap":{"uu-gateway-router/connectTimeout":...}},
reproduced from a laptop with NO securityToken — which rules out our token (a
live service answers 401), our network, our code, and our timeout. Nothing in
this repo could fix it, and the ISO orchestrator inside that same job kept
working the whole time: MISO and CAISO ingested normally through every red run.

A badge that is red for something you cannot fix teaches people to ignore the
badge, and the next red is a real ISO failure.

★ BUT THE D1 GATE (2026-09-02) EXISTS FOR A REASON, AND THIS MUST NOT UNDO IT.
  Before D1, ENTSO-E answered 503 for over 24h while this job stayed green —
  50 hours of a whole family dark and nothing said so. Softening the EXIT must
  not soften the REPORTING. So the invariant this file guards is not "entsoe is
  soft"; it is:

      a soft family is still announced, still notified, still heartbeaten, and
      STILL BEATS THE PER-FAMILY DEADMAN with its real status.

  The deadman beat is the load-bearing one: it is what makes the ledger row age
  and alarm. It works today only because it runs UNCONDITIONALLY, after the
  orchestrator step. The moment someone adds an `if:` to it, "soft" becomes
  "silent" and we are back to 50 hours of nothing. test_the_deadman_beat_step_
  is_unconditional is the assertion that matters most in this file.

★ Stdlib + pyyaml (already a CI dependency). Reads ONE file — no repo scan, so
  no scan_floors.json floor is required.
"""
import os
import re

import pytest
import yaml

_WF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".github", "workflows", "data-pulse.yml")


@pytest.fixture(scope="module")
def wf():
    with open(_WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def raw():
    with open(_WF, encoding="utf-8") as fh:
        return fh.read()


def _steps(wf):
    return wf["jobs"]["pulse"]["steps"]


def _step(wf, needle):
    for s in _steps(wf):
        if needle.lower() in str(s.get("name", "")).lower():
            return s
    raise AssertionError(f"step not found: {needle}")


def _fams(wf, var):
    return [f.strip() for f in str(wf.get("env", {}).get(var) or "").split(",")
            if f.strip()]


# ── 1 · the tiers ────────────────────────────────────────────────────────────

def test_entsoe_is_soft_not_must(wf):
    assert "iso-eu-entsoe" in _fams(wf, "SOFT_HAVE_FAMILIES")
    assert "iso-eu-entsoe" not in _fams(wf, "MUST_HAVE_FAMILIES")


def test_entsoe_is_not_simply_deleted(wf):
    """★ The failure mode this replaces is dropping the family entirely, which
    reads as 'handled' and is not. It must be in exactly ONE tier."""
    tiers = _fams(wf, "MUST_HAVE_FAMILIES") + _fams(wf, "SOFT_HAVE_FAMILIES")
    assert tiers.count("iso-eu-entsoe") == 1, tiers


# ── 2 · the invariant that keeps SOFT from meaning SILENT ────────────────────

@pytest.mark.parametrize("step_name", [
    "Beat the per-family deadman feeds",
    "Notify the autonomous intelligence brain",
    "Heartbeat the source registry",
])
def test_the_reporting_steps_are_unconditional(wf, step_name):
    """★ THE ONE THAT MATTERS. A soft family is only 'reported, not red' while
    these still run. An `if:` here turns soft into silent and restores the 50h
    blind spot D1 was written to close."""
    step = _step(wf, step_name)
    assert "if" not in step, (
        f"{step_name!r} gained an `if:` — a soft family's failure would stop "
        f"being recorded, which is the regression this tier must never cause")


def test_the_deadman_beat_copies_status_and_never_asserts_one(raw):
    """The beat must forward the orchestrator's derived status, not a literal.
    A hardcoded 'success' here would age nothing and alarm never."""
    assert '"status": fam.get("status")' in raw
    for literal in ('"status": "success"', "'status': 'success'"):
        assert literal not in raw, literal


# ── 3 · soft does not exit, must does ────────────────────────────────────────

def test_only_must_have_fails_the_job(wf):
    step = _step(wf, "Fail the job when a must-have feed family failed")
    cond = str(step.get("if", ""))
    assert "must_have_failed" in cond, cond
    assert "soft_have_failed" not in cond, (
        "the soft tier must never reach the exit-1 step")
    assert "exit 1" in str(step.get("run", ""))


def test_soft_failure_is_announced_as_a_warning(raw):
    """Visible in the run and in the Actions annotation list — just not red."""
    assert '_check(_families("SOFT_HAVE_FAMILIES"), "warning"' in raw
    assert '_check(_families("MUST_HAVE_FAMILIES"), "error"' in raw


def test_soft_failure_reaches_the_pulse_summary(raw):
    assert "soft_have_failed" in raw and "SOFT FAMILY DOWN" in raw
    assert "ops/deadman" in raw, "say where the durable alarm lives"


# ── 4 · the classifier itself ────────────────────────────────────────────────

def test_the_tier_check_treats_both_tiers_identically(raw):
    """Only the annotation level and the exit differ. If the two tiers ever
    diverge in how they DERIVE the verdict, a soft family could be judged by a
    looser rule than a must-have one."""
    body = re.search(r"def _check\(feeds, level, label\):(.+?)return out",
                     raw, re.S)
    assert body, "the shared classifier is gone — tiers may have diverged"
    assert 'st not in ("success", "no_new_data")' in body.group(1)


@pytest.mark.parametrize("status,should_flag", [
    ("success", False), ("no_new_data", False),
    ("failed", True), ("degraded", True),
    ("awaiting_upstream", True), ("missing", True),
])
def test_verdict_matches_the_orchestrators_vocabulary(status, should_flag):
    """Mirrors the workflow's rule against the statuses
    routes/iso_orchestrator.summarize_families can actually emit."""
    flagged = status not in ("success", "no_new_data")
    assert flagged is should_flag, status
