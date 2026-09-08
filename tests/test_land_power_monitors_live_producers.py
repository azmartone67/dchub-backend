"""/api/land-power/status must watch the producers that actually run.

THE DEFECT (measured 2026-09-07): the board published

    status: "degraded"
    unhealthy_sources: ["hifld-substations", "eia-ng-pipelines"]

and BOTH were retired producers:

  · hifld-substations  — verdict `never_succeeded`. The crawler refuses before
    it fetches (SUBSTATION_WRITES_BLOCKED) and every run it ever logged was
    fetched=0 errors=1. The table is maintained by hifld_substation_loader.py,
    and holds 127,288 rows updated as recently as that same day.
  · eia-ng-pipelines   — verdict `stale`, 161.6 days. It is a 365-row slice
    frozen at 2026-03-30. gas_pipelines is really fed by the geodot ingest:
    32,851 of its 33,771 rows, written that day.

So the board cried about two jobs that no longer run while BOTH live producers
ran unmonitored. That is worse than no monitor: if the geodot ingest had died,
the board would have shown the same `degraded` it was already showing, and
nobody would have looked. A monitor aimed at a retired producer looks like
coverage.

★ THE TWO HALVES OF THE FIX, AND WHY BOTH ARE NEEDED.
  1. _EXPECTED now names live, cron-driven producers only, and the geodot
     ingest logs itself so it can be judged.
  2. The layers whose producer is a BULK load are not crons and cannot be
     judged by a sync cadence, so they get a freshness block instead — and it
     asks the right question. max(updated_at) on `substations` read TODAY
     because 24 rows were touched this month, while the last real refresh moved
     74,927 rows in August. One touched row makes a frozen layer look fresh.
     `last_bulk_refresh` is the newest day on which >= 1% of rows changed:
     for substations that is 2026-08-14, exposing 24 days the old reading hid.

★ Stdlib only. Reads source text — no repo scan, no DB, no network.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def crawler():
    return _read("land_power_crawler.py")


@pytest.fixture(scope="module")
def gas():
    return _read("routes/gas_pipeline_ingest.py")


def _expected(src):
    m = re.search(r"_EXPECTED = \((.*?)\)", src, re.S)
    assert m, "_EXPECTED not found"
    return set(re.findall(r"'([^']+)'", m.group(1)))


# ── 1 · retired producers are not monitored as if they were live ─────────────

@pytest.mark.parametrize("retired", ["hifld-substations", "eia-ng-pipelines"])
def test_retired_producers_are_not_in_expected(crawler, retired):
    """★ Each of these produced a permanent false `degraded`."""
    assert retired not in _expected(crawler), (
        f"{retired} no longer runs — monitoring it publishes a standing false "
        f"degraded and hides whether the live producer is alive")


def test_expected_names_the_live_producers(crawler):
    assert _expected(crawler) == {
        "eia-860-plants", "hifld-transmission", "eia-geodot-pipelines"}


def test_the_live_gas_producer_reports_under_the_monitored_name(gas, crawler):
    """★ THE PAIRING. Renaming the expected source without teaching the live
    producer to log would swap a false `degraded` for a false `never_run` —
    and _EXPECTED would be judging nothing at all."""
    m = re.search(r'_SYNC_SOURCE = "([^"]+)"', gas)
    assert m, "the gas ingest declares no sync source"
    assert m.group(1) in _expected(crawler), (
        f"gas logs as {m.group(1)!r} but the board expects {_expected(crawler)}")


def test_the_gas_ingest_logs_success_and_both_failure_paths(gas):
    """A producer that logs only its wins reads healthy while it dies.

    ★ Bound to the AST, not to the source text. The first version regexed
    `_log_sync\\([^)]*0,\\s*None` and could never match, because `[^)]*` stops
    at the first `)` — which is inside `len(rows)`. It failed loudly here; a
    slightly different regex would have passed vacuously instead.
    """
    import ast
    tree = ast.parse(gas)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ingest_gas_pipelines")
    calls = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name) and c.func.id == "_log_sync"]
    assert len(calls) == 3, f"expected success + 2 failure paths, found {len(calls)}"

    # third positional arg is `errors`
    errs = []
    for c in calls:
        assert len(c.args) == 5, ast.unparse(c)
        a = c.args[2]
        errs.append(a.value if isinstance(a, ast.Constant) else None)
    assert errs.count(0) == 1, f"exactly one success log expected, got {errs}"
    assert errs.count(1) == 2, f"both failure paths must log errors=1, got {errs}"


def test_logging_never_breaks_the_ingest(gas):
    """Fail-soft: the monitor is not allowed to take down the producer."""
    fn = gas[gas.index("def _log_sync"):gas.index("def ingest_gas_pipelines")]
    assert "except Exception" in fn and "log.warning" in fn


# ── 2 · bulk layers are judged by a MATERIAL refresh, not max(ts) ────────────

def test_bulk_freshness_is_not_max_timestamp(crawler):
    """★ THE MASK. substations' max(updated_at) reads today off 24 touched
    rows while the last real refresh was 2026-08-14."""
    block = crawler[crawler.index("layers = {}"):]
    assert "bulk_floor_rows" in block and "last_bulk_refresh" in block
    assert re.search(r"WHERE c >= %s", block), (
        "the query must count rows changed per day, not take a max")


def test_the_bulk_floor_scales_with_the_table(crawler):
    block = crawler[crawler.index("layers = {}"):]
    assert re.search(r"floor = max\(100, int\(total \* 0\.01\)\)", block), (
        "a fixed floor either never fires on a big table or always fires on a "
        "small one")


def test_every_layer_covered_by_no_sync_source_has_a_freshness_row(crawler):
    """substations lost its sync entry, so it MUST appear here — otherwise the
    fix silently stops watching it, which is the failure this replaces."""
    block = crawler[crawler.index("layers = {}"):]
    for table in ("substations", "power_plants", "gas_pipelines"):
        assert f"'{table}'" in block, table


def test_the_timestamp_column_is_resolved_not_assumed(crawler):
    """★ These tables disagree: power_plants has `last_updated`, the others
    `updated_at`. Hardcoding made power_plants publish {"error": ...}, which
    reads as watched and is not.

    ★ BOUND TO THE TUPLE'S VALUE, NOT TO THE FILE'S TEXT. The first version
    asserted `"last_updated" in block` — which passed even after the candidate
    list was cut back to ('updated_at',), because the words survive in the
    comment right above it. Mutation caught it. Parse the literal instead.
    """
    block = crawler[crawler.index("layers = {}"):]
    assert "information_schema.columns" in block, "the column must be looked up"
    assert '"ts_column": col' in block, "report which column answered"

    m = re.search(r"_TS_CANDIDATES = \((.*?)\)", block, re.S)
    assert m, "_TS_CANDIDATES not found"
    candidates = re.findall(r"'([^']+)'", m.group(1))
    assert "updated_at" in candidates, candidates
    assert "last_updated" in candidates, (
        f"power_plants uses last_updated; without it that layer reports an "
        f"error and reads as watched. candidates={candidates}")


def test_a_layer_with_no_timestamp_column_says_so(crawler):
    block = crawler[crawler.index("layers = {}"):]
    assert "no timestamp column found among" in block


def test_layers_are_published(crawler):
    assert '"layers": layers,' in crawler
