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
    assert len(untriaged) == cap or cap - len(untriaged) > 0, "ratchet is consistent"


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
