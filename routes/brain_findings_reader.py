"""Read-side companion to brain_findings_writer — the ONE place that reads
brain_findings for triage.

Built 2026-08-29. /api/v1/brain/findings/triage merged the IN-PROCESS
`dchub_self_heal` caches (get_last_backend_findings, get_last_radar_findings,
…) and never read the table. Those caches are per-replica and empty on the
web dyno, so triage reported `source_findings: 0` against 4,241 durable rows:
nothing was ever `actionable_now`, so an approval landed nowhere.

The diagnosis was already written down — routes/loop_control_master_shell.py
item 3, "TRIAGE WIRED" — when the table held 3,012 rows. It was correct, it
was committed, and the fix never landed. This module is that fix.

Two disciplines carried over from the writer:

  1. INTROSPECT, NEVER ASSUME. The live table has drifted from the repo DDL
     before (that drift is why the writer exists). This module asks which
     columns are actually present and degrades to what it finds. Unlike the
     writer it never runs DDL — a read path that ALTERs is a read path that
     dies under SKIP_DDL, which db_utils.safe_db() sets by default on
     Railway.

  2. A FAILED READ IS NOT AN EMPTY READ. Returning {} on a dead connection
     is precisely the bug above, one layer down: a failure rendered as a
     benign value. Every function here returns a `basis` describing where
     its numbers came from, and callers are expected to surface it rather
     than publish a confident zero.

COUNT SEMANTICS (shell #49 lane 3, and item 2 of the same master shell):
`count` is per-detector FREE-FORM — some detectors write a tally, some a
duration, some a backlog size. brain_consistency_radar once wrote
`int(seconds_since)`, which made 5.5 days of cron silence read as 477,455
sightings and re-win the agenda every tick. Only `seen_count` is a
recurrence tally (EPISODES, not scan cycles), and `count` is an occurrence
tally only when `count_kind = 'occurrence'`. Triage therefore weighs
seen_count and carries the raw count alongside as context, never as
leverage.
"""
import logging

logger = logging.getLogger(__name__)

# Columns triage wants, in preference order. Every one is optional: the
# live table is whatever information_schema says it is.
_WANTED = ("issue", "url", "detail", "detector", "status",
           "seen_count", "count", "count_kind", "last_seen")

# A row in any of these statuses is not open work. Mirrors the writer's
# explicit-resolve transition set.
_CLOSED_STATUSES = ("resolved", "wont_fix", "dismissed")

# The one count_kind that means "this integer is a tally of sightings".
# Imported rather than redefined when the writer is importable.
try:                                          # pragma: no cover - trivial
    from routes.brain_findings_writer import OCCURRENCE_KIND
except Exception:                             # pragma: no cover - trivial
    OCCURRENCE_KIND = "occurrence"

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


class FindingsReadError(RuntimeError):
    """The read did not happen. Raised so a caller cannot mistake a failure
    for an empty table — the whole point of this module."""


def live_finding_columns(cur) -> set:
    """Which brain_findings columns actually exist right now.

    Read-only introspection: no ALTER, no CREATE. The writer self-heals the
    schema; a reader that tried would just fail closed under SKIP_DDL.
    """
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'brain_findings'")
    return {r[0] for r in cur.fetchall() or []}


def _label_for(issue: str, detail: str) -> str:
    """Compose the label triage_findings() parses.

    routes/brain_error_classes._finding_type_of() takes the token before the
    first ':' as the finding TYPE, and `issue` is exactly that type token
    (e.g. 'cron_schedule_collision'). Appending the detail keeps the type
    parseable while giving the classifier's regex fallback real text to
    match on — several detectors carry the discriminating words in `detail`,
    not in `issue`.
    """
    issue = (issue or "").strip()
    detail = (detail or "").strip()
    if not issue:
        return detail or "unknown"
    return "%s: %s" % (issue, detail) if detail else issue


def _weight_for(row: dict) -> int:
    """The number triage carries as `count`.

    seen_count is the recurrence tally (episodes). The raw `count` column is
    only a sighting tally when count_kind says so; otherwise it is a
    magnitude and must not buy agenda leverage. Defaults to 1 — a finding
    that exists has been seen at least once.
    """
    sc = row.get("seen_count")
    if isinstance(sc, int) and sc > 0:
        return sc
    if (row.get("count_kind") or "") == OCCURRENCE_KIND:
        c = row.get("count")
        if isinstance(c, int) and c > 0:
            return c
    return 1


def open_findings_for_triage(cur, limit: int = DEFAULT_LIMIT):
    """Read OPEN brain_findings rows into the {url: {label: count}} map that
    routes.brain_error_classes.triage_findings() consumes.

    Returns (merged, index, basis):
      merged — {url: {label: weight}}, the classifier's input shape
      index  — {(url, label): row}, so the caller can enrich classified
               entries with detector/status/raw count without a second query
      basis  — what was read, how it was scoped, and what was left out

    Raises FindingsReadError if the table cannot be read. It does NOT return
    an empty map on failure: an empty map is a legitimate answer meaning
    "no open findings", and conflating the two is the bug this module fixes.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    try:
        cols = live_finding_columns(cur)
    except Exception as e:
        raise FindingsReadError("introspection failed: %s" % str(e)[:160])
    if not cols:
        raise FindingsReadError("brain_findings not present in information_schema")
    if "issue" not in cols:
        raise FindingsReadError("brain_findings has no `issue` column")

    select = [c for c in _WANTED if c in cols]

    # Scope to open work using whichever markers this table actually has.
    where = []
    if "resolved_at" in cols:
        where.append("resolved_at IS NULL")
    if "status" in cols:
        placeholders = ", ".join(["%s"] * len(_CLOSED_STATUSES))
        where.append("(status IS NULL OR status NOT IN (%s))" % placeholders)
    params = list(_CLOSED_STATUSES) if "status" in cols else []

    order = []
    if "seen_count" in cols:
        order.append("seen_count DESC NULLS LAST")
    if "last_seen" in cols:
        order.append("last_seen DESC NULLS LAST")
    order.append("issue ASC")

    sql = "SELECT %s FROM brain_findings" % ", ".join('"%s"' % c for c in select)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + ", ".join(order) + " LIMIT %s"
    params.append(limit)

    try:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    except Exception as e:
        raise FindingsReadError("select failed: %s" % str(e)[:160])

    # How many open rows exist in total, so a truncated read says so instead
    # of quietly presenting the top N as the whole picture.
    total_open = None
    try:
        csql = "SELECT COUNT(*) FROM brain_findings"
        if where:
            csql += " WHERE " + " AND ".join(where)
        cur.execute(csql, tuple(params[:-1]))
        got = cur.fetchone()
        if got:
            total_open = int(got[0])
    except Exception:
        total_open = None            # a missing total is not a failed read

    merged: dict = {}
    index: dict = {}
    skipped_no_url = 0
    for r in rows:
        row = dict(zip(select, r))
        url = (row.get("url") or "").strip()
        if not url:
            # triage keys by url; a finding with none would collide under ""
            # and silently overwrite its neighbours.
            skipped_no_url += 1
            url = "(no-url):%s" % (row.get("detector") or "unknown")
        label = _label_for(row.get("issue"), row.get("detail"))
        merged.setdefault(url, {})[label] = _weight_for(row)
        index[(url, label)] = row

    return merged, index, {
        "source": "brain_findings",
        "rows_read": len(rows),
        "labels": sum(len(v) for v in merged.values()),
        "open_rows_total": total_open,
        "truncated": bool(total_open is not None and total_open > len(rows)),
        "limit": limit,
        "scope": " AND ".join(where) if where else "(no open/closed markers on table)",
        "columns_used": select,
        "weight_field": "seen_count (episodes); raw count only when count_kind='%s'"
                        % OCCURRENCE_KIND,
        "findings_without_url": skipped_no_url,
    }
