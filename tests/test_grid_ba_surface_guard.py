"""Grid telemetry rows carry the UPSTREAM observation time, and the balancing-
authority / region surfaces stay coherent.

PROVENANCE. This file is the rescued ws41 guard (untracked in a pruned
worktree, see ~/Downloads/dchub-worktree-rescue-2026-09-01/README.md — the
"register in test.yml" note there is stale: pre-merge runs `pytest tests/`, so
landing the file IS registering it). Landed 2026-09-02 with the D4 fix it
described. PRUNED HONESTLY, not adapted: the ws41 retirement/bulk work it was
written for NEVER LANDED on main — `_RETIRED`, `LIVE_BAS`, `is_live`,
`_eia_period_age_hours`, `feed_liveness`, `grid_intelligence_bulk`,
`_grid_digest_*`, `_grid_bulk_cold_budget`, `no_self_calls`, `returned_rows`,
`INTEL_ONLY_ISOS` and the `intelligence-bulk` warmer target do not exist in
routes/eia_utility_bas.py, main.py or routes/grid_cache_warmer.py — so the 13
tests that asserted them were dropped rather than rewritten to pass against
code that does not do what they claimed. Kept: the tests whose subject exists
on main (sections 3-5). Added: section 1, the guard the rescued header
promised but never contained.

 1. ★ THE FABRICATED-FRESH WRITE IS STOPPED AT THE SOURCE.
    routes/_iso_common.persist_metrics INSERTed (iso, metric_name,
    metric_value, unit) and OMITTED `timestamp`, so the column took DEFAULT
    NOW() — the insert clock — and its `ON CONFLICT (iso, timestamp,
    metric_name) DO NOTHING` guard could NEVER fire: the conflict key differed
    on every write by construction. data-pulse runs every 15 minutes, so a
    frozen reading was re-published as a brand-new "now" row 96 times a day.
    Verified live 2026-07-29 on AEC: latest_data_at 2026-07-29T07:09 with
    14,776 rows for a feed whose newest genuine EIA observation is
    2021-09-01T05. Re-verified 2026-09-02 (D4): the file was untouched since
    #2075. persist_metrics now takes `observed_at`; the callers pass the
    EIA-930 period / the ENTSO-E period end; None is allowed but LOGGED.
    Behavioural, against a fake cursor: the SQL must bind the coerced stamp as
    the row timestamp. Mutation-verified: dropping `timestamp` from the INSERT
    column list turns test_persist_metrics_binds_observed_at_as_the_row_timestamp
    red.

 2. The remaining module-local copies of that INSERT are INVENTORIED
    (section 2) and can only shrink — a new `INSERT INTO grid_data` without a
    timestamp fails, and a module fixed later must leave the list.

NO NETWORK, NO DB. Nothing runs at module scope.
"""
import ast
import glob
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
BAS = os.path.join(ROOT, "routes", "eia_utility_bas.py")
WARMER = os.path.join(ROOT, "routes", "grid_cache_warmer.py")
ROUTES = os.path.join(ROOT, "routes")

# EIA-930 REGION AGGREGATES — never balancing authorities.
REGION_AGGREGATES = ("CAL", "CAR", "CENT", "FLA", "MIDA", "MIDW", "NE", "NY",
                     "NW", "SE", "SW", "TEN", "TEX", "US48")

# The balancing authorities the MCP scoreboard is configured to surface
# (dchub-mcp-server server.mjs _US_GRID_ZONES, tiers 0-1). Every one must
# resolve in the backend map or it 400s — and the scoreboard converts a
# per-zone failure into a non-ranked row, so it would VANISH silently.
SCOREBOARD_BA_CODES = ("BPA", "TVA", "SOCO", "FPL", "DUK", "SRP", "CPLE",
                       "PACE", "PSCO", "FPC", "LGEE", "APS")

# ★ RATCHET (D4 audit, 2026-09-02). Module-local copies of the grid_data
# INSERT that still omit `timestamp` (insert clock, dedup guard dead). Each
# fix must REMOVE its module from this list; a NEW copy without a timestamp
# fails below. The shared routes/_iso_common.persist_metrics and
# routes/iso_eu_entsoe._persist_metrics are fixed and deliberately absent.
_KNOWN_INSERT_CLOCK_WRITERS = frozenset({
    "iso_aeso_intl.py", "iso_au_aemo.py", "iso_br_ons.py", "iso_caiso.py",
    "iso_ercot.py", "iso_hydroquebec.py", "iso_ieso.py", "iso_jp_denkiyoho.py",
    "iso_kr_kpx.py", "iso_nordpool_intl.py", "iso_nyiso.py", "iso_sg_nems.py",
    "iso_tw_taipower.py", "iso_uk_elexon.py",
})

UTC = timezone.utc


def _parse(path):
    """Parse a shipped file and PROVE the parse produced something — an empty
    parse passes every downstream assertion by never being exercised."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert src.strip(), f"{path} is empty"
    tree = ast.parse(src, filename=path)
    assert tree.body, f"{path} parsed to an EMPTY module body"
    return tree, src


def _func(tree, name, path):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name}() not found in {path}")


def _module_assign(tree, name, path):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
    raise AssertionError(f"module-scope assignment {name} not found in {path}")


def _src_of(node, src):
    seg = ast.get_source_segment(src, node)
    assert seg, "could not recover source for node — the AST/source mapping broke"
    return seg


# ── 1. persist_metrics writes the OBSERVATION time ──────────────────────
class _FakeCursor:
    def __init__(self, calls):
        self.calls = calls
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), tuple(params or ())))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_shared(monkeypatch):
    from routes import _iso_common as ic
    calls = []
    cur = _FakeCursor(calls)

    @contextmanager
    def _conn():
        yield _FakeConn(cur)

    monkeypatch.setattr(ic, "conn", _conn)
    return ic, calls


def _insert_columns(sql):
    m = re.search(r"INSERT INTO grid_data\s*\(([^)]*)\)", sql)
    assert m, f"no grid_data INSERT in {sql!r}"
    return [c.strip() for c in m.group(1).split(",")]


def test_persist_metrics_binds_observed_at_as_the_row_timestamp(monkeypatch):
    ic, calls = _capture_shared(monkeypatch)
    n = ic.persist_metrics("PJM", {"demand_mw": {"value": 1.0, "unit": "MW"}},
                           observed_at="2026-07-29T02")
    assert n == 1
    sql, params = calls[0]
    assert "timestamp" in _insert_columns(sql), (
        "the INSERT omits `timestamp` — the column takes DEFAULT NOW() and the "
        "ON CONFLICT (iso, timestamp, metric_name) guard can never fire")
    assert params[-1] == datetime(2026, 7, 29, 2, tzinfo=UTC), (
        "the bound timestamp must be the coerced upstream observation, "
        f"got {params[-1]!r}")
    # The dedup key must be the SAME literal, in the SAME statement.
    assert "ON CONFLICT (iso, timestamp, metric_name) DO NOTHING" in sql


def test_the_same_reading_binds_the_same_key_twice(monkeypatch):
    """The whole point: a repeated reading must collide, not multiply."""
    ic, calls = _capture_shared(monkeypatch)
    m = {"demand_mw": {"value": 5.0, "unit": "MW"}}
    ic.persist_metrics("SPP", m, observed_at="2026-07-29T02")
    ic.persist_metrics("SPP", m, observed_at="2026-07-29T02")
    assert calls[0][1] == calls[1][1], "two writes of one reading bound different keys"


def test_none_observed_at_is_an_explicit_logged_fallback(monkeypatch, caplog):
    ic, calls = _capture_shared(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dchub.iso_common"):
        ic.persist_metrics("AESO", {"x": {"value": 1}}, observed_at=None)
    sql, params = calls[0]
    assert params[-1] is None
    assert "COALESCE(%s, NOW())" in sql, "the NOW() fallback must be explicit in the SQL"
    assert any("observed_at=None" in r.getMessage() for r in caplog.records), (
        "an insert-clock row must be a logged choice, not the silent default")


def test_undeclared_observed_at_is_logged_not_silent(monkeypatch, caplog):
    ic, calls = _capture_shared(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dchub.iso_common"):
        ic.persist_metrics("X", {"x": {"value": 1}})
    assert calls[0][1][-1] is None
    assert any("not declared" in r.getMessage() for r in caplog.records)


def test_unparseable_observed_at_is_logged_and_never_replaced_by_the_clock(
        monkeypatch, caplog):
    ic, calls = _capture_shared(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dchub.iso_common"):
        ic.persist_metrics("X", {"x": {"value": 1}}, observed_at="not-a-time")
    assert calls[0][1][-1] is None, "an unreadable stamp must bind None (explicit NOW), not a guess"
    assert any("unparseable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw, want", [
    ("2026-07-29T02", datetime(2026, 7, 29, 2, tzinfo=UTC)),          # EIA-930 UTC hour
    ("2026-07-29T02-04", datetime(2026, 7, 29, 6, tzinfo=UTC)),       # EIA local-hourly
    ("2026-08-07T23:00Z", datetime(2026, 8, 7, 23, tzinfo=UTC)),      # ENTSO-E
    ("2026-08-07T23:00:00+00:00", datetime(2026, 8, 7, 23, tzinfo=UTC)),
    ("2026-08-07T23:15:30+02:00", datetime(2026, 8, 7, 21, 15, 30, tzinfo=UTC)),
    (datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 12, tzinfo=UTC)),  # naive = UTC
    (None, None), ("", None), ("junk", None), ("2026-13-40T02", None),
])
def test_coerce_observed_at_shapes(raw, want):
    from routes._iso_common import coerce_observed_at
    assert coerce_observed_at(raw) == want


def test_parse_eia_v2_latest_period_reads_the_newest_period():
    from routes._iso_common import parse_eia_v2_latest_period as p
    body = ('{"response":{"data":[{"period":"2026-07-29T01","value":"1"},'
            '{"period":"2026-07-29T02","value":"2"},{"period":"2026-07-28T23"}]}}')
    assert p(body) == "2026-07-29T02"
    assert p('{"response":{"data":[]}}') is None
    assert p("not json") is None


def test_every_shared_persist_metrics_caller_declares_observed_at():
    """Undeclared is logged, but a caller that never says which it is has not
    read the contract. Structural: the CALL must carry the keyword."""
    offenders, seen = [], 0
    for path in sorted(glob.glob(os.path.join(ROUTES, "*.py"))):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)   # __init__.py is legitimately empty
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "persist_metrics":
                continue
            seen += 1
            if not any(k.arg == "observed_at" for k in node.keywords):
                offenders.append(f"{os.path.basename(path)}:{node.lineno}")
    assert seen >= 7, f"expected the EIA-fed callers; found {seen}"
    assert not offenders, f"persist_metrics called without observed_at=: {offenders}"


def test_grid_data_insert_copies_are_inventoried_and_only_shrink():
    """Section 2: every `INSERT INTO grid_data` in routes/ either binds a
    timestamp or is on the ratchet list — and the list has not rotted."""
    clock_writers, stamped = set(), set()
    for path in sorted(glob.glob(os.path.join(ROUTES, "*.py"))):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for m in re.finditer(r"INSERT INTO grid_data\s*\(([^)]*)\)", src):
            cols = [c.strip() for c in m.group(1).split(",")]
            (stamped if "timestamp" in cols else clock_writers).add(os.path.basename(path))
    assert "_iso_common.py" in stamped and "iso_eu_entsoe.py" in stamped
    new = sorted(clock_writers - _KNOWN_INSERT_CLOCK_WRITERS)
    assert not new, (
        f"NEW grid_data INSERT(s) without a timestamp: {new} — bind the upstream "
        "observation time (see _iso_common.persist_metrics), do not extend the list")
    fixed = sorted(_KNOWN_INSERT_CLOCK_WRITERS - clock_writers)
    assert not fixed, (
        f"{fixed} no longer write the insert clock — remove them from "
        "_KNOWN_INSERT_CLOCK_WRITERS so the ratchet keeps its teeth")


def test_entsoe_persist_binds_each_zones_period_end_and_the_newest_for_the_aggregate(
        monkeypatch):
    from routes import iso_eu_entsoe as eu
    calls = []
    cur = _FakeCursor(calls)

    @contextmanager
    def _conn():
        yield _FakeConn(cur)

    monkeypatch.setattr(eu, "_conn", _conn)
    snap = {
        "metrics": {"generation_total_mw": {"value": 100.0, "unit": "MW"}},
        "zones": {
            "DE_LU": {"data_period_end": "2026-09-02T03:00:00+00:00",
                      "generation_total_mw": 60.0, "renewable_pct": 50.0, "gas_pct": 10.0},
            "FR": {"data_period_end": "2026-09-02T02:00:00+00:00",
                   "generation_total_mw": 40.0, "renewable_pct": 20.0, "gas_pct": 5.0},
        },
    }
    assert eu._persist_metrics(snap) == 7
    by_iso = {}
    for sql, params in calls:
        assert "timestamp" in _insert_columns(sql)
        by_iso.setdefault(params[0], set()).add(params[-1])
    assert by_iso["EU_DE_LU"] == {datetime(2026, 9, 2, 3, tzinfo=UTC)}
    assert by_iso["EU_FR"] == {datetime(2026, 9, 2, 2, tzinfo=UTC)}
    assert by_iso["ENTSOE"] == {datetime(2026, 9, 2, 3, tzinfo=UTC)}, (
        "the aggregate row takes the NEWEST zone period end")


def test_health_for_iso_says_what_its_clock_is(monkeypatch):
    ic, calls = _capture_shared(monkeypatch)

    class _Cur(_FakeCursor):
        def fetchone(self):
            return (datetime(2026, 9, 2, 3, tzinfo=UTC), 5)

    cur = _Cur(calls)

    @contextmanager
    def _conn():
        yield _FakeConn(cur)

    monkeypatch.setattr(ic, "conn", _conn)
    out = ic.health_for_iso("PJM", "src")
    assert out["latest_data_at"] == "2026-09-02T03:00:00+00:00"
    assert "latest_data_at_basis" in out, (
        "health must say whether latest_data_at is an observation or the insert clock")


# ── 3. region aggregates stay unroutable ────────────────────────────────
def test_eia_region_aggregates_are_not_routable():
    tree, path = _parse(MAIN)[0], MAIN
    rto_map = ast.literal_eval(_module_assign(tree, "_EIA_RTO_MAP", path))
    assert isinstance(rto_map, dict) and len(rto_map) > 40, (
        f"_EIA_RTO_MAP looks wrong: {len(rto_map)} entries")
    for agg in REGION_AGGREGATES:
        assert agg not in rto_map, (
            f"{agg} is an EIA REGION AGGREGATE, not a balancing authority — it "
            f"is the SUM of its members (MIDA == PJM to the MW, TEN == TVA, "
            f"TEX == ERCO). Routing it would double-count every member "
            f"underneath it. Its absence here is the only thing making it 400.")


def test_region_aggregates_are_not_registrable_as_bas_either():
    tree, path = _parse(BAS)[0], BAS
    bas = ast.literal_eval(_module_assign(tree, "_BAS", path))
    assert isinstance(bas, list) and len(bas) >= 40, (
        f"_BAS should hold the full registry; found {len(bas)}")
    codes = {b["code"] for b in bas}
    for agg in REGION_AGGREGATES:
        assert agg not in codes, f"{agg} is an EIA region aggregate, not a BA"
    # The registry is 43, not 45. AZPS is APS's EIA respondent and IOU is a
    # `type` value — neither is a registered code.
    assert "AZPS" not in codes, "AZPS is APS's EIA RESPONDENT, not a registered code"
    assert "IOU" not in codes, "IOU is a `type` field value, not a registered code"


# ── 4. the label -> EIA code map, and that it is one map ────────────────
def test_every_scoreboard_ba_resolves_in_the_backend_map():
    tree, path = _parse(MAIN)[0], MAIN
    rto_map = ast.literal_eval(_module_assign(tree, "_EIA_RTO_MAP", path))
    for code in SCOREBOARD_BA_CODES:
        assert code in rto_map, (
            f"{code} is configured on the MCP scoreboard but does not resolve "
            f"here — it would 400, and the scoreboard turns a per-zone failure "
            f"into a non-ranked row, so it would VANISH silently")
    # The known divergences, verified live 2026-07-29 against the echoed rto_code.
    assert rto_map["APS"] == "AZPS", "APS is the one registry divergence"
    assert rto_map["BPA"] == "BPAT"
    assert rto_map["ERCOT"] == "ERCO"
    assert rto_map["CAISO"] == "CISO"
    assert rto_map["NYISO"] == "NYIS"
    assert rto_map["SPP"] == "SWPP"
    assert rto_map["ISO-NE"] == "ISNE"
    for code in ("SOCO", "FPL", "DUK", "SRP", "TVA"):
        assert rto_map[code] == code


def test_there_is_only_one_region_code_map():
    """Two maps that must agree is how a code silently starts 400ing on one
    path only. The single-region route must ALIAS the module-scope table."""
    tree, src = _parse(MAIN)
    route = _src_of(_func(tree, "phase19b_grid_intelligence", MAIN), src)
    assert "EIA_RTO_MAP = _EIA_RTO_MAP" in route, (
        "the single-region route should alias the module-scope map")
    assert "'CAISO': 'CISO'" not in route, (
        "the route still carries its own copy of the region map")


# ── 5. the grid-intel surface keeps BOTH freshness readings ─────────────
def test_grid_intelligence_publishes_absolute_age_not_only_the_relative_gap():
    tree, src = _parse(MAIN)
    body = _src_of(_func(tree, "_grid_intel_fetch", MAIN), src)
    assert "generation_mix_stale_hours" in body, "the relative gap must be KEPT"
    assert "generation_mix_age_hours" in body, (
        "no ABSOLUTE age is published. generation_mix_stale_hours is "
        "demand_period minus generation_mix_period — a gap between two upstream "
        "feeds that collapses toward 0 when a feed dies (AEC returned 0 on 2021 "
        "data while SOCO returned 26 on current data).")
    assert "generation_mix_note" in body, "a dead feed must be able to carry a warning"


def test_bpa_and_tva_are_not_in_the_hot_warm_list():
    """HOT_ISOS also warms the /grid/<x> HTML page, which BPA/TVA have none of;
    warming it minted guaranteed 5xx (the r78 note)."""
    tree, _ = _parse(WARMER)
    hot = ast.literal_eval(_module_assign(tree, "HOT_ISOS", WARMER))
    for code in ("BPA", "TVA"):
        assert code not in hot
