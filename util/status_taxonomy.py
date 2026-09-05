"""status_taxonomy.py — canonical facility status → lifecycle bucket.

r-status-taxonomy (2026-07-29). Single source of truth for the question
"is this megawatt ENERGIZED, or is it a press release?". Replaces the
mixed-case 5-value literal that was hand-copied into every consumer:

    routes/dcpi.py  (US footprint query)     ← governs the DCPI saturation index
    routes/dcpi.py  (intl footprint query)
    routes/dcpi.py::lite_recompute
    main.py::_v216_dcpi_lite_recompute
    main.py         (/api/v1/markets pipeline_mw_total)
    routes/dynamic_hero.py, routes/operator_brief.py,
    routes/ai_capacity_daily.py, routes/intelligence_routes.py
        — seven mutually incompatible vocabularies, none shared.

A second hand-copy of this table is the bug class fixed in
util/iso_taxonomy the same week. Import from here; do not re-inline.

WHY AN ALLOW-LIST, NEVER A DENY-LIST
------------------------------------
The old filter was a deny-list by construction: pipeline was five named
strings and *everything else* — including NULL, '' and every status nobody
had thought of — silently became "operational". Measured against live data
on 2026-07-29 that put 18,118 MW of `Announced` / `Under Development` /
`Approved` capacity inside a figure published as operational, and three of
its five strings ('construction', 'planned', 'permitting' — lowercase)
matched ZERO rows, so the list looked like it handled five cases while
handling two.

The vocabulary also drifts between reads and differs BETWEEN TABLES:
`facilities` carries 'announced', 'planned', 'under_construction' and
'Planning', which `discovered_facilities` does not. A deny-list is wrong
the moment a producer invents a value. An allow-list plus an explicit
UNCLASSIFIED bucket is wrong only loudly.

So there are THREE buckets, not two. Anything unrecognised lands in
UNCLASSIFIED and must be surfaced, never folded into operational.

OBSERVED VALUES (live, 2026-07-29)
----------------------------------
discovered_facilities: Operational 11,055 · active 10,410 · Announced 758 ·
  Under Construction 154 · Planned 91 · operational 12 · Under Development 2 ·
  Expanding 1 · Approved 1
facilities (adds): announced · planned · under_construction · Planning

Every one of those is covered below, so UNCLASSIFIED reads 0 on both tables
today. If it ever reads non-zero, a producer added a value — add it here,
do not widen a call site.

JUDGEMENT CALLS, STATED RATHER THAN IMPLIED
-------------------------------------------
  'active'     → OPERATIONAL. It is the single largest count bucket
                 (46% of discovered_facilities) and carries ~0 MW, so it
                 moves facility counts, not capacity. An active data centre
                 is a running data centre.
  'Expanding'  → OPERATIONAL. Built and in service; the expansion itself is
                 a separate pipeline row when it is tracked at all.
  'commissioning' → PIPELINE. Energisation testing is not yet deliverable
                 load to a tenant.
"""

from __future__ import annotations

# Bump when a value moves between buckets, so a stored data_basis row from
# before the change is distinguishable from one after it.
STATUS_TAXONOMY_VERSION = "2026-07-29"

# Normalised (LOWER + TRIM) forms only. Both the ' ' and '_' spellings are
# listed rather than normalised in SQL — one REPLACE() less on a hot query,
# and the set stays greppable.
OPERATIONAL_STATUSES = frozenset({
    "operational", "operating", "active", "live", "running",
    "in-service", "in service", "in_service", "expanding",
})

PIPELINE_STATUSES = frozenset({
    "announced", "planned", "planning", "approved", "proposed",
    "permitting", "construction", "under construction",
    "under-construction", "under_construction", "under development",
    "under-development", "under_development", "in development",
    "development", "pre-construction", "pre-planning", "commissioning",
})

# A status in both buckets would make op_mw and pipe_mw overlap again —
# exactly the double-count this module exists to kill. Pinned by
# tests/test_dcpi_status_taxonomy.py rather than a module-level assert: an
# assert here would turn a typo into an ImportError, i.e. a dead blueprint.
OVERLAP = OPERATIONAL_STATUSES & PIPELINE_STATUSES

OPERATIONAL = "operational"
PIPELINE = "pipeline"
UNCLASSIFIED = "unclassified"


def normalize(status: str | None) -> str:
    """The single normalisation the SQL below also applies: LOWER + TRIM."""
    return (status or "").strip().lower()


def classify(status: str | None) -> str:
    """'operational' | 'pipeline' | 'unclassified'.

    NULL and '' are UNCLASSIFIED, never operational. That default is the
    whole point: an unknown status is unknown, not built.
    """
    s = normalize(status)
    if s in OPERATIONAL_STATUSES:
        return OPERATIONAL
    if s in PIPELINE_STATUSES:
        return PIPELINE
    return UNCLASSIFIED


def _sql_literal_list(values) -> str:
    """`'a','b',…` — sorted for a stable, diffable query string."""
    out = []
    for v in sorted(values):
        if "'" in v or "\\" in v or "%" in v:
            # Cannot happen with the constants above; refuse rather than
            # emit a query that psycopg2 would mis-parse (a literal % in a
            # query string is the 500-error trap in util/provenance).
            raise ValueError(f"unsafe status literal: {v!r}")
        out.append(f"'{v}'")
    return ",".join(out)


def norm_expr(col: str = "status") -> str:
    """The SQL form of normalize(), for callers matching against their own
    values rather than one of the buckets below.

    A caller comparing user input to `status` MUST wrap the column in this;
    the live vocabulary is Title-Case ('Operational', 'Under Construction')
    and a bare `status IN (...)` of lowercase literals silently matches
    almost nothing. That is the bug this module exists to prevent, and it
    recurred in reveal_endpoints.py (12 of 23,315 rows matched).
    """
    return f"LOWER(TRIM(COALESCE({col},'')))"


# Retained: the bucket helpers below were written against the private name.
_norm_expr = norm_expr


def operational_sql(col: str = "status") -> str:
    """SQL predicate: this row's capacity is IN SERVICE today."""
    return f"{_norm_expr(col)} IN ({_sql_literal_list(OPERATIONAL_STATUSES)})"


def pipeline_sql(col: str = "status") -> str:
    """SQL predicate: announced/permitted/building — NOT yet deliverable.

    Disjoint from operational_sql() by construction, so SUM(...) FILTER over
    the two predicates never counts the same megawatt twice.
    """
    return f"{_norm_expr(col)} IN ({_sql_literal_list(PIPELINE_STATUSES)})"


def unclassified_sql(col: str = "status") -> str:
    """SQL predicate: neither bucket — surface it, do not assume built."""
    return (f"{_norm_expr(col)} NOT IN ("
            f"{_sql_literal_list(OPERATIONAL_STATUSES | PIPELINE_STATUSES)})")


def basis(scope: str = "") -> dict:
    """Auditable stamp for any payload republishing a bucketed MW figure.

    Same house pattern as capacity_basis / signal_tier / reserve_margin_basis:
    a number is only honest if the reader can see which rows produced it.
    """
    out = {
        "taxonomy_version": STATUS_TAXONOMY_VERSION,
        "operational_statuses": sorted(OPERATIONAL_STATUSES),
        "pipeline_statuses": sorted(PIPELINE_STATUSES),
        "match": "LOWER(TRIM(COALESCE(status,'')))",
        "unclassified_policy": "counted separately — never added to operational",
    }
    if scope:
        out["scope"] = scope
    return out


#: What a NULL / blank status is called once it reaches a JSON key.
UNKNOWN_STATUS_KEY = "unknown"


def status_histogram(rows):
    """`{status: count}` from GROUP BY status rows, safe to serialize.

    r-status-null-key (2026-09-05). discovered_facilities.status is nullable,
    and callers built this histogram as `{r[0]: r[1] for r in rows}` — which
    puts a literal None among the KEYS. flask.jsonify sorts keys, so a single
    NULL row turned the whole 200 into

        TypeError: '<' not supported between instances of 'NoneType' and 'str'

    a 500 the CF worker renders to the caller as "503 Backend unreachable".
    Live 2026-09-05, /api/v1/markets/<id>: northern virginia, london-gb and
    singapore-sg all 503'd on this while ashburn, phoenix and tokyo-jp were
    fine — the split is exactly which markets contain a NULL-status facility,
    so the fault appears as ingest lands one and heals when it is backfilled.

    Lives here, pure, so a test can EXECUTE it against flask.jsonify and
    watch the real failure. A source-level guard for this is worthless: the
    broken and the fixed line differ by a conditional, and nothing about the
    text of either says whether Flask can sort the result.

    ACCUMULATES rather than assigns: NULL and '' are distinct GROUP BY
    buckets that both land on UNKNOWN_STATUS_KEY, so a plain assignment lets
    the second silently overwrite the first and the histogram under-reports.
    """
    out = {}
    for r in (rows or ()):
        key = str(r[0]) if r[0] else UNKNOWN_STATUS_KEY
        out[key] = out.get(key, 0) + (r[1] or 0)
    return out
