"""Guards for the 2026-07-31 schema repair on /api/v1/reveal-validation-feed.

The handler shipped against a GUESSED column list, recorded in a comment as
"Expected schema: discovered_facilities(id, name, lat, lng, status,
nameplate_mw, announcement_date, updated_at, state)", that was never checked
against the live table. Five of those nine columns do not exist:

    lat               -> latitude
    lng               -> longitude
    nameplate_mw      -> power_mw
    updated_at        -> last_updated
    announcement_date -> NO EQUIVALENT (see below)

So every call raised UndefinedColumn. A bare `except Exception` downgraded it
to `logger.warning` and the route returned HTTP 200 with `"facilities": []`.
The endpoint has therefore NEVER returned a row, while
docs/NLR_LEGAL_REDLINE_NOTES.md records it as "live since 2026-Q1" and
"exercised by NLR partner keys today". A dead endpoint that answers 200 is
why two quarters passed without anyone noticing.

Three separate failure modes are pinned here, because fixing only the first
would still ship a wrong feed:

 1. THE COLUMNS. Verified against the read replica 2026-07-31.

 2. THE STATUS FILTER. The default list was lowercase
    ["operational","under_construction","planned","announced"] fed to a
    case-sensitive `status IN (...)`. Live values are Title-Case
    ('Operational' 13,869 · 'Announced' 1,409 · 'Under Construction' 153 ·
    'Planned' 90), so even with the columns fixed it matched 12 rows of
    23,315. Must go through util/status_taxonomy, per #2058.

 3. THE MISSING DATE. There is no announcement date on this table.
    `first_seen` is a DC Hub observation stamp whose live distribution is two
    2026-03 backfills (11,361 + 9,849 rows), and every value is in 2026 — so
    substituting it would push 100% of the feed into the single "2030"
    projection bucket and manufacture a signal out of a stamp. That exact
    mistake is already written up as BUG-034 in
    dchub-frontend/QA-REPORT-v7.9.10.md, where a missing announcement_date
    fell back to Date.now() and stacked ~100 GW onto one month. So
    announcement_date is reported as null and the bucket is derived from
    `expected_completion` (10 of 23,315 rows) with its coverage declared.

Pure source/AST + stub-DB asserts. No DB, no network, no `import main`.
Nothing here runs at module scope.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_REVEAL = "reveal_endpoints.py"
_HANDLER = "reveal_validation_feed"

# Columns the shipped query named that do not exist on discovered_facilities.
PHANTOM_COLUMNS = ("lat", "lng", "nameplate_mw", "announcement_date", "updated_at")

# The live names they map onto (announcement_date has none).
REAL_COLUMNS = ("latitude", "longitude", "power_mw", "last_updated", "first_seen")


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel_path):
    tree = ast.parse(_read(rel_path))
    # Guard the guard: a degenerate parse makes every search below pass
    # vacuously (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{rel_path} parsed to a degenerate module — this harness is not "
        "looking at the real file")
    return tree


def _fn(tree, name, rel_path):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{rel_path} no longer defines {name}() — these "
                         "schema guards need updating, not deleting")


def _code_strings(node):
    """Every string constant in `node` EXCEPT its docstring.

    Scanning CODE only is the point, and it matters more here than usual: the
    handler's own docstring names all five phantom columns in order to explain
    the bug. Including prose would let that explanation satisfy the guard and,
    the direction that actually bites, let a real regression hide behind it.
    Same harness as tests/test_ai_capacity_depth_basis.py.
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    assert body, f"{node.name} has no body beyond its docstring"

    out, consumed = [], set()

    def render_fstring(n):
        parts = []
        for v in n.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                consumed.add(id(v))
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                parts.append(inner.id if isinstance(inner, ast.Name) else "<expr>")
                for c in ast.walk(inner):
                    consumed.add(id(c))
        return "".join(parts)

    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.JoinedStr):
                out.append(render_fstring(n))
            elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in consumed):
                out.append(n.value)
    return out


def _sql_fragments(node):
    """Only the strings that are actually SQL against discovered_facilities.

    The column guards must NOT see the rest of the function. The response
    payload legitimately keeps `lat`, `lon` and `nameplate_mw` as WIRE FIELD
    NAMES — that is the partner-facing contract and is deliberately unchanged.
    What must never reappear is those names as SQL IDENTIFIERS. Scanning every
    string in the handler would conflate the two and make this guard
    unsatisfiable without breaking the payload shape.

    Matching on the table NAME alone is not enough either: the handler also
    names discovered_facilities in two log messages, two error payloads, the
    source attribution and the date_basis prose — 7 strings, of which 2 are
    SQL. Requiring the FROM ... WHERE shape is what separates a query from a
    sentence about a query.
    """
    frags = [s for s in _code_strings(node)
             if "FROM discovered_facilities" in s and "WHERE" in s]
    assert frags, ("no SQL fragment in the handler selects FROM "
                   "discovered_facilities — re-point this guard")
    return frags


def _sql_identifiers(sql):
    """Bare word tokens, so `lat` does not match inside `latitude`."""
    import re
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql))


# ── 0. must-fail control ─────────────────────────────────────────────────────

def test_harness_actually_reads_the_file():
    """Prove this suite can fail. A conftest/collection abort exits SILENT
    GREEN, so an all-passing schema guard is not by itself evidence it ran
    (2026-07-28). Pin a fact that is true only if the real file was read."""
    src = _read(_REVEAL)
    assert "reveal-validation-feed" in src
    assert _sql_identifiers("latitude") == {"latitude"}
    assert "lat" not in _sql_identifiers("latitude"), (
        "token splitter is broken — every column assert below is vacuous")


# ── 1. the phantom columns must never come back ──────────────────────────────

def test_query_names_no_nonexistent_column():
    idents = set()
    for frag in _sql_fragments(_fn(_tree(_REVEAL), _HANDLER, _REVEAL)):
        idents |= _sql_identifiers(frag)
    for col in PHANTOM_COLUMNS:
        assert col not in idents, (
            f"{col!r} is back in the reveal-validation-feed query. It does not "
            f"exist on discovered_facilities (verified against the read replica "
            f"2026-07-31) and will raise UndefinedColumn on every call.")


def test_query_uses_the_real_columns():
    idents = set()
    for frag in _sql_fragments(_fn(_tree(_REVEAL), _HANDLER, _REVEAL)):
        idents |= _sql_identifiers(frag)
    for col in REAL_COLUMNS:
        assert col in idents, (
            f"{col!r} missing from the query — the feed cannot serve "
            f"coordinates/capacity/dates without it")


# Both guards below assert PER FRAGMENT, never over the concatenation.
# The handler issues two queries against discovered_facilities (the feed
# SELECT and the unclassified COUNT). Checking the joined text let the count
# query's dedup filter satisfy the guard for the feed query — caught by the
# must-fail control, which removed the filter from the feed query and stayed
# green. Same class as the SQL grep-window trap in the agreement shell.

def test_window_is_driven_by_first_seen_not_last_updated():
    """`since` must mean "newly observed". Measured 2026-07-31, a 30-day
    window over last_updated returns 10,596 rows against 1,292 for first_seen,
    because last_updated is dominated by bulk re-stamps (10,313 rows share
    2026-08-01). Filtering on it would report re-touched rows as new."""
    frags = _sql_fragments(_fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    for frag in frags:
        sql = " ".join(frag.split())
        assert "first_seen >=" in sql, (
            f"a discovered_facilities query is not windowed on first_seen: {sql!r}")
        assert "last_updated >=" not in sql, (
            f"since-window moved back onto last_updated in: {sql!r} — that "
            "counts bulk re-stamps as new observations")


def test_duplicates_are_filtered():
    """7,649 of 23,315 rows carry is_duplicate=1. is_duplicate is INTEGER
    NOT NULL here, not boolean — COALESCE(is_duplicate, FALSE) is a
    DatatypeMismatch on this table."""
    frags = _sql_fragments(_fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    for frag in frags:
        sql = " ".join(frag.split())
        assert ("COALESCE(is_duplicate, 0) = 0" in sql
                or "COALESCE(is_duplicate,0)=0" in sql), (
            f"a discovered_facilities query lost its dedup filter: {sql!r} — "
            "33% of the table is duplicates")


def test_both_queries_are_actually_checked():
    """Pins the fragment count the two guards above iterate over. If a third
    query appears and is not covered, or the two collapse into one, these
    guards silently change scope."""
    frags = _sql_fragments(_fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    assert len(frags) == 2, (
        f"expected 2 discovered_facilities queries (feed SELECT + unclassified "
        f"COUNT), found {len(frags)}: {[' '.join(f.split())[:70] for f in frags]}")


# ── 2. status must go through the taxonomy, never an inline literal ──────────

def test_no_inline_lowercase_status_vocabulary():
    """The original default list matched 12 rows of 23,315 because live values
    are Title-Case and the comparison was case-sensitive."""
    fn = _fn(_tree(_REVEAL), _HANDLER, _REVEAL)
    code = " ".join(_code_strings(fn))
    for literal in ("under_construction", "'operational'", '"operational"'):
        assert literal not in code, (
            f"{literal!r} re-inlined into the handler — status vocabulary "
            "belongs in util/status_taxonomy.py (#2058), and a bare "
            "case-sensitive IN(...) of lowercase literals matches ~nothing")


def test_status_predicate_normalises_case():
    src = _read(_REVEAL)
    assert "from util import status_taxonomy" in src, (
        "handler no longer imports the canonical status taxonomy")
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    assert "status_taxonomy.norm_expr" in fn_src, (
        "caller-supplied statuses are no longer matched on LOWER(TRIM(...))")
    for helper in ("operational_sql", "pipeline_sql", "unclassified_sql"):
        assert f"status_taxonomy.{helper}" in fn_src, (
            f"handler stopped using status_taxonomy.{helper}()")


def test_unclassified_is_counted_not_folded():
    """141 rows in this table carry a NULL status. They must be surfaced, not
    silently absorbed into operational — the whole reason the taxonomy is an
    allow-list with a third bucket."""
    src = _read(_REVEAL)
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    assert "unclassified_excluded" in fn_src, (
        "the unclassified count is no longer reported to the caller")


# ── 3. the missing date must stay missing, not get substituted ───────────────

def test_announcement_date_is_not_substituted():
    """Any of first_seen/discovered_at/last_updated stuffed into
    announcement_date dates every row to 2026 and collapses the entire feed
    into one projection bucket. That is BUG-034 (frontend QA v7.9.10) all over
    again: a missing date defaulted, producing a fake ~100 GW spike."""
    src = _read(_REVEAL)
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    tree = ast.parse(ast.unparse(ast.parse(fn_src)))

    # Scope to the PER-FACILITY row dict, identified by its own key set. The
    # `date_basis` block legitimately carries an "announcement_date" key whose
    # value is the prose explaining why the field is null; flagging that would
    # forbid documenting the decision.
    row_dicts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if {"id", "name", "status", "announcement_date"} <= keys:
            row_dicts.append(node)
    assert len(row_dicts) == 1, (
        f"expected exactly one per-facility row dict, found {len(row_dicts)} — "
        "re-point this guard rather than weakening it")

    found = []
    for k, v in zip(row_dicts[0].keys, row_dicts[0].values):
        if (isinstance(k, ast.Constant) and k.value == "announcement_date"
                and not (isinstance(v, ast.Constant) and v.value is None)):
            found.append(ast.unparse(v))
    assert not found, (
        f"announcement_date is being populated from {found!r}. "
        "discovered_facilities carries no announcement date; any stand-in is a "
        "guess presented to a partner as an observation.")


def test_projection_bucket_comes_from_expected_completion():
    src = _read(_REVEAL)
    assert "_completion_year" in src and "_projection_bucket" in src, (
        "projection-bucket helpers were removed")
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), "_completion_year", _REVEAL))
    assert "_YEAR_RE" in fn_src or "search" in fn_src, (
        "_completion_year no longer extracts a year — expected_completion is "
        "free text ('2027', 'Jun 2027', 'Q3 2027'), so fromisoformat rejects it")


def test_completion_year_parses_the_live_free_text_values():
    """Executed against the real helper. These are the only 10 non-null
    expected_completion values on the table as of 2026-07-31."""
    ns = {}
    src = _read(_REVEAL)
    tree = _tree(_REVEAL)
    pre = [n for n in tree.body
           if (isinstance(n, ast.Import) and any(a.name == "re" for a in n.names))
           or (isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name)
               and n.targets[0].id in ("_YEAR_RE", "PROJECTION_BUCKETS"))]
    fns = [_fn(tree, "_completion_year", _REVEAL), _fn(tree, "_projection_bucket", _REVEAL)]
    assert pre and len(fns) == 2, "helper fragments moved — re-point this guard"
    exec(compile(ast.Module(body=pre + fns, type_ignores=[]),
                 "<reveal:helpers>", "exec"), ns)

    cy, pb = ns["_completion_year"], ns["_projection_bucket"]
    assert cy("2026") == 2026
    assert cy("Jun 2027") == 2027
    assert cy("Q3 2027") == 2027
    assert cy("2042") == 2042
    # No year present, and the absent case — never a default.
    assert cy(None) is None
    assert cy("") is None
    assert cy("TBD") is None

    # Buckets are the reVeal horizons, and unknown stays unknown.
    assert pb(None) == "unknown"
    assert pb(2026) == "2030"
    assert pb(2042) == "2045"
    assert pb(2051) == "2050+"
    assert pb(2025) == "2025"


def test_payload_declares_its_coverage():
    """reVeal runs a validation rig against this feed. Measured 2026-07-31, of
    500 returned rows only 51 carry coordinates, 0 carry a non-zero MW figure
    and 0 carry expected_completion. A modeller who assumes those fields are
    populated validates against empty rows. The payload has to say so."""
    src = _read(_REVEAL)
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    for key in ("field_coverage", "date_basis", "status_basis", "since_basis"):
        assert key in fn_src, f"{key} dropped from the payload"
    assert "with_nonzero_nameplate_mw" in fn_src, (
        "present-but-zero and absent MW were collapsed — power_mw is 0.0 on "
        "most Operational rows and NULL on most Announced ones, and reporting "
        "them together reads as 'we measured 0 MW'")


def test_coverage_is_computed_after_the_projection_filter():
    """field_coverage['rows'] must equal count. Computed before the
    projection_year filter it reported rows=200 next to count=0 — two
    denominators in one payload. Caught end-to-end against the replica."""
    fn = _fn(_tree(_REVEAL), _HANDLER, _REVEAL)
    filter_line = coverage_line = None
    # Positional claim, so read source order — ast.walk would not preserve it.
    src_lines = ast.unparse(fn).splitlines()
    for i, line in enumerate(src_lines):
        if "projection_bucket'] == str(projection_year)" in line.replace('"', "'"):
            filter_line = i
        if "'with_coordinates'" in line.replace('"', "'"):
            coverage_line = i
    assert filter_line is not None, "projection_year filter not found"
    assert coverage_line is not None, "field_coverage block not found"
    assert coverage_line > filter_line, (
        "field_coverage is computed BEFORE the projection_year filter, so "
        "coverage.rows will disagree with count whenever that filter is used")


# ── 4. failures must stop being reported as an empty 200 ─────────────────────

def test_schema_errors_are_not_swallowed():
    """The bare `except Exception` + logger.warning + fallthrough to a 200 is
    precisely why this endpoint stayed dead for two quarters. A query that did
    not run must never be presented as a feed that found nothing."""
    src = _read(_REVEAL)
    fn = _fn(_tree(_REVEAL), _HANDLER, _REVEAL)

    handlers = [h for node in ast.walk(fn) if isinstance(node, ast.Try)
                for h in node.handlers]
    caught = set()
    for h in handlers:
        for n in ast.walk(h.type) if h.type is not None else []:
            if isinstance(n, ast.Attribute):
                caught.add(n.attr)
            elif isinstance(n, ast.Name):
                caught.add(n.id)
    assert "ProgrammingError" in caught, (
        "psycopg2.ProgrammingError (parent of UndefinedColumn/UndefinedTable) "
        "is no longer caught explicitly — a schema error will fall into a "
        "generic handler and be reported as success again")

    fn_src = ast.get_source_segment(src, fn)
    assert "logger.exception" in fn_src, (
        "query failures downgraded back to warning-level logging")
    assert "500" in fn_src and "503" in fn_src, (
        "the handler no longer returns an error status for a failed query")


def test_no_bare_except_around_the_query():
    """A bare `except Exception` that returns the success payload is the exact
    shape of the original bug. Bare excepts are allowed only for the
    best-effort rollback/close cleanups, which return nothing."""
    fn = _fn(_tree(_REVEAL), _HANDLER, _REVEAL)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            is_bare = h.type is None or (isinstance(h.type, ast.Name)
                                         and h.type.id == "Exception")
            if not is_bare:
                continue
            body_src = " ".join(ast.unparse(s) for s in h.body)
            assert body_src.strip() in ("pass",) or "close" in body_src, (
                f"broad except in {_HANDLER} does more than best-effort "
                f"cleanup: {body_src!r}. That is how UndefinedColumn became a "
                "200 with an empty list.")


def test_limit_is_capped_and_validated():
    """`int(request.args.get("limit", 500))` raised ValueError -> 500 on any
    non-integer, and was uncapped, so ?limit=999999 dumped the table to a
    partner key."""
    src = _read(_REVEAL)
    fn_src = ast.get_source_segment(src, _fn(_tree(_REVEAL), _HANDLER, _REVEAL))
    assert "MAX_FEED_LIMIT" in fn_src, "limit is uncapped again"
    assert "limit must be an integer" in fn_src, (
        "a non-integer limit is no longer a 400")
