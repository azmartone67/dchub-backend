"""tests/test_utility_bas_says_which_failure.py — "partial" for 25 hours.

MEASURED 2026-09-03. /api/v1/freshness: the iso domain in BREACH, 71 of 104
streams stale, worst grid_data:MISO at 26.0h against a 4h target. Every
data-pulse tick for 25 hours printed exactly:

    rows=483 failed=1 failed_isos=UTILITY_BAS(partial) no_new_data=none

and the newest stored observation stood at 2026-09-02T06:00:00Z across 45
balancing authorities. Nothing anywhere could say whether EIA had stopped
publishing or we had stopped reading.

TWO DEFECTS, EACH SUFFICIENT ON ITS OWN.

1. "partial" COLLAPSED TWO OPPOSITE SITUATIONS. `status = "ok" if ok else
   "partial"`, where `ok` counts BAs that persisted a ROW. After D4
   (2026-09-02) an unchanged EIA period dedups to zero rows BY DESIGN — so
   "every fetch worked, upstream has not published" and "every fetch failed"
   produced the same word.

   ★ THE STATUS KEY IS LOAD-BEARING AND THE ORDER IS A TRAP. classify_result
     tests `if st:` -> failed BEFORE it reads `awaiting_upstream`, so any
     non-empty status that is not ok/no_new_data is a failure and the wait list
     is never consulted. To be classified as waiting, an extractor must leave
     `status` UNSET. The orchestrator gained that vocabulary on 2026-09-03;
     this extractor never spoke it.

   ★ AND classify_result derives its REASON from `error` (then errors[0]),
     never from `note`. A diagnosis parked in `note` reaches nobody — which is
     why the log said only "(partial)".

2. THE ROLL-UP COULD NOT SEE NORTH AMERICA. Ten of twenty-one extractors —
   ERCOT CAISO NYISO MISO PJM SPP ISONE TVA BPA UTILITY_BAS — belonged to no
   family, so `feed_families` held one key, 'entsoe', reporting live_feed_ok
   true while 71 streams were stale. MUST_HAVE_FAMILIES could never protect
   them either: you cannot name a family that does not exist.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_utility_bas_says_which_failure.py -v
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest import mock

import pytest

from routes import eia_utility_bas as m
from routes.iso_orchestrator import AWAITING_UPSTREAM, _FAMILIES, classify_result

NOW = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
_REAL_AGE = m._period_age_hours


def _ba(code, status="ok", rows=0, period="2026-09-03T06", err=None):
    d = {"code": code, "status": status, "rows_inserted": rows,
         "eia_period": period}
    if err:
        d["error"] = err
    return d


def _run(results):
    """Drive the REAL run_extraction with extract_one stubbed and time pinned."""
    bas = [{"code": r["code"], "eia": r["code"], "name": r["code"]}
           for r in results]
    with mock.patch.object(m, "_BAS", bas), \
         mock.patch.object(m, "extract_one",
                           side_effect=lambda ba: next(r for r in results
                                                       if r["code"] == ba["code"])), \
         mock.patch.object(m, "_period_age_hours",
                           lambda p, now=None: _REAL_AGE(p, NOW)):
        out = m.run_extraction()
    return out, classify_result(out)


# ── the four situations, and they must read differently ──────────────────

def test_rows_landed_is_ok():
    out, (verdict, _) = _run([_ba("APS", rows=3), _ba("SRP", rows=2)])
    assert out.get("status") == "ok" and verdict == "ok"


def test_a_short_upstream_pause_is_a_WAIT_not_a_failure():
    """EIA-930 is hourly and publishes with a lag; an hour behind is ordinary."""
    out, (verdict, reason) = _run([_ba("APS"), _ba("SRP")])
    assert verdict == AWAITING_UPSTREAM, out
    assert "fetched cleanly" in str(reason)


def test_the_wait_case_leaves_status_UNSET():
    """★ THE ORDERING TRAP. classify_result checks `if st:` -> failed before it
    looks at awaiting_upstream, so any status word at all buries the wait."""
    out, _ = _run([_ba("APS"), _ba("SRP")])
    assert "status" not in out, (
        "a set status short-circuits classify_result to 'failed' and the "
        "awaiting_upstream list is never read: %r" % out.get("status"))
    assert out.get("awaiting_upstream")


def test_a_LONG_upstream_stall_goes_back_to_being_a_failure():
    """A leash, not an amnesty. Without this the change would convert the very
    25h outage it was written for into a permanently reassuring 'waiting'."""
    out, (verdict, reason) = _run([_ba("APS", period="2026-09-02T06"),
                                   _ba("SRP", period="2026-09-02T06")])
    assert verdict == "failed"
    assert "2026-09-02T06" in str(reason) and "25.0h" in str(reason)


def test_failing_fetches_are_a_failure_and_name_the_bas():
    out, (verdict, reason) = _run([_ba("APS", status="error", err="HTTP 500"),
                                   _ba("SRP", status="error", err="timeout")])
    assert verdict == "failed"
    assert "FAILED" in str(reason) and "APS=HTTP 500" in str(reason)


def test_a_real_error_outranks_a_wait():
    """An extractor must not launder a broken fetch by also being late."""
    out, (verdict, reason) = _run([_ba("APS", status="error", err="HTTP 500"),
                                   _ba("SRP")])
    assert verdict == "failed" and "FAILED" in str(reason)
    assert "status" in out, "a failure must set a status"


# ── the reason has to survive the wire ───────────────────────────────────

def test_the_diagnosis_rides_on_the_key_the_classifier_reads():
    """classify_result reads `error`, never `note`. This is the whole reason
    the log said '(partial)' for 25 hours."""
    out, (_, reason) = _run([_ba("APS", period="2026-09-02T06")])
    assert out.get("error"), "the diagnosis must be on `error`"
    assert reason == out["error"]


@pytest.mark.parametrize("case,lead", [
    ([_ba("APS", period="2026-09-02T06")], "EIA published nothing newer"),
    ([_ba("APS", status="error", err="HTTP 500")], "1 of 1 BA fetches FAILED"),
])
def test_the_first_60_chars_say_which_kind_of_broken(case, lead):
    """data-pulse truncates the reason to 60 characters, so the WHICH has to
    lead. `(r.get("reason") or "")[:60]` — .github/workflows/data-pulse.yml."""
    _, (_, reason) = _run(case)
    assert str(reason)[:60].startswith(lead), str(reason)[:60]


# ── the age helper is honest about not knowing ───────────────────────────

@pytest.mark.parametrize("period", [None, "", "not-a-date", "2026-13-99T06",
                                    "T06", [], {}])
def test_an_unreadable_period_is_UNMEASURED_not_zero(period):
    """None, never 0 — a period we cannot read must not present as perfectly
    fresh, and must not silently become a 'wait' either.

    ("20260902" is NOT in this list: it is valid ISO 8601 basic format and
    datetime.fromisoformat parses it, which is correct behaviour, not a gap.)"""
    assert _REAL_AGE(period, NOW) is None


@pytest.mark.parametrize("period,hours", [
    ("2026-09-03T06", 1.0), ("2026-09-02T06", 25.0),
    ("2026-09-03T06:00:00+00:00", 1.0), ("2026-09-03T06:00:00Z", 1.0),
])
def test_period_ages_parse(period, hours):
    assert abs(_REAL_AGE(period, NOW) - hours) < 0.01


def test_an_unreadable_period_does_not_buy_a_free_wait():
    """If we cannot tell how stale upstream is, we must not assert it is fine."""
    out, (verdict, _) = _run([_ba("APS", period=None)])
    assert verdict in (AWAITING_UPSTREAM, "failed")
    assert "unknown" in str(out.get("awaiting_upstream") or out.get("error"))


# ── every feed belongs to a family ───────────────────────────────────────

def _extractor_codes():
    import inspect
    from routes import iso_orchestrator as o
    src = inspect.getsource(o)
    blk = src[src.index("    extractors = ["):]
    blk = blk[:blk.index("\n    ]")]
    return [c for _, c in re.findall(r'\("([a-z0-9_]+)",\s*"([A-Z0-9_\-]+)"\)', blk)]


def test_every_extractor_is_owned_by_SOMETHING():
    """★ THE REAL INVARIANT — not "everything has a family". The seven
    organized US markets deliberately have none, because iso-data-pull.yml
    owns their ledger row and a second writer is the masking trap. What must
    never happen is an extractor owned by NOBODY: that is how TVA, BPA and the
    45-BA UTILITY_BAS fan-out stayed invisible through a 25-hour outage.

    Ownership is read from iso_grid_adapters.ISO_REGISTRY, not restated here,
    so the two lanes cannot drift into either a gap or a double-write."""
    from iso_grid_adapters import ISO_REGISTRY
    covered = {mem for _, mems in _FAMILIES for mem in mems}
    orphans = [c for c in _extractor_codes()
               if c not in covered and c not in ISO_REGISTRY]
    assert not orphans, (
        "%s are pulled by data-pulse and owned by no family and no other "
        "lane — an outage in them is unreportable" % orphans)
    both = [c for c in _extractor_codes()
            if c in covered and c in ISO_REGISTRY]
    assert not both, (
        "%s have TWO owners — a family here and iso-data-pull's ledger row; "
        "that is the one-direction-masking trap" % both)


def test_no_family_names_an_extractor_that_does_not_exist():
    """A member code with no extractor makes its family permanently 'failed'
    ('no member extractor reported a result')."""
    codes = set(_extractor_codes())
    ghosts = sorted({m for _, mems in _FAMILIES for m in mems} - codes)
    assert not ghosts, ghosts


def test_the_utility_bas_family_groups_by_shared_upstream():
    """TVA, BPA and the 45-BA fan-out share ONE upstream (EIA), so they fail
    together and a reader gets one answer rather than 47."""
    fam = dict((f, set(mems)) for f, mems in _FAMILIES)
    assert fam.get("iso-us-eia") == {"TVA", "BPA", "UTILITY_BAS"}
    assert "ERCOT" not in fam["iso-us-eia"], (
        "ERCOT is owned by iso-data-pull; a family here would double-write "
        "its ledger row")


def test_family_names_follow_the_existing_convention():
    for feed, _ in _FAMILIES:
        assert re.fullmatch(r"iso-[a-z0-9-]+", feed), feed
