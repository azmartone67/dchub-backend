"""water_risk read-honesty fence — 2026-09-22.

THE BUGS THIS FENCES
--------------------
Two published surfaces read `water_risk` and both got it wrong, in the two
different ways a read can be wrong. Schema verified read-only against prod
Neon, 2026-09-21: 51 rows, all source='wri_aqueduct', columns exactly
id / state / water_stress_score / baseline_water_stress / bws_category /
source / computed_at.

1. DEAD COLUMNS, SWALLOWED — routes/market_brief.py Section 8 "Risk Factors"
   selected `drought_d2_months`, a column water_risk has never had. Every call
   raised UndefinedColumn. The `except` branch then tried
   `SELECT stress_score, baseline_water_stress ... WHERE LOWER(market) = ...`
   — neither `stress_score` nor `market` exists either — and a bare
   `except: pass` ate that too. Section 8 served `water_stress: null` on EVERY
   market, forever, with no error field, while the table held all 51 states.

2. WRONG SCALE + A NULL FALLBACK — routes/hyperscaler_brief.py Section 5 read
   `water_stress_score` correctly and then classified with
   `if float(stress) >= 4.0`. That threshold is for a 1-5 index; the column is
   0-100 (100 = most stressed). Every state but the very least-stressed
   cleared it, so `stressed_state_pct` published ~100%. It also did
   `_as_float(r[0]) or _as_float(r[1])`, falling back to
   `baseline_water_stress` — NULL on all 51 rows — and, because `or` tests
   truthiness rather than None, additionally discarded a genuine 0.0, which is
   the reading of the LEAST-stressed states.

WHAT IS CHECKED
---------------
* The 0-100 -> 1-5 band lands on WRI's own category midpoints.
* A failed read publishes null PLUS a named error — never a silent null, and
  never a fabricated number.
* A failed read does not cascade into the next query on the same connection
  (emulated with real Postgres abort semantics, poison=True).
* The dead columns and the dead WHERE shape stay gone from both modules.
* `baseline_water_stress` is never read back — it is NULL on every row.
* A 0.0 score counts as a measurement, not as missing.
* `stressed_state_pct` reflects the 0-100 scale, not the old 1-5 threshold.
* Anti-vacuous floors, per the #2062 lesson: a fence that goes green because
  the thing it inspects became empty is worse than no fence.
"""
import ast
import functools
import os
import re

import psycopg2.extras
import pytest

mb = pytest.importorskip("routes.market_brief")
hb = pytest.importorskip("routes.hyperscaler_brief")
wr = pytest.importorskip("util.water_risk")


# ---------------------------------------------------------------- fake driver

class _FakeConn:
    def __init__(self):
        self.rollbacks = 0
        self.aborted = False

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False


class _FakeCursor:
    """psycopg2 semantics INSIDE an explicit transaction.

    Once a statement errors the connection is poisoned: every later statement
    raises InFailedSqlTransaction until somebody rolls back. Emulating that is
    the only way to prove the cascade is actually fixed rather than merely
    absent from the one code path a happy-path test happens to walk.
    """

    class Error(Exception):
        pass

    def __init__(self, fail_substrings=(), rows=None, poison=True):
        self.connection = _FakeConn()
        self.fail_substrings = tuple(fail_substrings)
        self.rows = rows or {}
        self.poison = poison
        self.executed = []
        self._result = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append(flat)
        if self.connection.aborted:
            raise self.Error("current transaction is aborted, commands "
                             "ignored until end of transaction block")
        if any(s in flat for s in self.fail_substrings):
            if self.poison:
                self.connection.aborted = True
            raise self.Error('column "drought_d2_months" does not exist')
        for key, val in self.rows.items():
            if key in flat:
                self._result = val
                return
        self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# Live shapes. The water rows are what `read_states_stress` / `read_state_stress`
# select: (UPPER(state), water_stress_score, bws_category) and
# (water_stress_score, bws_category) respectively.
_AZ_SCORE = 71.8      # measured live 2026-09-21
_AL_SCORE = 25.0
_AK_SCORE = 12.5


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sql_literals(path):
    """Every string constant in a module, EXCLUDING docstrings.

    AST, not a text scan: this fence's own prose names every dead column in
    order to warn about it, and a grep would flag the warning as the bug.
    """
    with open(os.path.join(_repo_root(), path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def _water_sql(path):
    """Only the string constants that actually talk to water_risk."""
    return [s for s in _sql_literals(path) if "water_risk" in s]


# ------------------------------------------------------------- the band math

def test_band_lands_on_wri_category_midpoints():
    """cat/4*100 puts the categories on 0/25/50/75/100; cuts are the midpoints."""
    assert wr.water_band_1_5(0.0) == 1      # Low            (<10%)
    assert wr.water_band_1_5(25.0) == 2     # Low-Medium     (10-20%)
    assert wr.water_band_1_5(50.0) == 3     # Medium-High    (20-40%)
    assert wr.water_band_1_5(75.0) == 4     # High           (40-80%)
    assert wr.water_band_1_5(100.0) == 5    # Extremely High (>80%)
    # Cut points belong to the HIGHER band, matching WRI's own bucket edges.
    assert wr.water_band_1_5(12.5) == 2
    assert wr.water_band_1_5(37.5) == 3
    assert wr.water_band_1_5(62.5) == 4
    assert wr.water_band_1_5(87.5) == 5
    assert wr.water_band_1_5(None) is None


def test_band_matches_live_samples():
    """Measured against prod Neon 2026-09-21."""
    assert wr.water_band_1_5(_AZ_SCORE) == 4    # AZ 71.8 -> High
    assert wr.water_band_1_5(_AL_SCORE) == 2    # AL 25.0 -> Low-Medium
    assert wr.water_band_1_5(_AK_SCORE) == 2    # AK 12.5 -> Low-Medium


def test_a_zero_score_is_a_band_not_a_missing_value():
    """0.0 is the LEAST-stressed reading there is, not the absence of one."""
    assert wr.water_band_1_5(0.0) == 1
    assert wr.water_band_1_5(0.0) is not None


def test_the_old_1_to_5_threshold_no_longer_sweeps_the_table():
    """`>= 4.0` on a 0-100 column called all but the very lowest state stressed."""
    every_live_band = [wr.water_band_1_5(s) for s in (0.0, 12.5, 25.0, 50.0, 71.8)]
    # Under the old test (score >= 4.0) four of these five counted as stressed.
    assert sum(1 for s in (0.0, 12.5, 25.0, 50.0, 71.8) if s >= 4.0) == 4
    # Under the band test, only the 71.8 does.
    assert sum(1 for b in every_live_band if b >= wr.STRESSED_BAND) == 1


def test_band_agrees_with_the_site_simulator_copy():
    """routes/site_simulator.py grew its own `_water_band` in #5259, landed
    while this change was in flight. Two implementations of one conversion is
    how a surface silently drifts a band away from its neighbours, so they are
    pinned to each other across the whole range until one of them goes away.
    """
    ss = pytest.importorskip("routes.site_simulator")
    other = getattr(ss, "_water_band", None)
    if other is None:                       # consolidated onto util/water_risk
        pytest.skip("routes.site_simulator no longer carries its own _water_band")
    probes = [i / 2.0 for i in range(0, 201)] + [12.5, 37.5, 62.5, 87.5, None]
    for score in probes:
        assert other(score) == wr.water_band_1_5(score), (
            f"band drift at {score!r}: site_simulator={other(score)} "
            f"util.water_risk={wr.water_band_1_5(score)}")


# ------------------------------------------------- market_brief Section 8

def test_market_brief_risk_publishes_the_score_not_a_forever_null():
    cur = _FakeCursor(rows={"FROM water_risk": [(_AZ_SCORE, "High")]})
    out = mb._section_risk(cur, {"state": "AZ", "name": "Phoenix"})
    assert out["water_stress"] == _AZ_SCORE
    assert out["water_band"] == 4
    assert out["water_band_label"] == "High"
    assert out["water_category"] == "High"
    assert not out["errors"].get("water_stress")


def test_market_brief_risk_failure_is_named_never_a_silent_null():
    cur = _FakeCursor(fail_substrings=("FROM water_risk",), poison=True)
    out = mb._section_risk(cur, {"state": "AZ", "name": "Phoenix"})
    assert out["water_stress"] is None, "a failed read must not invent a number"
    err = out["errors"].get("water_stress")
    assert err, "a failed read must name itself, not publish a bare null"
    assert "does not exist" in err


def test_market_brief_risk_failure_does_not_cascade():
    """The whole point of unpoison(): the NEXT read on the connection survives."""
    cur = _FakeCursor(fail_substrings=("FROM water_risk",), poison=True)
    mb._section_risk(cur, {"state": "AZ", "name": "Phoenix"})
    assert cur.connection.rollbacks >= 1, "nothing rolled the aborted tx back"
    cur.execute("SELECT 1 FROM facilities")   # must not raise


def test_market_brief_absent_row_is_distinguishable_from_a_broken_read():
    """A state with no row and a read that blew up are different answers."""
    cur = _FakeCursor(rows={})          # query succeeds, returns nothing
    out = mb._section_risk(cur, {"state": "ZZ", "name": "Nowhere"})
    assert out["water_stress"] is None
    assert "no water_risk row" in out["errors"]["water_stress"]

    broken = _FakeCursor(fail_substrings=("FROM water_risk",))
    out2 = mb._section_risk(broken, {"state": "ZZ", "name": "Nowhere"})
    assert out2["errors"]["water_stress"] != out["errors"]["water_stress"]


def test_market_brief_drought_is_named_as_uncollected():
    """No drought source exists in this schema; say so rather than imply a read."""
    cur = _FakeCursor(rows={"FROM water_risk": [(_AZ_SCORE, "High")]})
    out = mb._section_risk(cur, {"state": "AZ", "name": "Phoenix"})
    assert out["drought_months_d2_plus"] is None
    assert out["errors"]["drought_months_d2_plus"] == "no_drought_source_ingested"


# --------------------------------------------- hyperscaler_brief Section 5

def _water_section(state_rows, water_rows, **kw):
    cur = _FakeCursor(rows={"FROM discovered_facilities": state_rows,
                            "FROM water_risk": water_rows}, **kw)
    return hb._section_water(cur, {"aliases": ["Amazon"]}), cur


def test_hyperscaler_stressed_pct_uses_the_0_to_100_scale():
    """AZ 71.8 is stressed; IL 25.0 is not. The old >= 4.0 called BOTH stressed."""
    out, _ = _water_section(
        [("AZ", 100.0, 3), ("IL", 400.0, 5)],
        [("AZ", _AZ_SCORE, "High"), ("IL", _AL_SCORE, "Low-Medium")])
    assert out["stressed_state_pct"] == 20.0
    assert out["stressed_state_pct"] != 100.0, "the 1-5 threshold is back"
    assert out["avg_water_band"] == wr.water_band_1_5(out["avg_water_stress"])


def test_hyperscaler_counts_a_zero_score_as_a_measurement():
    """`x or y` discarded 0.0 — the reading of the least-stressed states."""
    out, _ = _water_section(
        [("WA", 100.0, 2), ("AZ", 100.0, 2)],
        [("WA", 0.0, "Low"), ("AZ", 100.0, "Extremely High")])
    assert out["states_matched"] == 2, "a 0.0 score was dropped as missing"
    # Old behaviour dropped WA and averaged AZ alone -> 100.0.
    assert out["avg_water_stress"] == 50.0
    assert out["stressed_state_pct"] == 50.0


def test_hyperscaler_pct_denominator_is_matched_mw_not_fleet_mw():
    """A numerator that can only come from MATCHED states, over ALL fleet MW,
    silently scores every unmatched state as unstressed."""
    out, _ = _water_section(
        [("AZ", 100.0, 3), ("XX", 300.0, 9)],      # XX has no water_risk row
        [("AZ", _AZ_SCORE, "High")])
    assert out["states_matched"] == 1
    assert out["stressed_state_pct"] == 100.0, \
        "the one state we can see IS stressed; 25.0 would be fleet-MW denominator"


def test_hyperscaler_read_failure_publishes_a_note_not_a_number():
    out, _ = _water_section([("AZ", 100.0, 3)], [],
                            fail_substrings=("FROM water_risk",), poison=True)
    assert out["avg_water_stress"] is None, "a failed read must not invent a number"
    assert out["stressed_state_pct"] is None
    assert out["avg_water_band"] is None
    assert "does not exist" in (out["water_stress_error"] or ""), \
        "the failure must survive in its own field, not be clobbered by `note`"
    # The MW numbers came from a query that SUCCEEDED — they still stand.
    assert out["total_us_mw"] == 100.0


def test_hyperscaler_water_failure_does_not_cascade():
    _, cur = _water_section([("AZ", 100.0, 3)], [],
                            fail_substrings=("FROM water_risk",), poison=True)
    assert cur.connection.rollbacks >= 1
    cur.execute("SELECT 1 FROM facilities")   # must not raise


def test_hyperscaler_reads_every_state_in_one_query():
    """The per-state loop swallowed each failure with `except: continue`."""
    _, cur = _water_section(
        [("AZ", 100.0, 3), ("IL", 400.0, 5), ("WA", 50.0, 1)],
        [("AZ", _AZ_SCORE, "High")])
    assert sum(1 for s in cur.executed if "water_risk" in s) == 1


# --------------------------------------------------- the dead shapes stay dead

DEAD_COLUMNS = ("drought_d2_months", "stress_score", "baseline_water_stress")

# The single read path. All FIVE route surfaces now go through it; a hand-copy
# coming back is caught by test_water_risk_has_one_read_path below.
READ_PATH = "util/water_risk.py"

#: Every module that used to carry its own `FROM water_risk` query and now
#: must not. The first three came through #5263; routes/dcpi.py and
#: routes/land_power_mcp.py joined in #5285, which also deleted
#: util/water_stress.py — the second reader that owned
#: STATE_WATER_STRESS_SQL and a second copy of the band.
CONVERTED_SURFACES = ("routes/market_brief.py", "routes/hyperscaler_brief.py",
                      "routes/site_simulator.py", "routes/dcpi.py",
                      "routes/land_power_mcp.py")

#: (module, the accessor it must call). A module can stop carrying its own SQL
#: by having its read DELETED as easily as by having it consolidated, and the
#: SQL scan cannot tell those apart — so pin the call too.
READ_PATH_CALLERS = (
    ("routes/market_brief.py",     "read_state_stress"),
    ("routes/hyperscaler_brief.py", "read_states_stress"),
    ("routes/site_simulator.py",   "read_state_stress"),
    ("routes/dcpi.py",             "read_state_stress"),
    ("routes/dcpi.py",             "read_states_stress"),
    ("routes/land_power_mcp.py",   "read_state_stress"),
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude", "venv", ".venv"}


@functools.lru_cache(maxsize=1)
def _modules_touching_water_risk():
    """Every .py in the repo whose SQL mentions water_risk — discovered, not
    listed. An allowlist that is only ever SUBTRACTED from goes stale green in
    the fix direction; a scan cannot."""
    hits = []
    root = _repo_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            try:
                sqls = [s for s in _water_sql(rel)
                        if "from water_risk" in " ".join(s.split()).lower()]
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            if sqls:
                hits.append((rel, tuple(sqls)))
    return tuple(hits)


def test_dead_columns_stay_out_of_every_water_query():
    scanned = _modules_touching_water_risk()
    assert scanned, "nothing in the repo reads water_risk any more"
    for path, sqls in scanned:
        for sql in sqls:
            # `water_stress_score` legitimately CONTAINS "stress_score".
            probe = " ".join(sql.split()).replace("water_stress_score", "")
            for dead in DEAD_COLUMNS:
                assert dead not in probe, f"{path}: dead column {dead!r} is back"


def test_no_query_filters_water_risk_on_a_market_column():
    """water_risk is keyed by 2-letter state. There is no `market` column."""
    for path, sqls in _modules_touching_water_risk():
        for sql in sqls:
            flat = " ".join(sql.split()).lower()
            assert "lower(market)" not in flat, f"{path}: the dead market filter is back"


def test_water_risk_has_one_read_path():
    """The two surfaces that got this wrong must not carry their own SQL again.

    Seven hand-copies of the deals guard is how that class got out of hand
    (util/deals.py, #2079). One read path, or the next schema fact has to be
    re-learned in every copy.
    """
    owners = {p for p, _ in _modules_touching_water_risk()}
    assert READ_PATH in owners, f"{READ_PATH} stopped reading water_risk"
    for hand_copy in CONVERTED_SURFACES:
        assert hand_copy not in owners, (
            f"{hand_copy} reads water_risk directly again — go through "
            f"{READ_PATH} so the schema facts live in one place")
    # ★ #5285: util/water_stress.py was a SECOND reader of this table that
    # this fence never named, because it was a util/ module rather than one of
    # the three routes on the list — so "one read path" was already false when
    # the assertion above was written, and it passed anyway. Name any util
    # module that reads the table, not just the routes.
    stray_utils = {o for o in owners
                   if o.startswith("util" + os.sep) and o != READ_PATH}
    assert not stray_utils, (
        f"a second util module reads water_risk: {sorted(stray_utils)}. That "
        f"is the shape of the #5262/#5263 duplicate — util/water_stress.py "
        f"owned STATE_WATER_STRESS_SQL and its own band for a day without "
        f"this fence noticing. Fold it into {READ_PATH}.")


def test_baseline_water_stress_is_never_read_back():
    """NULL on all 51 rows — falling back to it is how a good read became null."""
    for path, sqls in _modules_touching_water_risk():
        for sql in sqls:
            assert "baseline_water_stress" not in sql, path


# ----------------------------------------------------------------- anti-vacuous

def test_fence_is_not_vacuous():
    """Per #2062: a fence that greens because its subject vanished is worse
    than no fence. Prove the things being inspected still exist."""
    scanned = _modules_touching_water_risk()
    assert scanned, "nothing reads water_risk — this fence would inspect nothing"
    joined = " ".join(s for _, sqls in scanned for s in sqls)
    assert "water_stress_score" in joined, "no reader selects the live column"
    # Every converted module must still CALL the read path, or the
    # behavioural tests above are exercising code nothing reaches — and
    # test_water_risk_has_one_read_path would pass on a route whose water
    # read was deleted rather than consolidated.
    for rel, accessor in READ_PATH_CALLERS:
        src = open(os.path.join(_repo_root(), rel), encoding="utf-8").read()
        assert accessor + "(cur," in src, (
            f"{rel} no longer calls {accessor}() — either the read was "
            f"dropped, or it went back to its own SQL under a spelling the "
            f"scan above does not catch")
    assert hasattr(mb, "_section_risk")
    assert hasattr(hb, "_section_water")
    assert wr.STRESSED_BAND == 4


# ------------------------------------------------ the two psycopg2 row shapes
#
# ★ This is the trap that consolidating routes/site_simulator.py onto this
# module walked into. The two briefs read with a plain `conn.cursor()`, which
# yields TUPLES. site_simulator reads with a RealDictCursor, which yields a
# dict subclass — so the positional `row[0]` this module used raises
# `KeyError: 0` there instead of returning the first column.
#
# It matters because of WHERE that raise lands. `_shape` runs after
# try_fetchone has already returned, so a throw inside it is not turned into
# the `(None, "Type: msg")` this module promises to every caller. It unwinds
# into the caller's own `except` — and in site_simulator that except wraps the
# whole cursor block, so the DCPI and tax reads queued after water would never
# run. That is the #5259 cascade exactly, rebuilt out of a row shape, arriving
# through the shared read path that exists to prevent it.


def _dict_row(pairs):
    """A RealDictCursor row: a dict subclass, NOT a positional sequence."""
    return psycopg2.extras.RealDictRow(pairs)


def test_a_dict_row_really_does_index_by_key_not_position():
    """The premise the fences below rest on, asserted rather than assumed.

    If psycopg2 ever makes RealDictRow positionally indexable, this goes red
    and someone re-reads the comment above, instead of the fences quietly
    ceasing to test anything.
    """
    row = _dict_row([("water_stress_score", _AZ_SCORE), ("bws_category", "High")])
    with pytest.raises(KeyError):
        row[0]


def test_the_read_path_gives_one_answer_under_either_cursor_factory():
    """One read path has to mean one answer, whichever cursor the caller used."""
    as_tuple = wr._shape((_AZ_SCORE, "High"))
    as_dict = wr._shape(_dict_row([("water_stress_score", _AZ_SCORE),
                                   ("bws_category", "High")]))
    assert as_tuple == as_dict, (as_tuple, as_dict)
    assert as_dict["score"] == _AZ_SCORE
    assert as_dict["band"] == 4
    assert as_dict["category"] == "High"
    assert as_dict["stressed"] is True


def test_a_dict_row_does_not_escape_as_an_unnamed_throw():
    """A RealDictCursor read must come back as (value, None) — never a raise.

    A raise here is invisible to `read_errors`: it is not the named error this
    module contracts to return, so the caller reports a generic cursor failure
    at best and loses every read after it at worst.
    """
    cur = _FakeCursor(rows={"FROM water_risk": [
        _dict_row([("water_stress_score", _AZ_SCORE), ("bws_category", "High")])]})
    out, err = wr.read_state_stress(cur, "AZ")
    assert err is None
    assert out["score"] == _AZ_SCORE
    assert out["band"] == 4


def test_the_many_state_read_survives_a_dict_row_too():
    """`UPPER(state) AS state` is what makes the key readable by name."""
    cur = _FakeCursor(rows={"FROM water_risk": [
        _dict_row([("state", "AZ"), ("water_stress_score", _AZ_SCORE),
                   ("bws_category", "High")]),
        _dict_row([("state", "AK"), ("water_stress_score", _AK_SCORE),
                   ("bws_category", "Low-Medium")]),
    ]})
    out, err = wr.read_states_stress(cur, ["AZ", "ak"])
    assert err is None
    assert out["AZ"]["band"] == 4
    assert out["AK"]["band"] == 2


def test_the_many_state_sql_still_aliases_the_key_column():
    """If the alias goes, the dict path above silently keys on `upper`.

    ★ THIS FENCE COULD NOT FAIL UNTIL #5285. It asserted the SUBSTRING
    "upper(state) as state", which is also present in "upper(state) AS
    state_code" — so renaming the alias to anything STARTING with `state`
    passed. Caught by mutating the alias to `state_code` and watching all 75
    tests stay green: read_states_stress would then have keyed every row off
    a column the row does not carry, dropped all 51 states at `if not key:
    continue`, and published an empty enrichment with no error. Match the
    alias as a whole token.
    """
    flat = " ".join(wr._SQL_MANY.split()).lower()
    assert re.search(r"upper\(state\)\s+as\s+state\b", flat), wr._SQL_MANY


def test_the_many_state_alias_and_the_dict_lookup_cannot_drift_apart():
    """The alias in the SQL and the name the dict path looks up are ONE fact
    stored in two places. Pin them to each other rather than to a literal.

    The fixtures above hand back a dict keyed `state` no matter what the SQL
    selects — a FakeCursor answers on a substring match and does not parse the
    SELECT list — so they cannot catch a one-sided rename. This reads the
    alias out of the real SQL, keys a row by THAT, and requires the read path
    to find it: now either side moving alone fails, and both moving together
    passes, which is the actual invariant.
    """
    flat = " ".join(wr._SQL_MANY.split())
    m = re.search(r"(?i)UPPER\(state\)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)", flat)
    assert m, wr._SQL_MANY
    alias = m.group(1)

    cur = _FakeCursor(rows={"FROM water_risk": [
        _dict_row([(alias, "AZ"), ("water_stress_score", _AZ_SCORE),
                   ("bws_category", "High")])]})
    out, err = wr.read_states_stress(cur, ["AZ"])
    assert err is None
    assert list(out) == ["AZ"], (
        f"_SQL_MANY aliases the key column as {alias!r} but read_states_stress "
        f"looks it up under a different name, so every row is dropped at "
        f"`if not key: continue` and the enrichment publishes {out!r} with no "
        f"error to show for it")
    assert out["AZ"]["band"] == 4
