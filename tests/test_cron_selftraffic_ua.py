"""Guard: a scheduled workflow that calls production must not look like demand.

2026-08-26. Our own cron fleet calls dchub.cloud ~1,000 times a day. Every one
of those calls lands in mcp_call_log, and the read layer separates ours from
real agents with exactly one test: `mcp_calls_deloop.real_ua_predicate`, a regex
over the User-Agent. A workflow whose UA matches no family in that regex is
counted as an external agent — silently, and forever.

That was live. `cron-heartbeat.yml` sent `GH-Actions-CronHeartbeat/1.0`, which
matches nothing in `_SCRIPT_INTERNAL_UA`, at 288 calls/day. It was the ONLY
scheduled workflow doing so — all 17 other prod-calling crons already carried a
`dchub` UA — which is exactly why it was easy to miss: the fleet looked clean.

This test reads BOTH sides from source (the workflow files and the live regex)
so it cannot drift from either, and fails by name when a new workflow calls
production under a UA the funnel would count as a customer.
"""
import os
import re
import sys
import glob

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")


def _self_ua_regex():
    """The LIVE exclusion pattern, read out of mcp_calls_deloop.

    Comment lines are stripped BEFORE the string literals are joined: the block
    interleaves comments that themselves contain double-quoted phrases, and
    including them corrupts the alternation into something that silently stops
    matching `dchub`. Verified below against known-good and known-bad UAs.
    """
    src = open(os.path.join(REPO_ROOT, "mcp_calls_deloop.py"), encoding="utf-8").read()
    i = src.index("_SCRIPT_INTERNAL_UA = (")
    j = src.index("\n)", i)
    body = "\n".join(
        ln for ln in src[i:j].splitlines() if not ln.strip().startswith("#")
    )
    pat = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body))
    return re.compile(pat, re.I)


def _cron_workflows_calling_prod():
    out = []
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.y*ml"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        if "cron:" not in text or "dchub.cloud" not in text:
            continue
        uas = {u.strip() for u in re.findall(r"[Uu]ser-[Aa]gent:\s*([^\"'\\\n]+)", text)}
        uas |= {u.strip() for u in re.findall(r"-A\s+['\"]([^'\"]+)", text)}
        out.append((os.path.basename(path), {u for u in uas if u}))
    return out


def test_the_extracted_regex_is_the_real_one():
    """A guard built on a mangled regex passes vacuously. Pin both directions."""
    rx = _self_ua_regex()
    for ua in ("curl/8.7.1", "dchub-uptime-probe/1.0", "dchub-ingest-beat/1.0",
               "DCHub-SLA-Visitor/1.0", "dchub-cron-heartbeat/1.0"):
        assert rx.search(ua), f"{ua} should be excluded as self-traffic"
    for ua in ("claude-ai/1.0", "Mozilla/5.0 (Windows NT 10.0)",
               "GH-Actions-CronHeartbeat/1.0"):
        assert not rx.search(ua), (
            f"{ua} must NOT be excluded — if it is, the regex is over-broad and "
            "real agents are being deleted from the demand numbers")


def test_scheduled_workflows_that_call_prod_declare_themselves_as_ours():
    rx = _self_ua_regex()
    offenders = []
    for name, uas in _cron_workflows_calling_prod():
        for ua in uas:
            if not rx.search(ua):
                offenders.append(f"{name}: {ua!r}")
    assert not offenders, (
        "These scheduled workflows call production under a User-Agent the funnel "
        "counts as REAL external demand. Rename the UA into the canonical self "
        "family (include 'dchub'), or the traffic becomes a customer in a "
        "published number:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_scan_actually_scanned_something():
    """A zero-offender result is only evidence if the scan found workflows."""
    found = _cron_workflows_calling_prod()
    assert len(found) >= 10, (
        f"only {len(found)} prod-calling cron workflows found — the scan broke, "
        "and its green result carries no information")
    with_ua = [n for n, uas in found if uas]
    assert len(with_ua) >= 5, "no workflow declared a UA — the extractor is broken"
