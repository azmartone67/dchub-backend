"""Guard audit batch 3, Screen D — a shell that FAILED must beat a status the
board reads as RED.

WHY THIS EXISTS (measured live 2026-08-30 against /api/v1/ops/deadman):

    tracked=150  any_overdue=False  overdue_count=0

...while ELEVEN of the fifteen shell feeds carried their own failure in the
`note` field of that same response:

    surface-truth-shell-daily   status=success  note="served_text=FAIL
                                                     served_manifests=FAIL
                                                     repo_vs_served=FAIL ..."
    loop-control-shell-daily    status=success  note="cron_liveness=FAIL ... "
    growth-funnel-shell-daily   status=success  note="3 failing / 1 unknown of 4"

Twenty-nine failing lanes, and a board that said every one of 150 loops was
fine. The ledger has exactly two alarm paths besides cadence and the shells
defeat BOTH by construction:

  · `status` is the literal "success", which is in routes/ingest_runs._OK_STATUS,
    so the `st not in _OK_STATUS` check at ingest_runs.py can never fire.
  · `rows_inserted` is the literal 1 — commented in growth_funnel as
    "liveness sentinel — health lives in `note`" — which pins consecutive_zero
    at 0 so the >=3-zero-row rule can never fire either.

Only cadence survives, and cadence detects a shell that STOPPED, never a shell
that RAN AND FAILED. `note` is free text; nothing evaluates it.

THE NEAR MISS: agentic_loop_master_shell is the one shell that derives its
status rather than hardcoding it — `ok = not bool(out.get("tick_failed"))`.
That is still liveness, not health: `tick_failed` is only True when the tick
RAISED. Its own note reads "PASS 2 FAIL 2" beside status=success. Deriving the
status is not enough; it has to be derived from the VERDICT.

So this file asserts on the verdict, not on the shape of the expression.
"""

from __future__ import annotations

import ast
import sys
import glob
import json
import os

import pytest

from routes.ingest_runs import _OK_STATUS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _beating_shells(root: str = None) -> list[str]:
    """Every master shell that writes to the dead-man ledger."""
    root = root or _ROOT
    out = []
    for path in sorted(glob.glob(os.path.join(root, "routes", "*_master_shell.py"))):
        with open(path, encoding="utf-8") as fh:
            if "ingest-runs/beat" in fh.read():
                out.append(path)
    return out


# ── the behavioural guard: drive a real failing tick, read the wire ──────

def test_a_failing_tick_beats_a_status_the_board_reads_as_red(monkeypatch):
    """Kills: `"status": "success"` written regardless of the lane verdicts.

    This is the whole finding in one assertion. Every lane is forced to FAIL,
    so the tick's own verdict is "FAIL"; the beat that follows must not land
    in _OK_STATUS. On the code as of 649c841fc it lands on "success".
    """
    from routes import growth_funnel_master_shell as gs
    from routes.brain_ascension_master_shell import _check

    captured: dict = {}

    def _fake_post(url, data=None, timeout=None, headers=None, **kw):
        captured["url"] = url
        captured["body"] = json.loads(data.decode())

        class _R:
            status_code = 200
            text = "{}"
        return _R()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    monkeypatch.setattr(gs, "_LANES", (
        ("1 · forced red", lambda: [_check("x", "forced", False, "forced by the guard")]),
    ))

    out = gs._run_tick(beat=True)

    # Fixture integrity first — a guard that silently stopped producing a
    # failing tick would pass forever while proving nothing.
    assert out["verdict"] == "FAIL", f"fixture did not produce a failing tick: {out!r}"
    assert out["lanes_failing"] == 1, out
    assert captured, "the failing tick never beat the ledger at all"

    status = str(captured["body"].get("status", "")).lower()
    assert status not in _OK_STATUS, (
        f"growth-funnel beat status={status!r} after a tick whose own verdict was "
        f"FAIL. {status!r} is in _OK_STATUS, so /api/v1/ops/deadman reads this "
        f"shell as healthy. note={captured['body'].get('note')!r}"
    )


def test_a_passing_tick_still_beats_green(monkeypatch):
    """The other direction: honesty must not become a stuck alarm.

    A fix that beat red unconditionally would pass the test above and be just
    as useless. This pins the green case.
    """
    from routes import growth_funnel_master_shell as gs
    from routes.brain_ascension_master_shell import _check

    captured: dict = {}

    def _fake_post(url, data=None, timeout=None, headers=None, **kw):
        captured["body"] = json.loads(data.decode())

        class _R:
            status_code = 200
            text = "{}"
        return _R()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    monkeypatch.setattr(gs, "_LANES", (
        ("1 · forced green", lambda: [_check("x", "forced", True, "forced by the guard")]),
    ))

    out = gs._run_tick(beat=True)
    assert out["verdict"] == "PASS", out
    assert captured, "a healthy tick must still beat — silence reads as death"
    assert str(captured["body"].get("status", "")).lower() in _OK_STATUS, captured["body"]


# ── the structural sweep: no shell may hardcode a green status ───────────

def test_no_shell_hardcodes_a_green_beat_status():
    """AST, not text — a comment or a docstring quoting the pattern must not
    move this either way (six text-level checks in this repo have been wrong
    where the structural one was right).
    """
    offenders = []
    for path in _beating_shells():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "feed" not in keys or "status" not in keys:
                continue          # not a beat payload
            for k, v in zip(node.keys, node.values):
                if not (isinstance(k, ast.Constant) and k.value == "status"):
                    continue
                if isinstance(v, ast.Constant) and str(v.value).lower() in _OK_STATUS:
                    offenders.append(
                        f"{os.path.basename(path)}:{v.lineno} status={v.value!r} "
                        f"is a literal — this feed cannot report red")
    assert not offenders, (
        f"{len(offenders)} shell(s) beat a hardcoded green status:\n  "
        + "\n  ".join(offenders))


def test_no_shell_sends_rows_inserted_at_all():
    """The ledger's SECOND alarm path, and it has no correct literal value.

    A shell inserts no rows, so every possible answer here is a lie in one
    direction or the other:

      · `1` — what all fifteen used to send, commented "liveness sentinel" —
        pins consecutive_zero at 0, so the >=3-zero-row rule can never fire.
      · `0` — the obvious "fix" — CLIMBS that counter on every healthy run and
        reds the whole shell fleet on day three. This is not hypothetical: the
        comment in ingest_runs._OK_STATUS records eia-pricing, osm-crawl and
        competitor-gap burning red for days on exactly that rule.

    record_beat() gives a third option: `rows_sig = rows if rows is not None
    else -1`, and the upsert leaves consecutive_zero untouched when it is
    negative. Omitting the field is the only honest answer, and it is already
    the house pattern (tests/test_worker_deadman_beats.py: "never guess rows —
    the counter is the producer's").
    """
    offenders = []
    for path in _beating_shells():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "feed" not in keys or "status" not in keys:
                continue
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "rows_inserted":
                    offenders.append(
                        f"{os.path.basename(path)}:{k.lineno} sends rows_inserted "
                        f"— omit it instead")
    assert not offenders, (
        f"{len(offenders)} shell(s) send a rows_inserted a shell cannot have:\n  "
        + "\n  ".join(offenders))


# ── every SCHEDULED shell must have a feed at all ────────────────────────

def _scheduled_shell_labels(root: str = None) -> list[str]:
    """Shell labels a scheduler actually drives.

    TWO sources, and both must be read. The _DISPATCH literal is the original;
    a module declaring its own CRON_JOBS is the decentralised one. Reading only
    the literal is how a decentralised shell would drop out of the coverage
    check below SILENTLY — the assert would keep passing while nothing watched
    it, which is worse than the gap this test exists to catch.
    """
    root = root or _ROOT
    with open(os.path.join(root, "routes", "cron_heartbeat.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    dispatch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DISPATCH" for t in node.targets):
            dispatch = node.value
    assert dispatch is not None, "_DISPATCH not found in routes/cron_heartbeat.py"

    labels = []
    for el in dispatch.elts:
        if isinstance(el, ast.Tuple) and el.elts:
            label = el.elts[0]
            if isinstance(label, ast.Constant) and "shell" in str(label.value):
                labels.append(str(label.value))

    sys.path.insert(0, _ROOT)
    from routes.cron_declarations import declared_labels
    labels += [l for l in declared_labels(root) if "shell" in l]
    return labels


def _unwatched_shells(root: str = None) -> list[str]:
    """Scheduled shells with no dead-man feed — the gap this guard reports."""
    root = root or _ROOT
    beating_src = " ".join(os.path.basename(p) for p in _beating_shells(root))
    unwatched = []
    for label in _scheduled_shell_labels(root):
        stem = label.replace("_shell_daily", "").replace("_daily", "")
        # intel_expansion -> intelligence_expansion is the one alias in the table
        stem = {"intel_expansion": "intelligence_expansion"}.get(stem, stem)
        if f"{stem}_master_shell.py" not in beating_src:
            unwatched.append(label)
    return unwatched


def test_every_dispatched_shell_beats_the_ledger():
    """webmcp_shell_daily is on the cron dispatcher and beats nothing, so it is
    absent from /api/v1/ops/deadman entirely — if it stops, nothing notices.
    A shell that is scheduled is a loop, and every loop needs a feed.
    """
    unwatched = _unwatched_shells()
    assert not unwatched, (
        f"{len(unwatched)} shell(s) run on the cron dispatcher but beat no "
        f"dead-man feed, so their death is invisible: {', '.join(unwatched)}")
