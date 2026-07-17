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
import sys

import pytest

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
_NETIX = pathlib.Path(__file__).resolve().parents[1] / "network_ix_ingestion.py"


def _netix_func_src(name: str) -> str:
    """Slice a possibly-nested `def name(` out of network_ix_ingestion.py.

    Same never-import-the-app rule as _func_src, but bounded by ast (parsed, not
    imported) rather than by a "stop at the next def" regex. The read routes are
    nested inside register_network_ix_routes(), and facility_connectivity is the
    LAST one — a regex slice overruns it into module-level code and silently
    changes what these guards assert. It did: the blanket-except guard below
    tripped on the phase-92 heartbeat block, ~150 lines past the function.
    """
    src = _NETIX.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(src.splitlines()[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"{name}() not found in network_ix_ingestion.py")


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


# ─── network_ix_ingestion's /api/v1/connectivity/<id> (r-netixreg) ────────
#
# The same carrier link, reached from a second module. This endpoint straddles
# THREE id-spaces, so it broke in a fourth way on top of (1)-(3) above:
#   (4) ORPHANED — the module was never registered, so the route 404'd from
#       March to July 2026 and none of the bugs below could even be observed.
# It also carried mode (1) and (2) at once: `int(fac_id)` against the TEXT
# facilities.id raised text=integer, or ValueError on a hex slug, and a blanket
# `except Exception: pass` swallowed both into carriers=[] — which then silently
# understated connectivity_score, whose carrier term is worth 3 points each.


def test_network_ix_routes_are_registered():
    """The orphan guard: a module nothing calls cannot serve anything.

    network_ix_ingestion.py sat fully written and never registered for ~4
    months while its own docstring described how to wire it up — a reference
    that looks like integration but executes nothing. Assert the real call.
    """
    src = _MAIN.read_text(encoding="utf-8")
    assert "register_network_ix_routes(app, get_db)" in src, (
        "main.py no longer calls register_network_ix_routes — network_ix_ingestion "
        "is orphaned again and /api/v1/connectivity/<id> will 404 silently "
        "(it did exactly this from 2026-03-27 to 2026-07-17)")


def test_connectivity_carrier_join_matches_both_id_spaces():
    """Both cfp id-spaces, same as facility_by_slug/api_v1_map."""
    src = _code_only(_netix_func_src("facility_connectivity"))
    assert "carrier_facility_presence" in src, (
        "facility_connectivity no longer queries carrier_facility_presence")
    assert "df.id::text" in src, (
        "the integer id-space join (discovered_facilities.id::text) is gone — "
        "that space holds 604,332 of the 608,804 links")
    assert "merged_facility_id" in src, (
        "the hex id-space join (merged_facility_id -> facilities.id) is gone")


def test_connectivity_does_not_int_cast_the_facility_id():
    """`int(fac_id)` is the original bug, broken in both directions.

    facilities.id is TEXT (hex16) live: int(fac_id) either raises ValueError on
    a hex slug or produces `operator does not exist: text = integer`.
    """
    src = _code_only(_netix_func_src("facility_connectivity"))
    assert "int(fac_id)" not in src, (
        "int(fac_id) is back in facility_connectivity: facilities.id is TEXT, so "
        "this raises ValueError on a hex slug or text=integer on the compare — "
        "and either way the carrier list silently empties")


def test_connectivity_never_casts_indexed_column():
    """Cast the OTHER side — casting dchub_facility_id kills idx_cfp_dchub."""
    src = _code_only(_netix_func_src("facility_connectivity"))
    assert not re.search(r"dchub_facility_id\s*::", src), (
        "facility_connectivity casts the indexed column dchub_facility_id, which "
        "makes idx_cfp_dchub(dchub_facility_id) unusable — cast the other side")


def test_connectivity_carrier_query_does_not_swallow_errors():
    """The carrier query's handler must re-raise what it does not expect.

    `except Exception: pass` around this query is exactly how the endpoint
    served carriers=[] behind a 200 for four months. Scoped to the innermost
    try wrapping the carrier SQL: the route's conn.close() cleanup uses the
    same swallow-everything pattern legitimately (house style throughout this
    file), so a whole-function ban would fail on unrelated code.
    """
    src = _NETIX.read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)
               and n.name == "facility_connectivity"), None)
    assert fn, "facility_connectivity() not found"
    tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
             and "carrier_facility_presence" in (ast.get_source_segment(src, t) or "")]
    assert tries, "no try block wraps the carrier query any more"
    inner = min(tries, key=lambda t: t.end_lineno - t.lineno)
    for h in inner.handlers:
        body = ast.get_source_segment(src, h) or ""
        assert "raise" in body, (
            "the carrier query's exception handler swallows everything instead "
            "of re-raising the unexpected: that turns a real SQL error into a "
            "silent carriers=[] and an understated connectivity_score")


def test_connectivity_bridges_the_pdb_id_space():
    """pdb_* rows are keyed by PeeringDB's fac_id, never by facilities.id.

    The two spaces overlap in 0 of 59,728 rows, so without the source_id bridge
    a caller passing the platform's own hex id gets 0 networks and 0 IX while
    still getting a 200 and a confident-looking connectivity_score.
    """
    src = _code_only(_netix_func_src("facility_connectivity"))
    assert "_resolve_facility_ids" in src, (
        "facility_connectivity no longer resolves the PeeringDB/dchub id-spaces "
        "— it is passing one caller id to both, which cannot match both")
    resolver = _code_only(_netix_func_src("_resolve_facility_ids"))
    assert "source_id" in resolver and "PeeringDB" in resolver, (
        "the facilities.source_id -> PeeringDB fac_id bridge is gone from "
        "_resolve_facility_ids; it is the only link between the two id-spaces")


@pytest.mark.parametrize("job", ["network_sync_job", "ix_sync_job",
                                 "campus_sync_job", "peeringdb_full_sync_job"])
def test_sync_jobs_are_key_gated(job):
    """The sync triggers each start a multi-minute PeeringDB crawl.

    While the module was orphaned these were unauthenticated and unreachable;
    registering it made them reachable, so the gate has to hold.
    """
    src = _NETIX.read_text(encoding="utf-8")
    assert re.search(rf"@require_internal_key\s*\n\s*def {job}\(", src), (
        f"{job} is not decorated with @require_internal_key — that exposes an "
        "unauthenticated POST which kicks off a bulk PeeringDB crawl")


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
def test_resolver_rejects_ids_that_exist_in_neither_space():
    """An id that resolves nowhere must resolve to nothing — not to itself.

    _resolve_facility_ids used to accept any digit string as a PeeringDB
    fac_id on faith, so /api/v1/connectivity/99999999 answered 200 with
    score=0 / "Limited": an unknown id dressed up as a real but badly-connected
    facility. Imported here (not in main) and only under a live DB.
    """
    pytest.importorskip("psycopg2")
    import psycopg2
    sys.path.insert(0, str(_NETIX.parent))
    from network_ix_ingestion import _resolve_facility_ids

    conn = psycopg2.connect(_DB)
    try:
        with conn.cursor() as c:
            assert _resolve_facility_ids(c, "99999999") == (None, None), (
                "a non-existent numeric id resolved — it will report zero "
                "networks rather than 404, which reads as a real verdict")
            assert _resolve_facility_ids(c, "notafacility") == (None, None)
            # and a real one still resolves, both directions
            pdb_id, dchub_id = _resolve_facility_ids(c, "4024")
            assert pdb_id == "4024" and dchub_id, (
                "the raw-PeeringDB-id contract broke")
    finally:
        conn.close()


@_live
def test_pdb_fac_id_is_a_separate_space_from_facilities_id():
    """The invariant that forces the source_id bridge to exist.

    If these two ever DID overlap, keying the pdb_* reads straight off a dchub
    facilities.id would be correct and the bridge would be dead weight. They do
    not: 0 of 59,728 rows match. This test states that out loud so nobody
    "simplifies" _resolve_facility_ids away.
    """
    (overlap,), = _q("""
        SELECT COUNT(*) FROM pdb_network_facilities nf
        JOIN facilities f ON f.id = nf.fac_id
    """)
    assert overlap == 0, (
        f"{overlap} pdb_network_facilities.fac_id values now match facilities.id "
        "— the id-spaces have converged; re-check whether _resolve_facility_ids "
        "is still needed rather than assuming this test is broken")


@_live
def test_source_id_bridge_resolves_networks_for_a_known_facility():
    """A PeeringDB-sourced facility must resolve >0 networks through the bridge.

    Anchored on the hex facilities.id -> source_id hop the endpoint actually
    uses, so it fails if that bridge collapses (which returned 0 networks for
    every caller passing a platform id, with a 200 and a score of 0).
    """
    rows = _q("""
        SELECT f.id, f.source_id, COUNT(nf.id) AS nets
        FROM facilities f
        JOIN pdb_network_facilities nf ON nf.fac_id = f.source_id
        WHERE f.source = 'PeeringDB'
        GROUP BY 1, 2 ORDER BY nets DESC LIMIT 1
    """)
    assert rows, (
        "no facility bridges to pdb_network_facilities via source_id — the "
        "PeeringDB network layer is unreachable from a dchub facility id")
    assert rows[0][2] > 0


@_live
def test_pdb_snapshot_is_not_silently_ancient():
    """The reads serve a snapshot; a frozen one must be visible, not implied.

    pdb_* froze on 2026-03-27 for ~4 months because no scheduler existed. The
    weekly peeringdb_network_sync now refreshes it. This asserts the freshness
    stamp the endpoints publish is actually derivable — not that it is recent,
    since a fresh checkout against a stale replica is legitimate.
    """
    (stamp,), = _q("SELECT MAX(synced_at) FROM pdb_networks")
    assert stamp is not None, (
        "pdb_networks.synced_at is entirely NULL — /api/v1/connectivity's "
        "data_as_of would read as null and the snapshot's true age is unknowable")


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
