"""Guards on /api/v1/facilities/state-status-counts — the /daily data spine.

This endpoint published an unfiltered count for months. Every /daily share
image, and every number screenshotted off one, ran ~1.8x high and disagreed
with our own public /api/v1/facilities for the same state. Nothing failed.

main.py is never imported by this suite (it opens pools, starts threads and
registers ~200 blueprints), so we pull the real objects out of the source
with `ast` and exercise them against stubs — the house pattern.

Measured on the Neon read replica, 2026-08-03, US rows carrying a state:
    operational  6,181 raw  ->  3,392 with duplicate_of_id IS NULL
    announced    1,050 raw  ->  1,032 deduped, of which 1,023 have NO state
"""
import ast
import re
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"
FUNC = "facilities_state_status_counts"


def _tree():
    src = MAIN.read_text()
    return src, ast.parse(src)


def _func_src(name):
    """The function as PARSED CODE, not as source text.

    ★ These assertions must never be satisfiable by a comment. The first cut
    of this file checked `"duplicate_of_id IS NULL" in source` — and passed
    against a build where the filter had been replaced with TRUE, because the
    comment above it explains the filter and mentions the string twice. Two of
    three injected regressions survived. ast.unparse() drops every comment and
    docstring, so what is left is only what actually executes.
    """
    src, tree = _tree()
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert node is not None, f"{name}() not found in main.py"
    body = ast.unparse(node)
    assert "#" not in body.split("'")[0][:200]      # sanity: comments are gone
    return body


def _load(names):
    """exec only the named top-level assigns/defs, so main.py's imports and
    module-scope side effects never run."""
    src, tree = _tree()
    ns, got = {}, set()
    for n in tree.body:
        found = ({t.id for t in n.targets if isinstance(t, ast.Name)}
                 if isinstance(n, ast.Assign)
                 else {n.name} if isinstance(n, ast.FunctionDef) else set())
        if found & set(names):
            exec(compile(ast.Module([n], []), "<main>", "exec"), ns)
            got |= found & set(names)
    missing = set(names) - got
    assert not missing, f"could not extract {missing} from main.py"
    return ns


# ── the regression that actually shipped ────────────────────────────────────

def test_every_facility_read_applies_the_dedup_fleet_filter():
    body = _func_src(FUNC)
    assert "discovered_facilities" in body and "GROUP BY" in body   # anti-vacuous

    # Assert on the BINDING, not on the string appearing somewhere. The
    # payload's own `basis` block quotes the predicate as documentation, so
    # a substring check over the function passes even with the filter removed
    # — the data-shaped version of the comment trap above.
    assert re.search(r"_FLEET\s*=\s*'duplicate_of_id IS NULL'", body), (
        "state-status-counts must filter duplicate_of_id IS NULL. Every other "
        "facility surface does; without it this endpoint publishes ~1.8x the "
        "canonical count and contradicts /api/v1/facilities for the same state. "
        "Describing the filter in the payload is not applying it.")

    # BOTH facility passes must interpolate it — the main GROUP BY and the
    # blank-state derive pass. The derive pass is the easy one to miss: it
    # sits in a try/except that swallows its own failure.
    facility_reads = re.findall(r"FROM \{_[sS][rR][cC]\}", body)
    assert len(facility_reads) >= 2, (
        f"expected both facility reads, found {len(facility_reads)}")
    assert body.count("{_FLEET}") >= len(facility_reads), (
        f"{len(facility_reads)} facility SELECTs but only "
        f"{body.count('{_FLEET}')} carry the fleet filter")


def test_blank_status_is_not_silently_published_as_operational():
    """The old code ended `else: b['op'] += n`, so a row with no status was
    published as CONFIRMED OPERATIONAL. Assert on the BRANCH, not on the
    presence of the string 'unknown' — that also appears in the bucket
    initialiser and the totals, so it survives the regression."""
    body = _func_src(FUNC)
    # In unparsed form the guard reads `elif not status:` -> `if not status:`
    m = re.search(r"if not status:\s*\n\s*(.+)", body)
    assert m, ("no blank-status branch found — blank status must have its own "
               "bucket, not fall through to the operational default")
    assert "unknown" in m.group(1), (
        f"blank status assigns to {m.group(1)!r}, expected the 'unknown' "
        f"bucket. Publishing status-less rows as operational is how a new "
        f"source silently inflates the operational count.")


def test_pipeline_read_is_quarantine_guarded_and_reports_what_it_drops():
    body = _func_src(FUNC)
    assert "capacity_pipeline" in body, "announced no longer reads the pipeline table"
    # The guard must be INTERPOLATED INTO the query. Checking for the name
    # alone passes on a build where the SELECT was changed to WHERE TRUE and
    # only the now-unused import still mentions CP_OK.
    m = re.search(r"FROM capacity_pipeline WHERE ([^'\"]+)", body)
    assert m, "could not find the capacity_pipeline SELECT"
    assert "_CP_OK" in m.group(1), (
        f"capacity_pipeline read is not quarantine-guarded (WHERE "
        f"{m.group(1)[:60]!r}). Unguarded, the table carries 4.6x its real GW "
        f"— utility interconnection QUEUES booked as single buildings.")
    assert "pipeline_unplaced" in body, (
        "rows the resolver cannot place must be reported, not swallowed — "
        "~346 GW is non-US/Unknown/junk and silently dropping it reads as "
        "'no pipeline there'")


# ── the resolver ────────────────────────────────────────────────────────────

def test_pipeline_market_resolves_codes_names_and_city_state():
    ns = _load(["_DAILY_US_STATE_NAMES", "_VALID_US_ABBRS",
                "_DAILY_STATE_NAME_TO_ABBR", "_daily_pipeline_state"])
    assert len(ns["_DAILY_US_STATE_NAMES"]) == 51        # 50 + DC
    r = ns["_daily_pipeline_state"]

    assert r("AZ") == "ARIZONA"                  # bare code
    assert r("Michigan") == "MICHIGAN"           # bare full name
    assert r("Paducah, Kentucky") == "KENTUCKY"  # City, State
    assert r("Ashburn, VA") == "VIRGINIA"        # City, CODE
    assert r("LA") == "LOUISIANA"                # code wins over the metro read

    # Must refuse rather than guess: non-US and extraction junk.
    for junk in ("Singapore", "London", "Unknown", "MW", "MW AI", "", None):
        assert r(junk) is None, f"{junk!r} should not resolve to a US state"


def test_pipeline_status_vocabulary_covers_the_live_table():
    """Measured CP_OK-filtered on the replica 2026-08-03: pipeline 606,
    announced 381, under construction 154, planned 53, under_construction 33,
    permitting 17, expansion 10, approved 1. 'pipeline' is the LARGEST bucket
    — dropping it from the announced set silently halves the pipeline."""
    ns = _load(["_DAILY_CP_ANN", "_DAILY_CP_UC"])
    ann, uc = ns["_DAILY_CP_ANN"], ns["_DAILY_CP_UC"]
    assert {"announced", "planned", "pipeline", "permitting"} <= ann
    assert {"under construction", "under_construction"} <= uc
    assert not (ann & uc), f"a status cannot be both: {ann & uc}"


def test_payload_declares_the_basis_of_every_bucket():
    body = _func_src(FUNC)
    assert "'basis'" in body, (
        "the response must name each bucket's table/filter/unit — op and ann "
        "are different tables AND different units (buildings vs projects), and "
        "nothing in the old payload made that checkable")
    for key in ("ann_mw", "'projects'"):
        assert key in body, f"missing {key} in the payload"
