"""Guard: a scheduled workflow that calls production must not look like demand.

2026-08-26. Our own cron fleet calls dchub.cloud ~1,000 times a day. Every one
of those calls lands in mcp_call_log, and the read layer separates ours from
real agents with exactly one test: `mcp_calls_deloop.real_ua_predicate`, a regex
over the User-Agent. A workflow whose UA matches no family in that regex is
counted as an external agent — silently, and forever.

That was live. `cron-heartbeat.yml` sent `GH-Actions-CronHeartbeat/1.0`, which
matches nothing in the predicate, at 288 calls/day. It was the ONLY scheduled
workflow doing so — all 17 other prod-calling crons already carried a `dchub`
UA — which is exactly why it was easy to miss: the fleet looked clean.

Sibling to test_qa_smoke_ua_exclusion.py, which pins OBSERVED QA-fleet UAs
against the same predicate. This one pins the UAs we SHIP in workflow files, so
a new cron cannot introduce the problem in the first place.

Pure: no DB, no network, never imports main.
"""
import os
import re
import glob

import pytest

dl = pytest.importorskip("mcp_calls_deloop")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")


# ★ DERIVED from the predicate, never transcribed — same rule as
# test_qa_smoke_ua_exclusion._ua_excluded. An earlier draft of this file parsed
# the _SCRIPT_INTERNAL_UA literal out of the source instead, and that block
# interleaves comments containing their own double-quoted phrases: joining the
# string parts without stripping comments corrupted the alternation into
# something that silently stopped matching `dchub`, and produced a confidently
# wrong list of 18 offenders. Lifting the rendered regex has no such failure
# mode. Lazy, so a shape change is a FAILURE rather than a collection error.
def _ua_excluded(ua: str) -> bool:
    m = re.search(r"!~\*\s+'\((.+)\)'", dl.real_ua_predicate())
    assert m, "real_ua_predicate() no longer renders a !~* '(...)' regex"
    return bool(re.search(m.group(1), ua or "", re.I))


def _cron_workflows_calling_prod():
    """(filename, {declared UAs}) for every scheduled workflow that hits prod."""
    out = []
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.y*ml"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        if "cron:" not in text or "dchub.cloud" not in text:
            continue
        uas = {u.strip() for u in re.findall(r"[Uu]ser-[Aa]gent:\s*([^\"'\\\n]+)", text)}
        uas |= {u.strip() for u in re.findall(r"-A\s+['\"]([^'\"]+)", text)}
        out.append((os.path.basename(path), {u for u in uas if u}))
    return out


def test_the_predicate_is_two_sided():
    """A guard built on a mangled regex passes vacuously, and an over-broad one
    deletes real agents from the demand numbers. Pin both directions."""
    for ua in ("curl/8.7.1", "dchub-uptime-probe/1.0", "dchub-ingest-beat/1.0",
               "DCHub-SLA-Visitor/1.0", "dchub-cron-heartbeat/1.0"):
        assert _ua_excluded(ua), f"{ua} should be excluded as self-traffic"
    for ua in ("claude-ai/1.0", "Mozilla/5.0 (Windows NT 10.0)",
               "GH-Actions-CronHeartbeat/1.0"):
        assert not _ua_excluded(ua), (
            f"{ua} must NOT be excluded — an over-broad predicate silently "
            "deletes real agents to make a metric prettier")


def test_scheduled_workflows_that_call_prod_declare_themselves_as_ours():
    offenders = []
    for name, uas in _cron_workflows_calling_prod():
        for ua in uas:
            if not _ua_excluded(ua):
                offenders.append(f"{name}: {ua!r}")
    assert not offenders, (
        "These scheduled workflows call production under a User-Agent the funnel "
        "counts as REAL external demand. Rename the UA into the canonical self "
        "family (include 'dchub'), or our own cron becomes a customer in a "
        "published number:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_scan_is_not_vacuous():
    """A zero-offender result is only evidence if the scan found workflows.

    Floor is pinned in tests/scan_floors.json; this is the in-file companion
    that also proves UAs were actually extracted, not just files counted.
    """
    found = _cron_workflows_calling_prod()
    assert len(found) >= 10, (
        f"only {len(found)} prod-calling cron workflows found — the scan broke, "
        "and its green result carries no information")
    assert sum(1 for _, uas in found if uas) >= 5, (
        "no workflow declared a UA — the extractor is broken")
