"""The two defects that made /api/v1/site-report take 14-18s, and the cap that lied.

★ 2026-08-25. The Site Analysis (`generate_site_analysis`, the Land & Power
"Generate Site Analysis" button) rendered in 14.2s cold, which is what pushed it
past the Cloudflare edge budget and produced the founding-member 503 (FE#1248
bought headroom; it did not make anything fast). The handoff blamed a cold
process-level cache. Profiling said otherwise — the cost was one query and one
timeout that was never a timeout.

1. THE QUERY.  site_planner.find_nearest_transmission matched a substation name
   against line endpoints with a LEADING wildcard:

       WHERE LOWER(sub_1) LIKE LOWER('%OSM-917634654%') OR LOWER(sub_2) LIKE ...

   A leading wildcard can never become an index qual, so the planner walked
   idx_transmission_voltage over the entire table. Measured EXPLAIN on prod:

       Index Scan Backward using idx_transmission_voltage
         Rows Removed by Filter: 2821162
         Buffers: shared hit=67423 read=141133
         Execution Time: 2292.723 ms

   and, cold, "canceling statement due to statement timeout". The MISS pays the
   whole scan — and a miss is the common case, because most `substations.name`
   values are import placeholders (OSM-917634654, RISER167166) that match no
   endpoint. Anchoring the pattern lets Postgres rewrite it into a real index
   range (BitmapOr over idx_transmission_sub1/sub_2): 2.29s -> 26ms.

2. THE CAP THAT NEVER CAPPED.  _call_with_timeout promised "a hard wall-clock
   cap" so a slow probe could not blow the report's budget. It was written as

       with _cf.ThreadPoolExecutor(max_workers=1) as ex:
           return ex.submit(fn, ...).result(timeout=timeout)

   ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which JOINS the
   worker. So `.result(timeout=6)` raised at 6s and then __exit__ blocked until
   the work finished anyway. Measured on the unpatched helper: a 6s cap on a 10s
   call returned None after 10.01s. Every timeout in that file was decorative,
   including the 7-way section pool's _grab(timeout=18) — which is why a report
   could run 18s while no number in the source said anything larger than 6.

Why these guards are source- and behaviour-level rather than a live probe: DB
tests skip in CI, and both defects presented as a 200 with a plausible body —
the slow path returns the same report, just late. Same lesson as
[[test_substations_columns]]: a 200 is not an answer.

THE CONTRACT
────────────
  T1. _call_with_timeout returns at its deadline, not when the work finishes.
  T2. Anti-vacuity control for T1: the victim function really is slow, and the
      unpatched `with`-form really does block (so T1 can fail).
  T3. _call_with_timeout still returns the value on the happy path.
  S1. No SQL literal reading discovered_transmission_lines uses a LEADING
      wildcard in a LIKE against sub_1/sub_2.
  S2. Anti-vacuity control for S1: the scan actually finds that query.
  S3. Positive control: the anchored prefix form is the one in use.
  P1. _build_survey_data's section pool is not a `with` block — otherwise its
      per-section _grab(timeout=18) cannot bound the request either.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
  patched   : 7 passed, exit 0
  unpatched : 4 failed, 3 passed, exit 1 — T1 (returned after ~1.0s for a 0.25s
              cap), S1 (LOWER(sub_N) LIKE), S3 (anchored form absent), P1 (the
              `with` pool). T2/S2 are controls and pass in BOTH trees by design;
              T3 is the happy path and must survive the fix. Run against
              origin/main 1117752c, not a hand-edited copy.
"""

import concurrent.futures as _cf
import pathlib
import re
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SITE_PLANNER = REPO / "site_planner.py"
SITE_REPORT = REPO / "routes" / "site_report.py"


def _load(fn):
    """Import _call_with_timeout without dragging in the Flask blueprint."""
    import importlib
    mod = importlib.import_module("routes.site_report")
    return getattr(mod, fn)


# ── T1/T2/T3 — the cap must actually cap ─────────────────────────────────────

def test_t1_call_with_timeout_returns_at_its_deadline():
    """T1: a 0.25s cap on a 1.0s call must return in well under 1.0s."""
    call = _load("_call_with_timeout")

    def slow():
        time.sleep(1.0)
        return "finished anyway"

    t0 = time.time()
    result = call(slow, 0.25)
    elapsed = time.time() - t0

    assert result is None, f"expected None on timeout, got {result!r}"
    assert elapsed < 0.7, (
        f"_call_with_timeout(timeout=0.25) took {elapsed:.2f}s on a 1.0s call — "
        "the cap is not enforced. ThreadPoolExecutor.__exit__ does "
        "shutdown(wait=True), which joins the worker; build the executor by "
        "hand and shutdown(wait=False) instead."
    )


def test_t2_control_the_victim_is_slow_and_the_with_form_blocks():
    """T2: anti-vacuity. Proves T1 is capable of failing.

    Without this, T1 would pass trivially if `slow()` were not actually slow.
    """
    def slow():
        time.sleep(1.0)
        return "finished anyway"

    # (a) the victim really does take ~1s
    t0 = time.time()
    slow()
    assert time.time() - t0 >= 0.9, "control invalid: slow() is not slow"

    # (b) the ORIGINAL `with`-form really does block past its cap
    t0 = time.time()
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(slow).result(timeout=0.25)
    except Exception:
        pass
    blocked = time.time() - t0
    assert blocked >= 0.9, (
        f"control invalid: the `with` form returned in {blocked:.2f}s, so T1 "
        "would pass even unpatched"
    )


def test_t3_call_with_timeout_still_returns_the_value():
    """T3: the fix must not break the happy path."""
    call = _load("_call_with_timeout")
    assert call(lambda a, b=0: a + b, 5, 2, b=3) == 5


# ── S1/S2/S3 — the transmission query must be index-usable ───────────────────

# A LIKE parameter built as '%...%' — the un-indexable shape.
_LEADING_WILDCARD = re.compile(r"""['"]%\{?[A-Za-z_]""")


def _tx_query_blocks(src):
    """Every SQL literal in src that reads discovered_transmission_lines."""
    return [m for m in re.findall(r'"""(.*?)"""', src, re.S)
            if "discovered_transmission_lines" in m]


def test_s1_no_leading_wildcard_like_against_transmission_endpoints():
    """S1: sub_1/sub_2 LIKE patterns must be anchored, not '%term%'."""
    src = SITE_PLANNER.read_text()
    offenders = []
    for block in _tx_query_blocks(src):
        if "LIKE" not in block.upper():
            continue
        # find the argument tuple passed alongside this query
        if re.search(r"LOWER\(sub_[12]\)\s+LIKE", block, re.I):
            offenders.append("LOWER(sub_N) LIKE — column-side LOWER() defeats "
                             "idx_transmission_sub1/sub_2")
    for m in _LEADING_WILDCARD.finditer(src):
        line = src[:m.start()].count("\n") + 1
        ctx = src[max(0, m.start() - 400):m.start()]
        if "discovered_transmission_lines" in ctx:
            offenders.append(f"site_planner.py:{line} leading-wildcard LIKE param")
    assert not offenders, (
        "un-indexable LIKE against discovered_transmission_lines (2.8M rows): "
        + "; ".join(offenders)
        + ". Anchor the pattern (term.upper() + '%') so Postgres can rewrite it "
          "into an index range. Do NOT add COLLATE \"C\" — that defeats the index."
    )


def test_s2_control_the_scan_finds_the_transmission_query():
    """S2: anti-vacuity. If this fails, S1 proved nothing."""
    blocks = _tx_query_blocks(SITE_PLANNER.read_text())
    assert blocks, "scan found no SQL literal reading discovered_transmission_lines"
    assert any("LIKE" in b.upper() for b in blocks), (
        "scan found the table but no LIKE query — S1 would pass vacuously"
    )


def test_s3_positive_control_the_anchored_prefix_is_in_use():
    """S3: the replacement is present, not merely the offender absent."""
    src = SITE_PLANNER.read_text()
    assert re.search(r"sub_1 LIKE %s OR sub_2 LIKE %s", src), (
        "expected the anchored `sub_1 LIKE %s OR sub_2 LIKE %s` form"
    )
    assert re.search(r"search_term\.upper\(\)\s*\+\s*'%'", src), (
        "expected the prefix pattern to be built as search_term.upper() + '%'"
    )


# ── P1 — the section pool must not join on exit either ───────────────────────

def test_p1_section_pool_does_not_join_on_exit():
    """P1: a `with` around the 7-way pool makes _grab(timeout=18) decorative."""
    src = SITE_REPORT.read_text()
    assert "with _cf.ThreadPoolExecutor(max_workers=7)" not in src, (
        "_build_survey_data's section pool is a `with` block, so "
        "ThreadPoolExecutor.__exit__ joins every section and the per-section "
        "_grab(timeout=18) cannot bound the request"
    )
    assert "ex.shutdown(wait=False" in src, (
        "expected the section pool to be shut down without waiting"
    )
