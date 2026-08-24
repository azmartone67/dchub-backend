"""A date filter must be a half-open range, never a prefix LIKE (2026-08-24).

178 TEXT columns across 115 tables hold ISO-8601 timestamps. Converting them to
timestamptz is the actual fix, but the conversion cannot even START while
readers filter them with `WHERE <col> LIKE 'YYYY-MM-DD%'`, because the two
existing workaround styles behave OPPOSITELY under `ALTER ... TYPE timestamptz`:

    col::timestamptz > NOW() - INTERVAL ...   becomes a harmless no-op cast
    col LIKE 'YYYY-MM-DD%'                    BREAKS — you cannot LIKE a
                                              timestamptz

So the LIKE sites have to go first, and they have to go to a form that is
correct against BOTH types. That form is the half-open range:

    WHERE col >= '2026-08-24' AND col < '2026-08-25'

It reads correctly on TEXT because the stored values are ISO-8601 and
lexicographic order on ISO-8601 IS chronological order; it reads correctly on
timestamptz because Postgres casts the literals. It is also the only one of the
two that can use a b-tree range scan — a prefix LIKE cannot (the 2026-08-23
LIKE-prefix finding, which measured a 15s scan).

This was not hypothetical. `press_releases.published_at` is ALREADY timestamptz,
and /api/admin/content/stats ran `published_at LIKE %s` against it in the same
loop as social_media_posts. Measured 2026-08-24 against the live database:

    press_releases ... published_at ILIKE '2026-08-24%'
      -> operator does not exist: timestamp with time zone ~~* unknown

Equivalence of the replacement was measured, not assumed — LIKE vs the range,
every day of 2026-08, on the five live TEXT columns this change touches:

    facilities.first_seen          24 days, 0 mismatches (674 rows matched)
    deals.date                     24 days, 0 mismatches (135 rows matched)
    announcements.discovered_at    24 days, 0 mismatches (3,659 rows matched)
    ai_usage_tracking.timestamp    24 days, 0 mismatches (16 rows matched)
    social_media_posts.published_at 24 days, 0 mismatches (73 rows matched)

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_date_window_half_open_range.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Columns whose values are dates/timestamps. A prefix LIKE on one of these is
#: the defect; a LIKE on `city` or `provider` is an ordinary substring search
#: and is none of this test's business.
_DATE_COL = re.compile(
    r"^(.*_at|.*_date|date|timestamp|period|first_seen|last_seen|.*_day|.*_ts)$",
    re.I)

_LIKE_SITE = re.compile(r"\b(\w+)\s+I?LIKE\s+", re.I)

_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "tests",
    "site-packages", "build", "dist", "migrations",
}

#: capacity_tracking has NO `discovered_at` column — the live columns are
#: measured in tests/test_capacity_tracking_dead_lane.py, and the table is empty
#: and writerless. Those two reads raise UndefinedColumn before the predicate is
#: ever evaluated, so converting their LIKE would be repairing a dead lane, which
#: that test explicitly says not to do. Exempt by TABLE, so a new LIKE on a live
#: table is still caught.
_DEAD_TABLE = "capacity_tracking"


def _sql_literals():
    """Yield (relpath, lineno, sql_text) for every SQL string in deployed code.

    f-string pieces are joined with `{}` standing in for each interpolation, so
    an interpolated query still reads as one statement — `first_seen LIKE
    '{today}%'` must not be able to hide from this guard by being an f-string,
    which is exactly the form two of the original sites used.
    """
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, _ROOT)
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                tree = ast.parse(src)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                elif isinstance(node, ast.JoinedStr):
                    text = "".join(
                        p.value if isinstance(p, ast.Constant)
                        and isinstance(p.value, str) else "{}"
                        for p in node.values)
                else:
                    continue
                if not re.search(r"\b(SELECT|UPDATE|DELETE)\b", text, re.I):
                    continue
                yield rel, getattr(node, "lineno", 0), text


def _prefix_like_offenders():
    hits = []
    for rel, lineno, sql in _sql_literals():
        if _DEAD_TABLE in sql:
            continue
        for m in _LIKE_SITE.finditer(sql):
            if _DATE_COL.match(m.group(1)):
                hits.append((rel, lineno, m.group(1), " ".join(sql.split())[:110]))
    # one entry per (file, line, column)
    seen, out = set(), []
    for h in hits:
        key = (h[0], h[1], h[2])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def test_no_date_column_is_filtered_with_a_prefix_like():
    """★THE GATE. 16 such sites existed on 2026-08-24; 14 were converted and 2
    are dead-by-measurement reads of capacity_tracking, exempted by table."""
    offenders = _prefix_like_offenders()
    assert not offenders, (
        "a date/timestamp column is filtered with a prefix LIKE:\n" + "\n".join(
            "  %s:%s  column=%s\n    %s" % o for o in offenders
        ) + "\n\nUse a half-open range instead — `col >= %s AND col < %s` with "
            "(day, day+1). A prefix LIKE cannot use a b-tree range scan, and it "
            "raises `operator does not exist` the moment the column is migrated "
            "from TEXT to timestamptz."
    )


def _cp():
    spec = importlib.util.spec_from_file_location(
        "cp_under_test", os.path.join(_ROOT, "content_publisher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_day_bounds_are_half_open_and_one_day_apart():
    import datetime as _dt
    day, nxt = _cp()._utc_day_bounds(_dt.datetime(2026, 8, 24, 23, 59, 59))
    assert (day, nxt) == ("2026-08-24", "2026-08-25")


def test_day_bounds_roll_over_a_month_end():
    """A naive `day[:-2] + str(int(dd)+1)` would produce '2026-08-32' here."""
    import datetime as _dt
    day, nxt = _cp()._utc_day_bounds(_dt.datetime(2026, 8, 31, 12, 0, 0))
    assert (day, nxt) == ("2026-08-31", "2026-09-01")


@pytest.mark.parametrize("iso,inside", [
    ("2026-08-24", True),                       # date only (deals.date)
    ("2026-08-24T00:00:00", True),              # T separator, midnight
    ("2026-08-24T06:49:22.382829Z", True),      # T + micros + Z
    ("2026-08-24 06:40:36.049322+00", True),    # space separator + offset
    ("2026-08-23T23:59:59.999999", False),
    ("2026-08-25", False),
    ("", False),
])
def test_the_range_selects_exactly_the_day_on_iso_text(iso, inside):
    """The whole premise: on ISO-8601 TEXT, lexicographic order IS chronological
    order, so `>= day AND < day+1` is the same set a prefix LIKE would return.
    Every literal here is a real stored format from the live columns.
    """
    day, nxt = "2026-08-24", "2026-08-25"
    assert (day <= iso < nxt) is inside


def test_scalar_survives_a_realdict_cursor():
    """★REGRESSION. /api/admin/content/stats returned HTTP 500 with the body
    {"error":"0"} because it did `cur.fetchone()[0]` against a RealDictCursor,
    where subscripting by 0 raises KeyError: 0."""
    cp = _cp()

    class _RealDictish(dict):
        pass

    class _Cur:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    assert cp._scalar(_Cur(_RealDictish(n=7))) == 7
    assert cp._scalar(_Cur((7,))) == 7          # plain tuple cursor
    assert cp._scalar(_Cur(None)) == 0          # no row -> default
    assert cp._scalar(_Cur(_RealDictish(count=4))) == 4   # other key name
    with pytest.raises(KeyError):
        _RealDictish(n=7)[0]                    # the exact live failure


#: The one site that still filters press_releases by `status`, recorded rather
#: than repaired. /api/admin/content-queue?type=press is broken in FOUR ways at
#: once — it also selects `content`, `publish_platform` and `approved_at`, none
#: of which exist on the table (live columns: id, title, summary, source,
#: source_url, category, published_date, featured, created_at, slug, date,
#: subheadline, body, meta_description, published, published_at).
#:
#: Measured live 2026-08-24:
#:   GET /api/admin/content-queue?type=press&status=published
#:     -> HTTP 500 {"error": "column \"status\" does not exist"}
#:
#: It is NOT repaired here because press_releases has no draft/approved/rejected
#: concept at all — only `published` boolean — so mapping the queue's four
#: statuses onto it is a product decision, not a mechanical one, and guessing it
#: would quietly mis-populate an approval queue. Fix it and delete this entry in
#: the same change.
_MEASURED_BROKEN_PRESS_QUEUE = "FROM press_releases WHERE status = %s"


def test_content_stats_never_asks_press_releases_for_a_status_column():
    """★REGRESSION. press_releases has no `status` column — it carries
    `published` (boolean). The old loop ran the social_media_posts status
    queries against it too, so every one raised UndefinedColumn."""
    with open(os.path.join(_ROOT, "content_publisher.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    # Only an UNALIASED `FROM press_releases` is this bug. The aliased form,
    # `... EXISTS (SELECT 1 FROM press_releases p WHERE p.published = TRUE)`,
    # is correct and appears ~8 times — the `status =` in those statements
    # belongs to social_media_posts, not to press_releases.
    unaliased = re.compile(r"FROM\s+press_releases\s+(?=WHERE\b)", re.I)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            sql = node.value
        elif isinstance(node, ast.JoinedStr):
            sql = "".join(
                p.value if isinstance(p, ast.Constant)
                and isinstance(p.value, str) else "{}" for p in node.values)
        else:
            continue
        m = unaliased.search(sql)
        if not m or not re.search(r"\bstatus\s*=", sql[m.end():]):
            continue
        if _MEASURED_BROKEN_PRESS_QUEUE in " ".join(sql.split()):
            continue          # recorded above, with the measurement
        bad.append(" ".join(sql.split())[:120])
    assert not bad, (
        "press_releases filtered by `status`, a column it does not have — its "
        "live columns carry `published` (boolean). Measured 2026-08-24: "
        'UndefinedColumn: column "status" does not exist. Offenders: %s' % bad)


def test_the_stats_loop_no_longer_shares_one_query_across_both_tables():
    """The root cause was a single loop asking two different schemas the same
    question. If a `for table in [...]` reappears there, the trap is back."""
    with open(os.path.join(_ROOT, "content_publisher.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "for table in ['social_media_posts', 'press_releases']" not in src, (
        "the shared social_media_posts/press_releases loop is back — those two "
        "tables do not share a schema (status vs published, text vs timestamptz "
        "published_at), which is what made this endpoint 500 on every call")


def test_the_recorded_press_queue_break_is_still_there():
    """The exemption above must not go stale.

    Mirrors tests/test_capacity_tracking_dead_lane.py::
    test_measured_dead_reads_are_still_dead — if someone repairs
    /api/admin/content-queue?type=press, the entry has to go with it, otherwise
    the registry grants a permanent exemption to code that no longer needs one.
    """
    with open(os.path.join(_ROOT, "content_publisher.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert _MEASURED_BROKEN_PRESS_QUEUE in src, (
        "press_releases is no longer filtered by `status` — delete "
        "_MEASURED_BROKEN_PRESS_QUEUE from this test; the exemption is no "
        "longer earned.")


# ---------------------------------------------------------------------------
# The THIRD trap category, which the original triage missed entirely.
#
# The handoff listed two workaround styles and said one was safe:
#     col::timestamptz > NOW() - INTERVAL ...   -> no-op cast, safe
#     col LIKE 'YYYY-MM-DD%'                    -> breaks
# It then RECOMMENDED `NULLIF(last_used_at,'')::timestamptz` as the read that
# works around last_used_at being TEXT. That one BREAKS TOO. Verified live on
# 2026-08-24 against press_releases.published_at, which is already timestamptz:
#
#     published_at <> ''               -> invalid input syntax for type
#     COALESCE(published_at,'') != ''     timestamp with time zone: ""
#     NULLIF(published_at,'')::timestamptz
#     published_at ~ '^[0-9]{4}'       -> operator does not exist
#
# The empty string is the problem: these columns store '' instead of NULL, so
# every reader that copes with that does so by naming '' — and '' is not a
# timestamptz. The fix is `col::text` inside the guard expression, which is
# correct on BOTH types. Verified equivalent on live data:
#     api_keys.last_used_at (106 of 154 blank)
#       old NULLIF(last_used_at,'')::timestamptz            -> 48
#       new NULLIF(last_used_at::text,'')::timestamptz      -> 48
# ---------------------------------------------------------------------------

#: The columns migrations/2026-08-24_text_timestamps_tranche_ab.sql converts.
#: Extend this as later tranches land — the guard only protects what it names.
_MIGRATING = {
    "announcements": ["published_at"],
    "social_media_posts": ["approved_at"],
    "api_keys": ["created_at", "expires_at", "last_used_at", "last_reset_date"],
    "construction_permits": ["created_at", "discovered_at", "issued_date"],
    "news_articles": ["created_at", "fetched_at", "published_at"],
}

_TEXT_ONLY = [
    ("NULLIF-empty",   r"NULLIF\s*\(\s*(?:\w+\.)?(%s)\s*,\s*''"),
    ("COALESCE-empty", r"COALESCE\s*\(\s*(?:\w+\.)?(%s)\s*,\s*''"),
    ("compare-empty",  r"\b(?:\w+\.)?(%s)\s*(?:=|<>|!=)\s*''"),
    ("substr/left",    r"\b(?:substr|substring|left|right)\s*\(\s*(?:\w+\.)?(%s)\b"),
    ("regex-match",    r"\b(?:\w+\.)?(%s)\s*!?~\*?\s"),
]


def test_no_text_only_operation_survives_on_a_column_being_migrated():
    """★THE SECOND GATE. `col::text` inside the guard, or the ALTER breaks it."""
    names = sorted({c for cs in _MIGRATING.values() for c in cs})
    pats = [(lab, re.compile(p % "|".join(names), re.I)) for lab, p in _TEXT_ONLY]
    offenders = []
    for rel, lineno, sql in _sql_literals():
        if not re.search(r"\b(SELECT|UPDATE|DELETE|INSERT)\b", sql, re.I):
            continue
        tables = [t for t in _MIGRATING if re.search(r"\b%s\b" % t, sql, re.I)]
        if not tables:
            continue
        live = {c.lower() for t in tables for c in _MIGRATING[t]}
        for label, pat in pats:
            for m in pat.finditer(sql):
                if m.group(1).lower() in live:
                    offenders.append((rel, lineno, label, m.group(1)))
    offenders = sorted(set(offenders))
    assert not offenders, (
        "a column scheduled for TEXT -> timestamptz is used in a way only TEXT "
        "supports:\n" + "\n".join(
            "  %s:%s  %s on %s" % o for o in offenders
        ) + "\n\nWrap the column in ::text inside the guard — e.g. "
            "NULLIF(col::text, '') — which is correct on both types. Comparing "
            "a timestamptz to '' raises `invalid input syntax for type "
            "timestamp with time zone: \"\"`."
    )


# ---------------------------------------------------------------------------
# The consequence nobody sees until the ALTER actually runs: psycopg2 returns a
# TEXT column as `str` and a timestamptz as a `datetime`. Flask's jsonify
# renders a datetime as RFC 1123, not ISO-8601:
#
#     before   "published_at": "2026-08-24T06:42:40"
#     after    "published_at": "Mon, 24 Aug 2026 06:42:40 GMT"     <- observed
#
# Measured on production 2026-08-24 immediately after tranche B: /api/agent/news,
# /api/news-feed and /api/v1/news (all three are one function, get_agent_news)
# were emitting RFC 1123. /api/news/live was NOT affected — it already called
# .isoformat() on the value, which is exactly the guard the others were missing.
#
# This matters more here than it would elsewhere: these are the agent-facing
# feeds, and RFC 1123 is not a format an ISO-8601 parser accepts.
# ---------------------------------------------------------------------------

def _deals_module(rel):
    spec = importlib.util.spec_from_file_location(
        "deals_under_test_" + rel.replace("/", "_").replace(".", "_"),
        os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rel", ["routes/deals_routes.py", "deals_routes.py"])
def test_iso_renders_a_datetime_as_iso8601_never_rfc1123(rel):
    import datetime as _dt
    iso = _deals_module(rel)._iso
    aware = _dt.datetime(2026, 8, 24, 6, 42, 40, tzinfo=_dt.timezone.utc)
    out = iso(aware)
    assert out == "2026-08-24T06:42:40+00:00"
    assert "GMT" not in out, "RFC 1123 leaked through — agents parse ISO-8601"
    # str in -> str out, so it is a no-op while a column is still TEXT
    assert iso("2026-08-24T06:42:40") == "2026-08-24T06:42:40"
    assert iso(None) is None


@pytest.mark.parametrize("rel", ["routes/deals_routes.py", "deals_routes.py"])
def test_agent_news_never_hands_jsonify_a_raw_timestamp(rel):
    """★REGRESSION. `'published_at': row['published_at']` is the exact line that
    started emitting RFC 1123 the moment the column became timestamptz."""
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        src = fh.read()
    assert "'published_at': row['published_at']," not in src, (
        "%s passes a raw timestamp straight to jsonify — wrap it in _iso(), or "
        "Flask renders it as RFC 1123 once the column is timestamptz" % rel)
    assert "'published_at': _iso(row['published_at'])," in src
