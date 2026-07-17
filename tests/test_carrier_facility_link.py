"""tests/test_carrier_facility_link.py — the carrier→facility link (2026-07-17).

Guards the r-carrierlink2 fix. `carrier_facility_presence.dchub_facility_id` is
TEXT and holds TWO id-spaces at once:

  · 604,332 rows / 13,866 ids — integer-as-text = discovered_facilities.id
    (the legacy bulk; verified as the TRUE link — all 13,865 matched pairs agree
    on coordinates exactly, max 0.0000 deg, vs ~60 deg for a decoy df.id+1 join)
  · 4,471 rows / 1,793 ids — hex16 = facilities.id (newer ingestion, which
    matches against `SELECT id FROM facilities`)

This link has now broken THREE ways, and each mode is guarded below:
  (1) TYPE — `dchub_facility_id = <integer>` raises `operator does not exist:
      text = integer` → 500 (2fbce20d's storm, n5xx~247).
  (2) SILENT-ZERO — joining the wrong/partial id-space returns 0 carriers while
      looking correct. Joining ONLY facilities.id yields 109 facilities where
      both spaces yield 13,870 — a 127x silent loss that still returns 200.
  (3) STUBBED — serving `NULL AS fiber_providers, 0 AS fiber_carrier_count`
      renders an honest-looking on_net=false for facilities that DO have carriers.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO DB and
must NEVER import main — so the SQL-shape guards below read main.py as TEXT, and
the live-data assertions skip without a DB URL.

Run:  python3 -m pytest tests/test_carrier_facility_link.py -v
"""
from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import re

import pytest

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def _func_src(name: str) -> str:
    """Slice a top-level `def name(` out of main.py's TEXT (never import main)."""
    src = _MAIN.read_text(encoding="utf-8")
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    assert m, f"{name}() not found in main.py"
    nxt = re.search(r"^(?:@app\.route|def |@)", src[m.end():], re.M)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]


def _code_only(src: str) -> str:
    """Drop `#` and `--` comment lines.

    This file's own guards quote the broken joins verbatim, and so do the
    r-carrierlink comments in main.py — without this, documenting a bug would
    trip the test that bans it.
    """
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith(("#", "--")))


# ─── (1)+(2) SQL shape: both id-spaces joined, indexed column never cast ───

def test_facility_by_slug_joins_both_id_spaces():
    """The discovered_facilities branch must match BOTH id-spaces.

    Matching only one space is the silent-zero mode: integer-only misses the
    1,793 hex ids, hex-only collapses 13,870 facilities to 109.
    """
    src = _code_only(_func_src("facility_by_slug"))
    assert "carrier_facility_presence" in src, (
        "facility_by_slug no longer queries carrier_facility_presence — the "
        "fiber-carrier link has been stubbed out again (failure mode 3)")
    # integer space, cast on the df side (not the indexed column)
    assert "discovered_facilities.id::text" in src, (
        "the integer id-space join (discovered_facilities.id::text) is gone — "
        "that space holds 604,332 of the 608,804 links")
    # hex space, via merged_facility_id -> facilities.id
    assert "discovered_facilities.merged_facility_id" in src, (
        "the hex id-space join (merged_facility_id -> facilities.id) is gone")


def test_facility_by_slug_has_no_untyped_integer_join():
    """`dchub_facility_id = discovered_facilities.id` (uncast) is the 500."""
    src = _code_only(_func_src("facility_by_slug"))
    assert not re.search(
        r"dchub_facility_id\s*=\s*discovered_facilities\.id\s*(?!::)", src), (
        "bare `dchub_facility_id = discovered_facilities.id` is back: the column "
        "is TEXT live, so this raises `operator does not exist: text = integer` "
        "and 500s every /api/v1/facility/<slug> request")


def test_facilities_fallback_branch_links_carriers():
    """The legacy-`facilities` branch must use the direct text=text join."""
    src = _code_only(_func_src("facility_by_slug"))
    assert "cfp.dchub_facility_id = facilities.id" in src, (
        "the `facilities` fallback branch is not linking carriers — it serves "
        "rows whose id IS the hex space, so it must join cfp.dchub_facility_id "
        "= facilities.id rather than hard-coding NULL/0")
    assert "NULL AS fiber_providers" not in src, (
        "a hard-coded `NULL AS fiber_providers` stub is back in facility_by_slug")


@pytest.mark.parametrize("fn", ["facility_by_slug", "api_v1_map"])
def test_indexed_column_is_never_cast(fn):
    """Cast the OTHER side — casting dchub_facility_id kills idx_cfp_dchub."""
    src = _code_only(_func_src(fn))
    assert not re.search(r"dchub_facility_id\s*::", src), (
        f"{fn} casts the indexed column dchub_facility_id, which makes the text "
        "index idx_cfp_dchub(dchub_facility_id) unusable — cast the other side")


def test_map_query_joins_both_id_spaces():
    """/api/v1/map must match both spaces — `= f.id` alone is a 127x silent loss."""
    src = _code_only(_func_src("api_v1_map"))
    assert "cfp.dchub_facility_id IN (df.id::text, f.id)" in src, (
        "the map's carrier join no longer matches both id-spaces: joining only "
        "facilities.id shows carriers for 109 facilities instead of 13,870, and "
        "still returns 200 — a silent regression")


def _sql_fstrings(fn: str):
    """Every f-string SQL literal inside a top-level function, via AST."""
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn:
            for sub in ast.walk(node):
                if isinstance(sub, ast.JoinedStr):
                    yield "".join(v.value for v in sub.values
                                  if isinstance(v, ast.Constant))


@pytest.mark.parametrize("fn", ["api_v1_map"])
def test_sql_fstrings_have_no_bare_percent(fn):
    """A literal percent-sign in an f-string SQL 500s the endpoint.

    psycopg2 applies Python percent-formatting to the query, so a stray
    percent-sign — even inside a `--` SQL COMMENT — is parsed as a conversion
    spec, consumes a bound arg and raises "tuple index out of range" /
    "unsupported format character". This cost a live 500 on /api/v1/map on
    2026-07-17: the offending character was in a comment, not in code.
    """
    for sql in _sql_fstrings(fn):
        if "carrier_facility_presence" not in sql:
            continue
        bare = re.findall(r"%(?!s)", sql)
        assert not bare, (
            f"{fn}'s SQL f-string contains {len(bare)} bare percent-sign(s) "
            "(not %s). psycopg2 will read them as format specs and 500 the "
            "endpoint. Spell the word out in prose.")
        try:
            sql % (5, 0)  # emulate psycopg2 binding (limit, offset)
        except Exception as e:  # pragma: no cover - the assert is the point
            pytest.fail(f"{fn}'s SQL does not bind with (limit, offset): "
                        f"{type(e).__name__}: {e}")


# ─── live-data guards (skip without a DB) ─────────────────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL — live link test skipped")

_BOTH_SPACES = """
    SELECT COUNT(DISTINCT cfp.carrier_name)
    FROM carrier_facility_presence cfp
    WHERE cfp.dchub_facility_id IN (df.id::text, df.merged_facility_id)
      AND cfp.carrier_name IS NOT NULL AND cfp.carrier_name <> ''
"""


def _q(sql, args=None):
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(_DB)
    try:
        with conn.cursor() as c:
            c.execute(sql, args or ())
            return c.fetchall()
    finally:
        conn.close()


@_live
def test_known_linked_facility_returns_carriers():
    """A facility known to have carriers must resolve >0 through the real join.

    Identified by provider|name (the canonical stable slug), NOT by the join
    under test — so this cannot pass vacuously if the join collapses to zero.
    """
    provider, name = "Equinix", "Equinix Ashburn DC2"
    hash8 = hashlib.md5(f"{provider}|{name}".encode()).hexdigest()[:8]
    rows = _q(f"""
        SELECT df.name, ({_BOTH_SPACES}) AS carrier_count
        FROM discovered_facilities df
        WHERE LEFT(MD5(COALESCE(df.provider,'')||'|'||COALESCE(df.name,'')),8) = %s
        LIMIT 1
    """, (hash8,))
    assert rows, f"anchor facility {name!r} (slug {hash8}) vanished from " \
                 "discovered_facilities — pick a new anchor, do NOT delete this test"
    assert rows[0][1] > 0, (
        f"{name} resolves 0 on-site fiber carriers — the carrier→facility join "
        "has silently regressed to zero (it had 517)")


@_live
def test_carrier_link_coverage_has_not_collapsed():
    """Fleet-wide guard: the link must not quietly shrink to a rounding error.

    Live at fix time: 13,870 facilities carry >=1 carrier. Joining only the hex
    space yields 109 and still returns 200 — exactly the regression this catches.
    """
    (n,), = _q(f"""
        SELECT COUNT(*) FROM discovered_facilities df
        WHERE ({_BOTH_SPACES}) > 0
    """)
    assert n >= 5000, (
        f"only {n} facilities have on-site carriers (was 13,870 at fix time) — "
        "the join has likely lost an id-space")


@_live
def test_integer_space_is_the_true_link_not_a_collision():
    """The integer-space match is real: matched pairs share coordinates.

    An id-space collision would pair unrelated buildings (a decoy df.id+1 join
    scatters ~60 deg). If this ever drifts, the join is matching by luck.
    """
    (pairs, agree), = _q("""
        SELECT COUNT(*),
               COUNT(*) FILTER (
                 WHERE sqrt(power(cfp.facility_lat - df.latitude, 2)
                          + power(cfp.facility_lng - df.longitude, 2)) < 0.01)
        FROM (SELECT DISTINCT ON (dchub_facility_id) *
              FROM carrier_facility_presence
              WHERE dchub_facility_id ~ '^[0-9]+$') cfp
        JOIN discovered_facilities df ON cfp.dchub_facility_id = df.id::text
        WHERE cfp.facility_lat IS NOT NULL AND df.latitude IS NOT NULL
    """)
    assert pairs > 0, "the integer-space join matched nothing at all"
    assert agree / pairs > 0.95, (
        f"only {agree}/{pairs} integer-space matches agree on coordinates — "
        "dchub_facility_id's integer population may no longer be "
        "discovered_facilities.id")
