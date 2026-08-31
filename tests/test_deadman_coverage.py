"""The deadman board must not be silent about work it does not watch.

★ WHAT THE BATCH-3 SWEEP MEASURED (2026-08-30). /api/v1/ops/deadman reported
`tracked: 150, overdue_count: 0` — which reads as full coverage and total
health. Cross-checking every scheduled workflow against the GitHub Actions API
showed the board watched 36 of 143 scheduled workflows. Sixty-seven of the 107
unwatched ones do real work against production, and two were failing at the
time of the sweep. One of them, mcp-facts-export, had failed SIXTY CONSECUTIVE
RUNS across 17 days while serving a 30-day-stale public AI-discovery surface.

★ THE BEAT MECHANISMS THEMSELVES WERE CORRECT. All three were read and checked:
dchub-scheduler beats success-only behind an `if 200 <= status < 300` guard;
crawler_scheduler beats inside a `finally` but its bare `except Exception` sets
`status` first, so it cannot fabricate a success; tools/deadman/watch.py marks a
workflow whose newest completed run failed with a status outside _OK_STATUS, and
that was verified against all 36 watched workflows. The deadman was not lying.
IT WAS NOT LOOKING — which no amount of auditing the beat path would have found.

★ WHAT THIS FENCE ASSERTS. Every scheduled workflow that touches production is
either watched or explicitly listed with a reason. It reads the parsed workflow
YAML and the AST of watch.py — never prose — so it cannot be satisfied by a
comment claiming coverage. And the untriaged backlog RATCHETS DOWN: a new
unwatched producer cannot join silently, the way all 67 of these did.
"""
import ast
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
WATCH_PY = os.path.join(ROOT, "tools", "deadman", "watch.py")

# A workflow "touches production" when its YAML names a live API surface. This
# UNDER-counts on purpose rather than guessing: a workflow that shells into
# Python which then calls the API is not matched here. See the note in watch.py.
_PRODUCTION = re.compile(r"dchub\.cloud/api/|/api/v1/admin/|/api/v1/ops/|ingest-runs/beat")
_UNTRIAGED = "untriaged"


def _watch_consts():
    """Read WORKFLOWS / NOT_WATCHED / MAX_UNTRIAGED without importing the module
    (it runs GitHub CLI calls at import time)."""
    with open(WATCH_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in (
                "WORKFLOWS", "NOT_WATCHED", "MAX_UNTRIAGED"):
            out[node.targets[0].id] = ast.literal_eval(node.value)
    return out["WORKFLOWS"], out["NOT_WATCHED"], out["MAX_UNTRIAGED"]


def _scheduled_workflows():
    found = {}
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(WF_DIR, fn)
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        # PyYAML parses a bare `on:` key as the boolean True
        triggers = doc.get(True) or doc.get("on") or {}
        if isinstance(triggers, dict) and "schedule" in triggers:
            found[fn] = raw
    return found


def test_every_scheduled_producer_is_watched_or_explicitly_listed():
    watched, not_watched, _ = _watch_consts()
    missing = [fn for fn, raw in _scheduled_workflows().items()
               if _PRODUCTION.search(raw)
               and fn not in watched and fn not in not_watched]
    assert not missing, (
        "these scheduled workflows do real work against production and are "
        "invisible to the deadman board — add them to WORKFLOWS, or to "
        "NOT_WATCHED with a reason. Absence is not a decision:\n"
        + "".join(f"  {fn}\n" for fn in missing))


def test_the_untriaged_backlog_only_shrinks():
    _, not_watched, cap = _watch_consts()
    untriaged = sorted(fn for fn, why in not_watched.items()
                       if _UNTRIAGED in (why or "").lower())
    assert len(untriaged) <= cap, (
        f"the untriaged deadman backlog grew from {cap} to {len(untriaged)}. "
        "A new production workflow was added without deciding whether it needs "
        "watching — which is exactly how 67 of them accumulated unnoticed.")
    # ★ REMOVED 2026-08-30: this line used to read
    #     assert len(untriaged) == cap or cap - len(untriaged) > 0
    # which is a tautology whenever the assertion above it passes — it can only
    # be false when len > cap, and that case is already caught. A vacuous
    # assertion written INTO the fence family whose whole subject is vacuous
    # assertions. Deleted rather than repaired: the ratchet above is the check.


def test_the_not_watched_ledger_has_not_rotted():
    """A ledger entry naming a workflow that no longer exists covers nothing,
    and inflates the backlog so the ratchet reads as progress that never
    happened. Same rot class drained from _EXCLUDE_FILES in batch 2."""
    _, not_watched, _ = _watch_consts()
    present = set(os.listdir(WF_DIR))
    dead = sorted(fn for fn in not_watched if fn not in present)
    assert not dead, (
        "NOT_WATCHED names workflows that no longer exist — drop them and lower "
        "MAX_UNTRIAGED by the same number, so the ratchet keeps meaning "
        "something:\n" + "".join(f"  {fn}\n" for fn in dead))


def test_every_watched_feed_clears_the_watchers_own_margin():
    """A cadence the watcher cannot keep green produces FALSE reds — six hours
    of them on 2026-07-30. The watcher warns; this fails the build instead."""
    watched, _, _ = _watch_consts()
    with open(WATCH_PY, encoding="utf-8") as fh:
        src = fh.read()
    interval = float(re.search(r"^WATCH_INTERVAL_H\s*=\s*([\d.]+)", src, re.M).group(1))
    margin = float(re.search(r"^WATCH_MARGIN\s*=\s*([\d.]+)", src, re.M).group(1))
    floor = interval * margin
    too_tight = {wf: cad for wf, cad in watched.items() if (2.0 * cad) < floor}
    assert not too_tight, (
        f"deadman-watch runs every {interval}h; these feeds go overdue sooner "
        f"than {floor}h and will false-RED on ordinary cron drift:\n"
        + "".join(f"  {wf}: cadence {cad}h -> overdue {2 * cad}h\n"
                  for wf, cad in sorted(too_tight.items())))


# ★ 2026-08-31 — WHY THIS FENCE EXISTS. Every test above asks whether a producer
# is WATCHED. None asked whether the cadence it is watched AT is the right one,
# and the cadences are hand-written next to a hand-written comment naming the
# interval. Two of the 82 had a comment that did not match the workflow's own
# cron, and the number had been written from the comment:
#
#   failover-canary.yml   declared 190h "# weekly Mon"  — actual cron '7 */6 * * *'
#   monthly-trend-cron.yml declared  36h "# every 24h"  — actual cron '5 0 1 * *'
#
# They fail in OPPOSITE directions, which is why this asserts both. A feed goes
# overdue at 2x its cadence (routes/ingest_runs.py), so:
#   too tight -> the feed ages past the threshold before it is next due to fire,
#     and the board publishes a RED for a job that is succeeding on schedule.
#     monthly-trend-cron did this for ~90% of every month.
#   too loose -> the alarm cannot arrive in any useful time. failover-canary,
#     the canary for the Railway->Render->KV path, had a 380h (16-day) window.
#
# ★ It reads the cron out of the workflow YAML, never the trailing comment —
# the comment is what was wrong both times.
_LOOSE_CYCLES = 8.0   # the board may take up to 8 missed fires to alarm, no more


def _cron_period_h(expr):
    """Longest legitimate gap, in hours, between two fires of one cron expression.
    Returns None if this parser does not understand the expression — callers must
    FAIL on None rather than skip it, or the fence quietly stops covering it."""
    f = expr.split()
    if len(f) != 5:
        return None
    minute, hour, dom, _month, dow = f
    if dom != "*" and not dom.startswith("*"):
        return 744.0                                   # a specific day of the month
    if dow != "*" and not dow.startswith("*"):
        days = [d for d in dow.split(",") if d]
        return 168.0 if len(days) == 1 else round(168.0 / len(days), 2)
    if hour == "*":
        return int(minute[2:]) / 60.0 if minute.startswith("*/") else 1.0
    if hour.startswith("*/"):
        return float(int(hour[2:]))
    hrs = sorted({int(h) for h in hour.split(",") if h.strip().isdigit()})
    if not hrs:
        return None
    if len(hrs) == 1:
        return 24.0
    gaps = [hrs[i + 1] - hrs[i] for i in range(len(hrs) - 1)] + [24 - hrs[-1] + hrs[0]]
    return float(max(gaps))


def _crons(raw):
    doc = yaml.safe_load(raw)
    triggers = doc.get(True) or doc.get("on") or {}
    sched = triggers.get("schedule") or []
    return [e["cron"] for e in sched if isinstance(e, dict) and e.get("cron")]


def test_every_watched_cadence_brackets_its_own_cron():
    watched, _, _ = _watch_consts()
    with open(WATCH_PY, encoding="utf-8") as fh:
        src = fh.read()
    interval = float(re.search(r"^WATCH_INTERVAL_H\s*=\s*([\d.]+)", src, re.M).group(1))
    margin = float(re.search(r"^WATCH_MARGIN\s*=\s*([\d.]+)", src, re.M).group(1))
    # A fast job cannot be watched tighter than the watcher itself can see; that
    # floor is _assert_watch_margin's, and it legitimately forces a loose ratio.
    ceiling_floor = 2.0 * interval * margin

    scheduled = _scheduled_workflows()
    unreadable, too_tight, too_loose = [], [], []
    for wf, cad in sorted(watched.items()):
        raw = scheduled.get(wf)
        if raw is None:
            unreadable.append(f"{wf}: watched, but no scheduled workflow file")
            continue
        exprs = _crons(raw)
        if not exprs:
            unreadable.append(f"{wf}: watched, but its YAML declares no cron")
            continue
        periods = [_cron_period_h(e) for e in exprs]
        if any(p is None for p in periods):
            unreadable.append(f"{wf}: cron not understood by this fence: {exprs}")
            continue
        period = min(periods)          # several crons -> it fires more often
        overdue_at = 2.0 * cad
        if overdue_at < period:
            too_tight.append(
                f"{wf}: cadence {cad}h -> overdue at {overdue_at}h, but it only "
                f"fires every {period}h — a false RED on a healthy job")
        elif overdue_at > max(ceiling_floor, period * _LOOSE_CYCLES):
            too_loose.append(
                f"{wf}: cadence {cad}h -> overdue at {overdue_at}h against a "
                f"{period}h cron — {overdue_at / period:.0f} missed fires before "
                f"the board says anything")

    assert not unreadable, (
        "this fence could not read a watched workflow's schedule, so it is not "
        "covering it — teach _cron_period_h the expression or drop the entry:\n"
        + "".join(f"  {m}\n" for m in unreadable))
    assert not too_tight, (
        "declared cadence is tighter than the workflow's own cron; the board "
        "will publish a RED for a job running exactly as scheduled:\n"
        + "".join(f"  {m}\n" for m in too_tight))
    assert not too_loose, (
        f"declared cadence is more than {_LOOSE_CYCLES:.0f}x the workflow's own "
        "cron; a death here cannot be reported in any useful time:\n"
        + "".join(f"  {m}\n" for m in too_loose))
