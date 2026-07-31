"""Guard: the substations refresh must not destroy what upstream cannot speak to.

WHAT THIS PINS
──────────────
`substations` holds a HIFLD slice that is a ONE-SHOT SNAPSHOT — all 79,686 rows
carry created_at = 2026-03-17 and have never been re-pulled. It could not be:
every refresh path was dead, measured 2026-07-30, not assumed.

    land_power_crawler.crawl_substations   opendata download API -> HTTP 500
    hifld_substation_loader.py  BOTH endpoints:
        opendata.arcgis.com/.../f48d61b8...                      -> HTTP 500
        services1.arcgis.com/Hp6G80Pky0om7QvQ/Electric_Substations_1
                                     -> {"error":{"code":400,"message":"Invalid URL"}}

routes/substation_ingest.py adds the working one:
    services5.arcgis.com/HDRa0B57OVrv2E1q/.../Electric_Substations/FeatureServer/0
    75,328 features, national extent, HIFLD schema, maxRecordCount 2000.

★★ THE DANGEROUS PART IS THE WRITE, NOT THE FETCH. The sibling
routes/transmission_ingest.py does a FULL REPLACE, and copying that here would
destroy data — measured two ways:

  1. This upstream has NO OWNER/OPERATOR FIELD, and 28,319 held rows carry
     `operator` populated from elsewhere. A replace, or merely listing `operator`
     in the UPDATE SET, nulls all of them. Upstream cannot speak to that column,
     so upstream must not overwrite it.
  2. Upstream lists 75,328 against 79,686 held. Delete-then-insert would drop the
     published total 126,842 -> 122,484, and "126k substations" is a published
     headline figure. Rows a snapshot omits are REPORTED, never deleted — an
     upserting feed is a snapshot, not a floor.

This file exists because both failure modes are invisible in a green run: the
ingest would report success while silently nulling 28,319 values.

THE CONTRACT
────────────
  U1. The upstream is a FeatureServer /query endpoint, not the retired
      opendata.arcgis.com download API that returns 500.
  U2. The write is an UPSERT whose ON CONFLICT target is the constraint that
      actually re-identifies a substation — (name, lat, lng), 79,686 distinct
      over 79,686 HIFLD rows. NOT hifld_objectid: the March load stored the
      ArcGIS OBJECTID (export row number, 1..79,687) there instead of the real
      HIFLD ID (107,655..311,000), so it re-identifies nothing. The ingest
      corrects it in the UPDATE SET. No DELETE / TRUNCATE anywhere.
  U3. `operator` is never in the UPDATE SET. Nor are the other columns this
      upstream cannot speak to (owner, capacity_mva, source_id).
  U4. Column arity: the INSERT's column list matches _ROW_FIELDS + source +
      updated_at, and every named column exists on the live table.
  U5. HIFLD's -999999 'not available' sentinel becomes NULL, never a negative kV
      and never a measured 0.
  U6. Epoch-millisecond dates are converted, not passed through.
  U7. An empty fetch is refused, never reported as a successful no-op.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
This module is NEW, so there is no unpatched counterpart to measure the
contract tests against — `git archive origin/main` has no routes/substation_ingest.py
at all, and every test would fail on the import/read rather than on the property
it pins, which proves nothing. Measured instead:

  PATCHED (this branch):    0 failed, 10 passed, 1 xfailed
  UNPATCHED (origin/main):  collection error — the module does not exist

So the falsification here is MUTATION-BASED, not diff-based: test_u3 and test_u5
mutate the shipped source (inject `operator` into the UPDATE SET; neuter the
sentinel guard) and assert the checks then FAIL. A guard that cannot be made to
fail is not a guard, and for a new file that has to be demonstrated directly
rather than borrowed from the previous commit.

MUST-FAIL CONTROL
─────────────────
test_zzz_must_fail_control is xfail(strict=True). A collection abort exits 0 with
0 tests and renders as an ordinary red job (it shipped twice on 2026-07-28) — the
control's presence in the summary is the only proof this file ran.

Run:  python3 -m pytest tests/test_substation_ingest_upsert_safety.py -v
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "substation_ingest.py")

# Live substations column set (information_schema, 2026-07-30).
LIVE_COLUMNS = {
    "id", "name", "operator", "substation_type", "voltage_kv", "capacity_mva",
    "lat", "lng", "city", "state", "country", "connected_transmission",
    "status", "source", "source_id", "created_at", "updated_at",
    "hifld_objectid", "zip", "county", "naics_code", "naics_desc",
    "source_date", "val_method", "val_date", "type", "lines", "min_volt",
    "max_volt", "owner", "county_fips", "max_voltage_kv", "min_voltage_kv",
    "sub_type", "lines_count", "available_mva",
}
LIVE_UNIQUE_COLUMNS = {"id", "hifld_objectid", "source_id"}

# Columns this upstream carries no field for. Measured: `operator` is populated
# on 28,319 held rows and would be nulled if listed in the UPDATE SET.
UPSTREAM_CANNOT_SPEAK_TO = {"operator", "owner", "capacity_mva", "source_id"}

OPERATOR_ROWS_AT_RISK = 28319


def _src():
    with open(MOD) as f:
        return f.read()


def _tree(src=None):
    t = ast.parse(src if src is not None else _src())
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t


def _const(tree, name):
    """Value of a module-level string constant, joined if implicitly concatenated."""
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name:
            return ast.literal_eval(n.value)
    return None


def _upsert_sql(src=None):
    sql = _const(_tree(src), "_UPSERT_SQL")
    assert sql, "_UPSERT_SQL not found"
    return " ".join(sql.split())


def _insert_columns(sql):
    m = re.search(r"(?i)INSERT INTO substations\s*\((.*?)\)\s*VALUES", sql)
    assert m, f"could not parse the INSERT column list: {sql[:120]}"
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _update_set_columns(sql):
    m = re.search(r"(?i)DO UPDATE SET\s+(.*?)$", sql)
    assert m, "no DO UPDATE SET clause"
    return [c.strip().lower()
            for c in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", m.group(1))]


def _fn(tree, name):
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found"
    assert fn.body, f"{name} parsed with an EMPTY body"
    return fn


def _exec_fn(name, src=None):
    """Execute one self-contained helper out of the module."""
    tree = _tree(src)
    fn = _fn(tree, name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), MOD, "exec"), ns)
    return ns[name]


# ── sanity ────────────────────────────────────────────────────────────────────
def test_module_parses_and_declares_what_the_tests_read():
    tree = _tree()
    assert len(tree.body) > 5, "module parsed to almost nothing"
    for const in ("_SVC", "_ROW_FIELDS", "_UPSERT_SQL", "_SRC"):
        assert _const(tree, const) is not None, f"{const} missing"
    for fn in ("_volt", "_num", "_epoch_ms_to_date", "_row_from_attrs", "_fetch"):
        _fn(tree, fn)


# ── U1 ────────────────────────────────────────────────────────────────────────
def test_upstream_is_a_featureserver_query_not_the_retired_download_api():
    svc = _const(_tree(), "_SVC")
    assert "opendata.arcgis.com" not in svc, (
        "points at the retired opendata.arcgis.com download API, which returns "
        "HTTP 500 'Item does not exist or is inaccessible' for every HIFLD "
        "dataset this repo referenced")
    assert "/FeatureServer/" in svc and svc.rstrip("/").endswith("/query"), (
        f"not a FeatureServer /query endpoint: {svc}")
    assert svc.startswith("https://"), "insecure scheme"


# ── U2 ────────────────────────────────────────────────────────────────────────
def test_it_upserts_and_never_deletes():
    src = _src()
    sql = _upsert_sql(src)
    assert "ON CONFLICT" in sql.upper(), "not an upsert"
    m = re.search(r"(?i)ON CONFLICT\s*\(([^)]*)\)", sql)
    assert m, "no ON CONFLICT target"
    target = tuple(x.strip().lower() for x in m.group(1).split(","))
    assert target == ("name", "lat", "lng"), (
        f"ON CONFLICT {target} — must be (name, lat, lng), the only constraint "
        f"that re-identifies a substation here. hifld_objectid holds the ArcGIS "
        f"OBJECTID (export row number 1..79,687), not the HIFLD ID "
        f"(107,655..311,000), so keying on it matches nothing and the insert "
        f"then collides on (name, lat, lng) anyway — measured on a live run.")
    updated = set(_update_set_columns(sql))
    assert "hifld_objectid" in updated, (
        "the positional key is not corrected — every matched row should get its "
        "real HIFLD ID written as the ingest passes over it")
    for keycol in ("name", "lat", "lng"):
        assert keycol not in updated, f"{keycol} is the conflict key; not in SET"
    # No destructive statement anywhere in the module.
    for stmt in ("DELETE FROM substations", "TRUNCATE"):
        assert stmt.lower() not in src.lower(), (
            f"module contains `{stmt}` — upstream lists 75,328 against 79,686 "
            f"held; pruning against one snapshot deletes real assets")


# ── U3 — the one that would silently destroy data ─────────────────────────────
def test_operator_is_never_overwritten_by_an_upstream_that_lacks_it():
    sql = _upsert_sql()
    updated = set(_update_set_columns(sql))
    clash = updated & UPSTREAM_CANNOT_SPEAK_TO
    assert not clash, (
        f"UPDATE SET writes {sorted(clash)}, which this upstream carries no "
        f"field for. `operator` alone is populated on {OPERATOR_ROWS_AT_RISK:,} "
        f"held rows — listing it here nulls every one, and the ingest would "
        f"still report success")
    # …and the same columns must not be smuggled in via the INSERT list either.
    inserted = {c.lower() for c in _insert_columns(sql)}
    assert not (inserted & UPSTREAM_CANNOT_SPEAK_TO), (
        f"INSERT names {sorted(inserted & UPSTREAM_CANNOT_SPEAK_TO)}")


def test_u3_is_falsifiable():
    """Mutate the shipped SQL to add `operator` and prove the check fails.

    This module is new, so there is no previous commit to measure the guard
    against. Demonstrate falsifiability directly instead — a guard that cannot
    be made to fail is not a guard.
    """
    sql = _upsert_sql()
    mutated = sql.replace("DO UPDATE SET", "DO UPDATE SET operator = EXCLUDED.operator,")
    assert "operator" in set(_update_set_columns(mutated)), \
        "the mutation did not take — the parser cannot see UPDATE SET columns"
    assert set(_update_set_columns(mutated)) & UPSTREAM_CANNOT_SPEAK_TO, \
        "U3's assertion would NOT fire on a real regression"


# ── U4 ────────────────────────────────────────────────────────────────────────
def test_column_arity_and_every_named_column_exists_live():
    tree = _tree()
    sql = _upsert_sql()
    cols = _insert_columns(sql)
    fields = list(_const(tree, "_ROW_FIELDS"))
    # row tuple + source + updated_at(NOW())
    assert len(cols) == len(fields) + 2, (
        f"INSERT names {len(cols)} columns but _ROW_FIELDS has {len(fields)} "
        f"(+source +updated_at = {len(fields) + 2}) — a silent column/value "
        f"misalignment writes the right values into the wrong columns")
    missing = [c for c in cols if c not in LIVE_COLUMNS]
    assert not missing, f"columns absent from the live table: {missing}"
    for f in fields:
        assert f in LIVE_COLUMNS, f"_ROW_FIELDS names a non-existent column: {f}"


# ── U5 ────────────────────────────────────────────────────────────────────────
def test_the_minus_999999_sentinel_becomes_null():
    volt = _exec_fn("_volt")
    assert volt(-999999) is None, (
        "HIFLD's -999999 'not available' sentinel survived — publishing it as a "
        "voltage means negative kV; publishing 0 means a MEASURED zero. Both are "
        "wrong; unknown is None")
    assert volt(0) is None, "0 kV is not a measurement"
    assert volt(69) == 69.0 and volt("345") == 345.0
    assert volt(None) is None and volt("n/a") is None


def test_u5_is_falsifiable():
    """Neuter the sentinel guard in the shipped source; the check must fail."""
    src = _src().replace("return f if f > 0 else None",
                         "return f", 1)
    volt = _exec_fn("_volt", src)
    assert volt(-999999) == -999999.0, "the mutation did not take"
    # which is precisely what U5 forbids
    assert volt(-999999) is not None, "U5 would NOT fire on a real regression"


# ── U6 ────────────────────────────────────────────────────────────────────────
def test_epoch_millisecond_dates_are_converted():
    conv = _exec_fn("_epoch_ms_to_date")
    # measured on a live row: SOURCEDATE 1473984000000 -> 2016-09-16
    got = conv(1473984000000)
    assert got is not None and got.year == 2016, (
        f"epoch-ms not converted (got {got!r}) — passing 1473984000000 to a DATE "
        f"column lands in the year 48000 or raises")
    assert conv(None) is None and conv(0) is None and conv("x") is None


# ── U7 ────────────────────────────────────────────────────────────────────────
def test_an_empty_fetch_is_refused_not_reported_as_success():
    src = _src()
    assert re.search(r"if not rows:", src), "no empty-result branch"
    idx = src.index("if not rows:")
    window = src[idx:idx + 400]
    assert "400" in window and "ok=False" in window, (
        "an empty fetch does not return an error — a broken feed would upsert "
        "zero rows and report a successful ingest")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
