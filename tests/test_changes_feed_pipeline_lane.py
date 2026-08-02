"""The pipeline_new lane of /api/v1/changes.

★ 2026-08-02: this lane had been a DEAD READ since it shipped — it selected
`first_seen`, a column `capacity_pipeline` does not have, so every call
raised UndefinedColumn, got swallowed, and the feed reported zero (later
null) new pipeline projects forever.

The repair is a regex-guarded cast, the pattern already proven in
ai_tracking.get_cumulative_totals() for the same TEXT-compared-as-timestamp
problem. These tests pin the properties that make it safe, because the
failure mode they prevent is SILENT: a lane that throws looks exactly like a
lane with nothing to report.

Source-level assertions (no DB): DB-backed tests skip in CI, so a test that
only ran against a live database would be green-by-absence here.
"""
import ast
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "routes" / "changes_feed.py").read_text()


def _pipeline_lane_region() -> str:
    """The source between the _row_ts definition and the end of the lane call."""
    i = SRC.index("_row_ts = (")
    j = SRC.index("# News: new articles", i)
    return SRC[i:j]


def test_module_parses_and_the_lane_region_is_found():
    # Guards against every assertion below passing vacuously on an empty or
    # unparseable read (the empty-parse-passes-all trap).
    assert ast.parse(SRC)
    region = _pipeline_lane_region()
    assert len(region) > 200
    assert "pipeline_new" in region


def test_the_dead_column_is_gone_from_the_query():
    """`first_seen` must survive only as an output ALIAS, never as a column
    read from capacity_pipeline — reading it is what killed the lane."""
    region = _pipeline_lane_region()
    # The alias is fine and intended; a bare column reference is not.
    assert "AS first_seen" in region
    assert not re.search(r"WHERE\s+first_seen", region)
    assert not re.search(r"ORDER BY\s+first_seen", region)
    assert "first_seen IS NOT NULL" not in region


def test_the_cast_is_regex_guarded_so_it_cannot_throw():
    """CASE guarantees the shape check runs before the cast. Without the
    guard, one malformed created_at takes down the whole lane — and the
    exception is swallowed, so it comes back as 'no new projects'."""
    region = _pipeline_lane_region()
    assert "created_at::timestamptz" in region, "expected a cast"
    m = re.search(r"CASE WHEN created_at ~[^\n]*", region)
    assert m, "the created_at cast must be inside a regex-guarded CASE"
    # The regex test must appear before the cast in the same expression.
    assert region.index("created_at ~") < region.index("created_at::timestamptz")


def test_extracted_at_is_preferred_over_the_cast():
    """extracted_at is a real timestamptz; use it where present and fall back
    to the guarded cast only for the ~73% of rows it does not cover."""
    region = _pipeline_lane_region()
    assert "COALESCE(extracted_at," in region.replace(" ", "").replace(
        "COALESCE(extracted_at,", "COALESCE(extracted_at,"
    ) or "COALESCE(extracted_at" in region
    assert region.index("extracted_at") < region.index("created_at")


def test_where_order_and_output_all_use_the_same_expression():
    """One expression, three uses. If the WHERE and the ORDER BY disagreed
    about which clock they were on, the 'newest N' page would silently not be
    the newest N — the kind of wrongness that never raises."""
    region = _pipeline_lane_region()
    # _row_ts is interpolated, so the literal token appears once per use.
    assert region.count("{_row_ts}") == 3, (
        "expected _row_ts in SELECT, WHERE and ORDER BY — found "
        f"{region.count('{_row_ts}')}"
    )


def test_the_quarantine_guard_is_still_applied():
    """CP_OK gates the 718 quarantined rows (#2064). A repair that restored
    the lane while dropping the guard would publish quarantined data."""
    region = _pipeline_lane_region()
    assert "{CP_OK}" in region
