"""capacity_pipeline.py — canonical quarantine guard for `capacity_pipeline`.

Single source of truth for "is this pipeline row publishable?". The
predicate itself is one line; this module exists because the *claim* that
it was applied everywhere was false for eleven months of served surfaces.

THE CLAIM THAT WASN'T TRUE
--------------------------
main.py carried, since the 2026-07-27 pipeline-GW audit:

    ★★2026-07-27 pipeline-GW audit — every capacity_pipeline read now
    excludes quarantined rows [...] Never drop the data_flag guard from
    a published figure.

"every read" was one read — the `curated_pipeline_*` block it sat in.
Measured against the Neon read replica on 2026-07-31:

    SUM(capacity_mw) unfiltered                       = 2,680.6 GW
    SUM(capacity_mw) WHERE COALESCE(data_flag,'')=''  =   586.6 GW   (4.6x)

725 of 1,973 rows carry a quarantine flag stamped by
repair_capacity_pipeline_quarantine.py: 392 quarantine_unparsed, 160
quarantine_aggregate, 102 quarantine_duplicate, 71 quarantine_not_pipeline.
The `quarantine_aggregate` rows are the expensive ones — utility
interconnection-request QUEUES (AEP 63,000 MW, Dominion 48,000, PPL 25,200)
summed as if each were a single building, plus impossible singles like
Google Nevada 150,000 MW stamped 'operational'.

Fourteen live served reads published the unfiltered number, including the
unauthenticated CSV export, /api/rankings/construction, and the `ai_query`
suggested_response string that hands AI agents a sentence to cite.

WHY A MODULE AND NOT A COPIED LINE
-----------------------------------
This is the third table to need it (`deals` inlines `_LIVE_DEALS` in
routes/graph_spine_master_shell.py, `_live` in routes/infra_growth.py,
and again in routes/deals_routes.py — three hand-copies of one predicate).
The 07-27 audit failed precisely because the guard lived as a local
variable inside the one function that used it, so nothing else could
import it and nothing detected that they hadn't. Import from here; do not
re-inline. tests/test_capacity_pipeline_guard.py fails the build if a
served read reintroduces a bare `FROM capacity_pipeline`.

★ THE STRICT `= ''` FORM, NOT `deals`' PREFIX TEST
`deals` uses `LEFT(data_flag,11) <> 'quarantine_'` because it carries a
legitimate non-quarantine flag (cumulative_capex). capacity_pipeline does
not, and repair_capacity_pipeline_quarantine.py prescribes this exact
predicate for its consumers. Matching routes/brain_rag.py's registry gate
(#2064) byte-for-byte is what stops the two from drifting apart again, and
it fails CLOSED: a future flag vocabulary drops out of published figures
instead of leaking through a prefix that no longer matches.

★ NO LITERAL `%` — DELIBERATE
The predicate is `=`-based, never `LIKE 'quarantine_%'`. Several callers
interpolate this string into a query that ALSO passes a params tuple; a
literal `%` would make psycopg2 attempt substitution on it and 500 the
route. Keep it %-free.

★ THE GUARD IS NOT A ROW FILTER FOR *EVERY* READ
Quarantined rows are KEPT on purpose — the utility queue data is real and
valuable, it is simply a different metric that needs its own surface. Use
this guard on figures we PUBLISH. A maintenance script auditing extraction
quality should read the whole table.
"""

from __future__ import annotations

__all__ = ["CP_OK", "cp_ok"]


#: Canonical predicate. Bare form, for unaliased `FROM capacity_pipeline`.
CP_OK = "COALESCE(data_flag,'') = ''"


def cp_ok(alias: str = "") -> str:
    """Return the guard, optionally qualified by a table alias.

    >>> cp_ok()
    "COALESCE(data_flag,'') = ''"
    >>> cp_ok("cp")
    "COALESCE(cp.data_flag,'') = ''"

    Use the alias form in any query that joins another table carrying its
    own `data_flag` (`deals` does) — unqualified, Postgres resolves the
    column ambiguously or errors, and an errored read in a try/except
    silently becomes a zero.
    """
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}data_flag,'') = ''"
