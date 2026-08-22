"""fiber_kmz_routes: the writer must name its conflict target (2026-08-22).

THE BUG: kmz_auto_discovery._fetch_arcgis_routes inserted with a BARE
`ON CONFLICT DO NOTHING` while the live table had no unique index except the
PK. That is legal SQL that never fires, so every one of the same 15,082
features was re-inserted every cycle — 12,296,960 rows / 10 GB by 2026-08-22
(~70k distinct identities) and a 2.4e9 km distance total. With a named target,
a missing index RAISES instead of silently inserting; the boot check in
init_tables says so in the log.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
IDENTITY = "ON CONFLICT (source_url, kmz_file, md5(coordinates)) DO NOTHING"


def _src(rel):
    return (ROOT / rel).read_text()


def _strings(rel):
    tree = ast.parse(_src(rel))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_live_writer_names_the_identity_target():
    inserts = [s for s in _strings("kmz_auto_discovery.py") if "INSERT INTO fiber_kmz_routes" in s]
    assert inserts, "writer INSERT not found"
    for s in inserts:
        assert IDENTITY in s, "bare ON CONFLICT DO NOTHING never fires without a matching unique index"
        assert "source_url" in s, "identity needs source_url written"


def test_second_writer_shares_the_identity():
    inserts = [s for s in _strings("global_intelligence_agent.py") if "INSERT INTO fiber_kmz_routes" in s]
    assert inserts, "second writer INSERT not found"
    for s in inserts:
        assert IDENTITY in s
        assert "source_url" in s


def test_no_bare_on_conflict_on_fiber_kmz_routes_anywhere():
    bare = re.compile(r"INSERT INTO fiber_kmz_routes[\s\S]{0,600}?ON CONFLICT DO NOTHING")
    offenders = [p.name for p in ROOT.glob("*.py") if bare.search(p.read_text())]
    offenders += [str(p.relative_to(ROOT)) for p in (ROOT / "routes").glob("*.py") if bare.search(p.read_text())]
    assert not offenders, f"bare ON CONFLICT on fiber_kmz_routes in {offenders}"


def test_arcgis_paging_is_ordered():
    assert "orderByFields=OBJECTID" in _src("kmz_auto_discovery.py"), \
        "unordered resultOffset paging overlaps (682 duplicate features per cycle)"


def test_insert_failure_is_not_debug_level():
    src = _src("kmz_auto_discovery.py")
    assert 'logger.debug(f"Batched route insert error' not in src
    assert 'logger.warning(f"Batched route insert error' in src
    assert 'note_swallowed_write("fiber_kmz_routes", where="kmz_auto_discovery._fetch_arcgis_routes")' in src


def test_init_tables_asserts_the_identity_index_but_does_not_create_it():
    src = _src("kmz_auto_discovery.py")
    assert "md5(coordinates)" in src
    creators = [s for s in _strings("kmz_auto_discovery.py")
                if re.search(r"CREATE UNIQUE INDEX\s+(IF NOT EXISTS\s+)?\w", s)]
    assert not creators, \
        "never create the unique index inside init_tables (aborts boot on duplicates)"
    assert "fiber_kmz_routes has NO identity UNIQUE index" in src
