"""Every LinkedIn slot must fall inside a multiplatform-amplifier sweep window.

THE DEFECT (2026-06-07 → 2026-09-10, never once worked): the amplifier was
written for a 30-minute cron and queried `posted_at > NOW() - INTERVAL '60
minutes'`. crawler_scheduler then collapsed it to TWO slots a day — 15 and 03
UTC — to fit the harness's two-slot cap, and nobody widened the window. So the
sweep looked at [14:00,15:00) and [02:00,03:00) while LinkedIn published at 08,
12, 16 and 20. Zero overlap. The sweep found nothing by CONSTRUCTION.

It failed as SILENCE: the sweep returned swept=0 and logged a clean success, the
`linkedin_publish` cadence lane stayed green because LinkedIn itself was fine,
and only the downstream lanes went dark — so the alarm pointed at Bluesky, which
was never the problem. On 2026-09-09 the callout reported non-LinkedIn and
Bluesky publishing 110.3h stale and LinkedIn healthy in the same email.

This test does the arithmetic against the REAL schedules rather than restating
the numbers, so moving either one has to move the other.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _linkedin_slot_hours() -> set[int]:
    from routes.linkedin_quad_daily import SLOTS
    return {int(s["hour"]) for s in SLOTS if s.get("hour") is not None}


def _amplifier_sweep_hours() -> set[int]:
    """The two cron hours, read out of crawler_scheduler.SCHEDULE by AST.

    Importing crawler_scheduler executes module-level scheduler wiring, so the
    entry is parsed from source instead: SCHEDULE holds tuples shaped
    (hour_a, hour_b, name, method).
    """
    tree = ast.parse((ROOT / "crawler_scheduler.py").read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "SCHEDULE"
                        for t in node.targets)):
            continue
        for el in getattr(node.value, "elts", []):
            vals = getattr(el, "elts", [])
            if len(vals) < 3:
                continue
            name = vals[2]
            if isinstance(name, ast.Constant) and name.value == "multiplatform_amplifier":
                return {v.value for v in vals[:2]
                        if isinstance(v, ast.Constant) and isinstance(v.value, int)}
    return set()


def _lookback_minutes() -> int:
    from routes.multiplatform_amplifier import _lookback_minutes
    return _lookback_minutes()


def _covered(hour: int, sweep_hours: set[int], lookback_min: int) -> bool:
    """Is `hour` inside (sweep - lookback, sweep] for some sweep hour, mod 24?"""
    for s in sweep_hours:
        # distance backwards from the sweep to the post, wrapping the day
        back_h = (s - hour) % 24
        if 0 <= back_h * 60 < lookback_min:
            return True
    return False


def test_the_schedules_are_readable():
    """Floor: if either side resolves to nothing, every coverage assertion
    below is vacuously true over an empty set."""
    assert _linkedin_slot_hours(), "no LinkedIn slot hours resolved"
    assert _amplifier_sweep_hours(), "no amplifier sweep hours resolved"
    assert _lookback_minutes() > 0


@pytest.mark.parametrize("hour", sorted(_linkedin_slot_hours()))
def test_every_linkedin_slot_is_swept(hour):
    sweeps = _amplifier_sweep_hours()
    look = _lookback_minutes()
    assert _covered(hour, sweeps, look), (
        f"a LinkedIn post at {hour:02d}:00 UTC is never swept: the amplifier "
        f"runs at {sorted(sweeps)} with a {look}-minute lookback, so it sees "
        f"only {[f'{(s - look // 60) % 24:02d}:00-{s:02d}:00' for s in sorted(sweeps)]}. "
        "Bluesky, Twitter and Mastodon go dark while LinkedIn stays healthy.")


def test_the_60_minute_window_would_fail_this():
    """The bug, pinned as a control. If this ever passes, the coverage test
    above has stopped being able to detect the original defect."""
    assert not all(_covered(h, _amplifier_sweep_hours(), 60)
                   for h in _linkedin_slot_hours()), (
        "a 60-minute lookback now covers every slot — either the schedules "
        "moved or _covered() is not measuring what it claims")


def test_the_query_uses_the_configured_window_not_a_literal():
    """Bind to the SQL: a parameterised window that still hardcodes 60 minutes
    somewhere else is the same outage."""
    src = (ROOT / "routes" / "multiplatform_amplifier.py").read_text()
    fn = src[src.index("def auto_sweep_recent"):]
    fn = fn[:fn.index("\ndef ") if "\ndef " in fn[1:] else len(fn)]
    assert "INTERVAL '60 minutes'" not in fn, (
        "auto_sweep_recent still carries the hardcoded 60-minute window")
    assert re.search(r"INTERVAL '1 minute'", fn), (
        "the sweep no longer scales a parameterised interval")
