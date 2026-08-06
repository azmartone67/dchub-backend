#!/usr/bin/env python3
"""White-glove propagation must run once a day — and actually manage it.

MEASURED 2026-08-05: it ran 3 of the last 7 days (missing 07-30, 08-02, 08-03,
08-05). Cause: crawler_scheduler keeps its already-ran set in an IN-PROCESS dict
(`last_run_hours`) and only fires a slot while `now.minute < 5`, with 18 jobs
walked ahead of this one in the same sequential tick. A redeploy anywhere inside
20:00-20:04 loses the day outright — no catch-up, no record. main took 10 commits
between 19:00 and 21:00Z on 08-03 alone.

The fix is a catch-up slot at 23:00 plus a DURABLE once-per-day guard. The guard
is what makes a second slot safe; these tests exist so nobody removes one without
the other.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def sched(monkeypatch):
    import crawler_scheduler as cs
    calls = []

    fake = types.ModuleType("routes.white_glove_propagation")
    fake.run_white_glove_propagation = lambda dry_run: calls.append(dry_run) or {}
    monkeypatch.setitem(sys.modules, "routes.white_glove_propagation", fake)
    monkeypatch.delenv("WHITE_GLOVE_PROPAGATE_DISABLE", raising=False)
    return cs, fake, calls


class TestOncePerDayGuard:
    def test_it_runs_when_it_has_not_run_today(self, sched):
        cs, fake, calls = sched
        fake.ran_today = lambda: False
        cs._run_white_glove_propagate()
        assert len(calls) == 1

    def test_it_skips_when_it_already_ran_today(self, sched):
        """★ This is what makes the 23:00 catch-up slot safe. Without it the
        second slot would propagate twice every healthy day.

        MUTATION: change `if _already is not False` to `if _already` -> this
        still passes but the None case below fails, which is the point.
        """
        cs, fake, calls = sched
        fake.ran_today = lambda: True
        cs._run_white_glove_propagate()
        assert calls == []

    def test_an_unreadable_db_does_NOT_license_a_second_run(self, sched):
        """★★ None means "could not tell", and it must be treated as "do not
        run" — not as "has not run".

        Reading an outage as "no run today" would let the catch-up slot fire a
        second propagation on a day the morning slot already succeeded. Same
        unmeasured-as-zero mistake this codebase keeps paying for; here it costs
        16 outbound registry fetches and a duplicate run row.
        """
        cs, fake, calls = sched
        fake.ran_today = lambda: None
        cs._run_white_glove_propagate()
        assert calls == []

    def test_the_kill_switch_still_wins(self, sched, monkeypatch):
        cs, fake, calls = sched
        fake.ran_today = lambda: False
        monkeypatch.setenv("WHITE_GLOVE_PROPAGATE_DISABLE", "1")
        cs._run_white_glove_propagate()
        assert calls == []


class TestSlotWiring:
    def test_both_slots_exist_and_both_have_a_runner(self):
        """★ A SCHEDULE entry whose name is absent from _RUNNERS fires NOTHING —
        silently. The slot looks scheduled and never executes."""
        import re
        import crawler_scheduler as cs
        src = open(cs.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
        slots = set(re.findall(r'^\s*\(\d+,\s*\d+,\s*"([a-z_]+)"', src, re.M))
        runners = set(re.findall(r'^\s*"([a-z_]+)":\s*_run_', src, re.M))
        assert "white_glove_propagate" in slots
        assert "white_glove_propagate_catchup" in slots
        assert not (slots - runners), f"slots with no runner: {sorted(slots - runners)}"

    def test_the_catchup_is_hours_after_the_primary(self):
        """A catch-up in the same hour would be caught by the same redeploy."""
        import re
        import crawler_scheduler as cs
        src = open(cs.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
        hours = {n: int(h) for h, _h2, n in
                 re.findall(r'^\s*\((\d+),\s*(\d+),\s*"(white_glove[a-z_]*)"', src, re.M)}
        assert hours["white_glove_propagate_catchup"] - hours["white_glove_propagate"] >= 2
