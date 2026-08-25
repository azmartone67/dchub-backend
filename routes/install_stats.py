"""Public install ledger — how many humans actually installed a connector.

★ WHY THIS EXISTS (2026-08-25). `/install/{grok,claude,chatgpt,perplexity,cursor}`
has shipped since 08-19 and mints keys as `client_name=install-<client>`, but NO
surface anywhere could answer "has a single human ever installed from those
pages?". Every probe for one 404'd. A funnel you cannot read is a funnel you
cannot fix, and the whole 7-platform partner round closed on exactly that rule,
stated by Perplexity: *a figure should be observable from the machine-readable
surface that asks others to rely on it.* This is that surface for installs.

★ BASIS — score on DISTINCT api_keys, never sessions, never IPs.
Grok rotates its egress IP per request AND opens a fresh MCP session per tool
call, so both inflate ~10x ([[registry_reach_0818]]). The key is the only stable
identity, and `/api/v1/keys/claim` already stores `client_name` in
`mcp_dev_keys.metadata`, so the mint is the countable event with no new writes.

★ MINTED != USED != RETAINED. "Registration is not function" has been re-learned
on this codebase repeatedly (agent-retention shell #49; Smithery's 160 keys were
100% free-tier with 6.9% second-day return). So this endpoint reports all three
separately and never collapses them into one "installs" number.

Public and keyless on purpose, same class as /api/v1/ops/deadman: a claim we ask
other people to repeat has to be checkable without credentials.
"""
import os
import datetime
import logging

import psycopg2
from flask import Blueprint, jsonify

log = logging.getLogger("install_stats")
install_stats_bp = Blueprint("install_stats", __name__)

# The client_name prefix written by the /install/<client> pages. Bound as a
# PARAMETER, never inlined: a literal '%' inside a psycopg2 query that also
# carries params raises "unsupported format character" and 500s the route.
_INSTALL_PREFIX = "install-%"

# ★★★ THE CONTROL (2026-08-25). This endpoint's whole finding is a row of zeros,
# and a zero is only evidence if the same query can return non-zero. The first
# publication reported `minted: 0` with `by_client: []` and NO control, which is
# indistinguishable from a query that cannot return anything — a filter typo, a
# renamed metadata key, an empty table. `web-%` is the client_name prefix the
# web surfaces mint under and it is known non-empty, so running the SAME ledger
# against it proves the instrument works. It is NOT an install channel and is
# never summed into any install figure.
_CONTROL_PREFIX = "web-%"

# Windows reported. Keep small — each is one indexed scan of a table that is
# tiny by construction (one row per claimed key).
_WINDOWS = (("7d", 7), ("30d", 30))


def _dsn():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or ""
    )


_LEDGER_SQL = """
                WITH ik AS (
                    SELECT k.api_key,
                           k.metadata->>'client_name' AS client,
                           k.created_at,
                           k.email IS NOT NULL AND k.email <> '' AS bound,
                           k.tier
                      FROM mcp_dev_keys k
                     WHERE k.metadata->>'client_name' LIKE %s
                ),
                use AS (
                    SELECT l.api_key,
                           COUNT(*)                                   AS calls,
                           COUNT(DISTINCT (l.timestamp AT TIME ZONE 'UTC')::date) AS active_days,
                           MAX(l.timestamp)                           AS last_call
                      FROM mcp_call_log l
                      JOIN ik ON ik.api_key = l.api_key
                     GROUP BY l.api_key
                )
                SELECT ik.client,
                       COUNT(*)                                                AS minted,
                       COUNT(*) FILTER (WHERE use.api_key IS NOT NULL)         AS called,
                       COUNT(*) FILTER (WHERE use.active_days >= 2)            AS returned,
                       COUNT(*) FILTER (WHERE ik.bound)                        AS email_bound,
                       COUNT(*) FILTER (WHERE ik.tier IN ('paid','enterprise')) AS paid,
                       MIN(ik.created_at)                                      AS first_mint,
                       MAX(ik.created_at)                                      AS last_mint,
                       MAX(use.last_call)                                      AS last_call,
                       COALESCE(SUM(use.calls), 0)                             AS total_calls
                  FROM ik
                  LEFT JOIN use ON use.api_key = ik.api_key
                 GROUP BY ik.client
                 ORDER BY minted DESC, ik.client
"""


def _summarize(rows):
    """Rows -> (per-client records, totals). Shared by the ledger AND the
    control so the two cannot diverge in how they count."""
    out, tot = [], {
        "minted": 0, "called": 0, "returned": 0,
        "email_bound": 0, "paid": 0, "total_calls": 0,
    }
    for (client, minted, called, returned, bound, paid,
         first_mint, last_mint, last_call, total_calls) in rows:
        rec = {
            "client":      client,
            "minted":      int(minted or 0),
            "called":      int(called or 0),
            "returned":    int(returned or 0),
            "email_bound": int(bound or 0),
            "paid":        int(paid or 0),
            "total_calls": int(total_calls or 0),
            "first_mint":  first_mint.isoformat() if first_mint else None,
            "last_mint":   last_mint.isoformat() if last_mint else None,
            "last_call":   last_call.isoformat() if last_call else None,
        }
        out.append(rec)
        for k in tot:
            tot[k] += rec[k]
    return out, tot


def _ledger(cur, prefix):
    """Run the ledger for one client_name prefix.

    ★ The control MUST go through this same function. A separately-written
    control query would prove only that the control query works — a bug in the
    real one (wrong metadata key, wrong table, wrong join) would still read as
    "no installs". Same SQL, different bound parameter, or it is not a control.
    """
    cur.execute(_LEDGER_SQL, (prefix,))
    return cur.fetchall()


@install_stats_bp.route("/api/v1/ops/install-stats", methods=["GET"])
def install_stats():
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            # ── per-client ledger ────────────────────────────────────────────
            # minted   : distinct keys claimed by an /install/<client> page
            # called   : those keys that ever appear in mcp_call_log
            # returned : those keys that called on 2+ DISTINCT UTC days.
            #            Retention is a cross-day signal — a key that made 40
            #            calls in one session has not returned, and counting
            #            calls instead of days is how mature key-reuse got
            #            mis-read as 1.7% before (r-durable-key 2026-07-06).
            rows = _ledger(cur, _INSTALL_PREFIX)
            control_rows = _ledger(cur, _CONTROL_PREFIX)

            # ── windowed mint counts ─────────────────────────────────────────
            windowed = {}
            for label, days in _WINDOWS:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_dev_keys
                        WHERE metadata->>'client_name' LIKE %s
                          AND created_at >= NOW() - (%s || ' days')::interval""",
                    (_INSTALL_PREFIX, str(days)),
                )
                windowed[label] = int(cur.fetchone()[0] or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("install_stats query failed: %s", e)
        return jsonify(ok=False, error="query_failed", detail=str(e)[:200]), 503

    by_client, tot = _summarize(rows)
    control_clients, control_tot = _summarize(control_rows)

    # ★ The verdict is derived, never asserted. If the control is also empty we
    # say so and DOWNGRADE the reading — a zero next to an unproven instrument
    # is not evidence of absence, and this endpoint must not imply it is.
    _instrument_live = control_tot["minted"] > 0
    control = {
        "prefix": _CONTROL_PREFIX,
        "why": (
            "A row of zeros is only evidence if the same query can return "
            "non-zero. This runs the IDENTICAL ledger SQL against a prefix that "
            "is known non-empty, via the same _ledger() helper — a separately "
            "written control would prove only itself."
        ),
        "is_not_an_install_channel": (
            "web-% keys are minted by the web surfaces, not by /install/<client>. "
            "They are NEVER added to totals or by_client above."
        ),
        "totals": control_tot,
        "clients_tracked": len(control_clients),
        "instrument": "live" if _instrument_live else "unproven",
        "reading": (
            "The ledger returns rows for a non-empty prefix, so an empty "
            "install-% result is a real zero, not a broken query."
            if _instrument_live else
            "The control is ALSO empty. This endpoint cannot currently tell "
            "'nobody installed' from 'the query matches nothing'. Do not cite "
            "the install figures as evidence of absence until this reads 'live'."
        ),
    }

    resp = jsonify(
        ok=True,
        generated_at=now.isoformat(),
        # ── the basis, published with the numbers ────────────────────────────
        # Same contract the handoff funnel carries: a figure is only citable if
        # the surface says what it counted. See evidence_status below.
        basis={
            "population": (
                "distinct api_keys in mcp_dev_keys whose "
                "metadata->>'client_name' matches 'install-%' — the client_name "
                "the /install/<client> pages send to POST /api/v1/keys/claim"
            ),
            "not_counted": (
                "sessions and IPs. Grok rotates egress IP per request and opens "
                "a fresh MCP session per tool call, inflating both by ~10x; the "
                "key is the only stable identity on this surface."
            ),
            "minted_vs_called": (
                "minted = a key was claimed from an install page. called = that "
                "key appears in mcp_call_log at least once. returned = it called "
                "on 2 or more distinct UTC days. Registration is not function; "
                "these are never summed into a single 'installs' figure."
            ),
            "known_gap": (
                "a human who pastes the keyless connector URL and never clicks "
                "'get a durable free key' is NOT counted here — they arrive "
                "anonymous and are indistinguishable from any other anonymous "
                "caller. This number is a FLOOR on installs, not a total."
            ),
        },
        evidence_status={
            "evidence_status_version": 1,
            "states": {
                "observed":   "We measured this directly.",
                "hypothesis": "Proposed explanation, not experimentally confirmed.",
                "verified":   "An experiment isolated the mechanism.",
            },
            "contract": (
                "Any field carrying a `status` key uses this vocabulary. A value "
                "without a status is UNSTAMPED — treat it as unclassified, never "
                "as observed. Nothing here is promoted automatically."
            ),
        },
        evidence_status_claims={
            "counts": {
                "status": "observed",
                "note": (
                    "Direct counts over mcp_dev_keys and mcp_call_log by the "
                    "definitions in .basis. Counts are measurements; what they "
                    "IMPLY about install-page effectiveness is not."
                ),
            },
            "control_proves_the_instrument": {
                "status": "observed" if _instrument_live else "hypothesis",
                "note": (
                    "The control runs the same _ledger() SQL against "
                    "'" + _CONTROL_PREFIX + "'. Stamped observed only while it "
                    "returns non-zero; if the control empties, this drops to "
                    "hypothesis and .control.reading says the zeros are "
                    "uninterpretable."
                ),
            },
            "installs_is_a_floor": {
                "status": "observed",
                "note": (
                    "Keyless pastes are structurally uncountable — see "
                    ".basis.known_gap. The floor claim is a property of the "
                    "instrument, not an estimate."
                ),
            },
        },
        totals=tot,
        minted_by_window=windowed,
        by_client=by_client,
        clients_tracked=len(by_client),
        control=control,
    )
    # Short cache: this is a low-cardinality ledger, and a stale-by-60s read is
    # honest. ★ A new /api/v1/* path needs a Cloudflare bypass/short-TTL rule or
    # CF Rule #3 (mode: override_origin) serves it stale regardless of this header.
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_install_stats(app):
    try:
        app.register_blueprint(install_stats_bp)
        log.info("install_stats registered (/api/v1/ops/install-stats)")
    except Exception as e:  # noqa: BLE001
        log.warning("install_stats register failed: %s", e)
