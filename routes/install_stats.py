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

from mcp_calls_deloop import (
    external_session_predicate,
    internal_tag_regex_predicate,
    real_ua_predicate,
)
from routes.api_usage_tracker import STORED_PREFIX_LEN, TRACKED_KEY_PREFIX

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

# ★★★ THE PROBE IS NOT AN INSTALL (2026-09-20). Measured on production the same
# day: the install-% population was exactly ONE key, client_name
# `install-verify-durability`, minted 2026-09-01, 0 calls, 0 returns — our OWN
# end-to-end mint probe. This endpoint published it as `minted: 1` /
# `clients_tracked: 1`, keyless and public, while the true count of keys any
# human or agent ever claimed from an /install/<client> page was ZERO. The MCP
# server's own workflow comments had already named that key as ours since
# 2026-09-07 (registry-discover.yml and three siblings); only the surface other
# people are asked to cite still counted it.
#
# `install-verify-` is therefore RESERVED: it is where our own probes mint, it is
# never a page slug, and it is excluded from every install figure below and
# reported in its own `probes` block instead — the same discipline the control
# already gets. tests/test_sitemap_lists_install_pages.py holds the page roster
# and asserts no page slug can enter this namespace, so the exclusion cannot
# start swallowing a real install channel.
_PROBE_PREFIX = "install-verify-%"

# Excludes nothing. Used where a query needs the exclusion parameter bound but
# must not exclude anything — reading the probes themselves. It carries NO LIKE
# wildcard, so it can only drop a client_name equal to it character-for-
# character, and nothing mints that name. ★ `_` is a wildcard too (any single
# character): the first draft of this constant was `__never_matches...__` and
# would have quietly excluded every 33-character client_name.
_EXCLUDE_NOTHING = "never-matches-any-client-name"

# ★ ONE exclusion clause, shared TEXTUALLY by the ledger and the windowed count.
# Two hand-written copies of "who is a probe" is how the windowed mint count and
# by_client would come to disagree on the same page; the windowed query aliases
# mcp_dev_keys as `k` for no other reason than to let this one string serve both.
_NOT_A_PROBE = "AND k.metadata->>'client_name' NOT LIKE %s"

# Windows reported. Keep small — each is one indexed scan of a table that is
# tiny by construction (one row per claimed key).
_WINDOWS = (("7d", 7), ("30d", 30))


def _dsn():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or ""
    )


_LEDGER_SQL = f"""
                WITH ik AS (
                    SELECT k.api_key,
                           k.metadata->>'client_name' AS client,
                           k.created_at,
                           k.email IS NOT NULL AND k.email <> '' AS bound,
                           k.tier
                      FROM mcp_dev_keys k
                     WHERE k.metadata->>'client_name' LIKE %s
                       {_NOT_A_PROBE}
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


def _ledger(cur, prefix, exclude=_PROBE_PREFIX):
    """Run the ledger for one client_name prefix, minus `exclude`.

    ★ The control MUST go through this same function. A separately-written
    control query would prove only that the control query works — a bug in the
    real one (wrong metadata key, wrong table, wrong join) would still read as
    "no installs". Same SQL, different bound parameter, or it is not a control.

    The probes block goes through it too, with `exclude=_EXCLUDE_NOTHING`: the
    rows this endpoint refuses to call installs are counted by the SAME SQL that
    counts installs, so "1 probe, 0 calls" cannot be an artefact of a second,
    laxer query written to display it.
    """
    cur.execute(_LEDGER_SQL, (prefix, exclude))
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
            # Our own probes, through the identical SQL, excluding nothing.
            probe_rows = _ledger(cur, _PROBE_PREFIX, _EXCLUDE_NOTHING)
            control_rows = _ledger(cur, _CONTROL_PREFIX)

            # ── windowed mint counts ─────────────────────────────────────────
            windowed = {}
            for label, days in _WINDOWS:
                cur.execute(
                    f"""SELECT COUNT(*) FROM mcp_dev_keys k
                        WHERE k.metadata->>'client_name' LIKE %s
                          {_NOT_A_PROBE}
                          AND k.created_at >= NOW() - (%s || ' days')::interval""",
                    (_INSTALL_PREFIX, _PROBE_PREFIX, str(days)),
                )
                windowed[label] = int(cur.fetchone()[0] or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("install_stats query failed: %s", e)
        return jsonify(ok=False, error="query_failed", detail=str(e)[:200]), 503

    by_client, tot = _summarize(rows)
    probe_clients, probe_tot = _summarize(probe_rows)
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
            ("The ledger returns rows for a non-empty prefix, so the "
             "install-% result above is a real count, not a broken query."
             + (" It is empty: nobody has claimed a key from an install page."
                if tot["minted"] == 0 else ""))
            if _instrument_live else
            "The control is ALSO empty. This endpoint cannot currently tell "
            "'nobody installed' from 'the query matches nothing'. Do not cite "
            "the install figures as evidence of absence until this reads 'live'."
        ),
    }

    probes = {
        "prefix": _PROBE_PREFIX,
        "why": (
            "Our own end-to-end mint probes. They exercise the real POST "
            "/api/v1/keys/claim so this ledger can be shown to count a mint at "
            "all, which is why they live in the install- namespace — and why "
            "they must never be reported as somebody's install."
        ),
        "is_not_an_install": (
            "install-verify-% is RESERVED for us. These rows are excluded from "
            ".totals, .by_client, .clients_tracked and .minted_by_window."
        ),
        "totals": probe_tot,
        "clients": [r["client"] for r in probe_clients],
        "was_counted_as_installs_until": "2026-09-20",
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
                "these are never summed into a single 'installs' figure. "
                "mcp_call_log is NOT MCP-only: it also holds REST bulk-brief "
                "calls and onboarding page events, and it misses most REST use "
                "(the REST tracker does not record dch_live_ keys). For first use "
                "split by channel, read /api/v1/ops/install-stats/first-use."
            ),
            "self_declared": (
                "client_name is supplied by the caller in the claim POST, so it "
                "attests the claimer's stated surface, not verified provenance. "
                "Our own probes are excluded by the reserved install-verify-% "
                "namespace (see .probes); a forged install-<client> from any "
                "other caller would still land in these counts."
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
        probes=probes,
        by_client=by_client,
        clients_tracked=len(by_client),
        control=control,
    )
    # Short cache: this is a low-cardinality ledger, and a stale-by-60s read is
    # honest. ★ A new /api/v1/* path needs a Cloudflare bypass/short-TTL rule or
    # CF Rule #3 (mode: override_origin) serves it stale regardless of this header.
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


# ══════════════════════════════════════════════════════════════════════════
# FIRST USE, BY CHANNEL — GET /api/v1/ops/install-stats/first-use
# ══════════════════════════════════════════════════════════════════════════
#
# ★★★ WHY (2026-09-21). The ledger above reports its control `web-%` as
# 164 minted -> 1 called -> 0 returned, and that was handed on as the
# mint->first-call cliff. But `called` there is "any mcp_call_log row", and the
# web surfaces that mint web-% keys (mostly web-map) send them on REST —
# map.html as `Authorization: Bearer`, js/map.js as `X-API-Key`. So "1 called"
# could mean "1 ever made an MCP call", not "1 ever used the key". This block
# measures first use per key as MCP ∪ REST, from every table that records a
# keyed call, and says which of those tables can see these keys at all.
#
# WHAT RECORDS A KEYED CALL, AND FOR WHICH KEYS (read from the writers):
#   mcp_call_log      full api_key, per-call timestamp. Written by the MCP
#                     server's /api/v1/mcp/track callback (event_type from
#                     status: tool_call, tool_error, paywall_block,
#                     trial_preview; NULL before r47), AND by two REST writers:
#                     routes/market_brief.py logs every /api/v1/market-brief bulk
#                     call as event_type 'bulk:<tier>' with the raw X-API-Key,
#                     and routes/onboarding_page.py logs 'key_first_use' (a keyed
#                     POST from the onboarding test button) and 'key_issued' (a
#                     page view — the page was used, not the key).
#   api_endpoint_log  per call, but keyed by the first STORED_PREFIX_LEN chars
#                     of X-API-Key, and only when that key starts with
#                     TRACKED_KEY_PREFIX (routes/api_usage_tracker.py). Every
#                     self-serve key from /api/v1/keys/claim is `dch_live_…`, so
#                     this tracker never records one — nor any `Authorization:
#                     Bearer` key, whatever its shape.
#   api_usage_meter   day grain. The same tracker writes the same prefix;
#                     POST /track-usage writes a full key but has no caller.
#   api_usage(_daily) keyed by api_keys.id — a dch_live_ key has no api_keys row,
#                     so these cannot hold one. Not read.
#
# So the REST side is measured only through two narrow slices (bulk briefs,
# onboarding test), and the endpoint says so from data: .instrument cross-checks
# every keyed REST request that mcp_call_log DID record against the tracker's
# tables, which should hold the same request if they could see the key.

# mcp_call_log.event_type -> channel. Anything else is `unclassified`: counted
# as use on NEITHER channel and published, so a new writer shows up as a number
# instead of silently landing on one side.
_MCP_EVENT_TYPES = ("tool_call", "tool_error", "paywall_block", "trial_preview")
_REST_EVENT_PATTERN = "bulk:%"          # market_brief bulk; bound, never inlined
_REST_EVENT_TYPES = ("key_first_use",)  # onboarding test button (keyed POST)
_NOT_USE_EVENT_TYPES = ("key_issued",)  # onboarding page view

# A REST row this recent may not be flushed to the tracker's tables yet (the
# flusher runs every USAGE_FLUSH_INTERVAL_SEC, 30s by default), so the
# cross-check only uses rows older than this.
_FLUSH_GRACE_MIN = 10

# ★ Probe ROWS, not just probe keys. The shared predicates the funnels already
# apply to mcp_call_log (routes/funnel_health.py) and to mcp_tool_calls
# (mcp_calls_deloop.is_real_external): scripting/internal user-agents, operator
# self-traffic sessions, internal/QA platform tags. Regex forms, so they carry no
# literal % and are safe beside bound parameters.
_ROW_NOT_A_PROBE = (
    "(" + real_ua_predicate("l.user_agent")
    + " AND " + external_session_predicate("l.session_id")
    + " AND " + internal_tag_regex_predicate("l.platform") + ")"
)

_FIRST_USE_SQL = f"""
                WITH pop AS (
                    SELECT k.api_key,
                           k.metadata->>'client_name' AS client,
                           k.created_at,
                           (starts_with(k.api_key, %s)
                            AND LENGTH(k.api_key) >= %s)     AS tracker_sees_format
                      FROM mcp_dev_keys k
                     WHERE k.metadata->>'client_name' LIKE %s
                       {_NOT_A_PROBE}
                ),
                calls AS (
                    SELECT l.api_key,
                           l.timestamp AS at,
                           CASE WHEN l.event_type IS NULL
                                  OR l.event_type = ANY(%s)  THEN 'mcp'
                                WHEN l.event_type LIKE %s
                                  OR l.event_type = ANY(%s)  THEN 'rest'
                                WHEN l.event_type = ANY(%s)  THEN 'not_use'
                                ELSE 'unclassified'
                           END AS channel,
                           {_ROW_NOT_A_PROBE} AS not_probe
                      FROM mcp_call_log l
                      JOIN pop ON pop.api_key = l.api_key
                ),
                per_key AS (
                    SELECT api_key,
                           MIN(at) FILTER (WHERE channel = 'mcp'  AND not_probe) AS mcp_first,
                           MIN(at) FILTER (WHERE channel = 'rest' AND not_probe) AS rest_first,
                           MIN(at) FILTER (WHERE channel = 'mcp')                AS mcp_first_any_row,
                           MIN(at) FILTER (WHERE channel = 'rest')               AS rest_first_any_row,
                           MIN(at) FILTER (WHERE channel = 'rest'
                                AND at < NOW() - make_interval(mins => %s))      AS rest_settled_first,
                           COUNT(*) FILTER (WHERE channel = 'not_use')           AS not_use_rows,
                           COUNT(*) FILTER (WHERE channel = 'unclassified')      AS unclassified_rows
                      FROM calls
                     GROUP BY api_key
                ),
                endpoint_log AS (
                    SELECT pop.api_key, MIN(e.called_at) AS first_at
                      FROM pop
                      JOIN api_endpoint_log e ON e.api_key_prefix = LEFT(pop.api_key, %s)
                     GROUP BY pop.api_key
                ),
                meter AS (
                    SELECT pop.api_key, MIN(m.usage_date) AS first_day
                      FROM pop
                      JOIN api_usage_meter m
                        ON m.api_key IN (pop.api_key, LEFT(pop.api_key, %s))
                     GROUP BY pop.api_key
                )
                SELECT pop.client,
                       pop.created_at,
                       pop.tracker_sees_format,
                       pk.mcp_first,
                       pk.rest_first,
                       el.first_at,
                       mt.first_day,
                       pk.mcp_first_any_row,
                       pk.rest_first_any_row,
                       pk.rest_settled_first,
                       COALESCE(pk.not_use_rows, 0),
                       COALESCE(pk.unclassified_rows, 0)
                  FROM pop
                  LEFT JOIN per_key pk      ON pk.api_key = pop.api_key
                  LEFT JOIN endpoint_log el ON el.api_key = pop.api_key
                  LEFT JOIN meter mt        ON mt.api_key = pop.api_key
                 ORDER BY pop.created_at
"""


def _first_use_params(prefix, exclude):
    """Bound parameters for _FIRST_USE_SQL, in the order its %s appear."""
    return (
        TRACKED_KEY_PREFIX, STORED_PREFIX_LEN,           # pop.tracker_sees_format
        prefix, exclude,                                 # population, minus probes
        list(_MCP_EVENT_TYPES),                          # channel: mcp
        _REST_EVENT_PATTERN, list(_REST_EVENT_TYPES),    # channel: rest
        list(_NOT_USE_EVENT_TYPES),                      # channel: not_use
        _FLUSH_GRACE_MIN,                                # rest_settled_first
        STORED_PREFIX_LEN,                               # api_endpoint_log join
        STORED_PREFIX_LEN,                               # api_usage_meter join
    )


def _first_use(cur, prefix, exclude=_PROBE_PREFIX):
    """Per-key first use for one client_name prefix, minus `exclude`.

    ★ Same discipline as _ledger(): the measured populations AND the probe
    control go through this one function and this one SQL, differing only in
    bound parameters. Returns one row per key and never the key itself."""
    cur.execute(_FIRST_USE_SQL, _first_use_params(prefix, exclude))
    return cur.fetchall()


def _aware(ts):
    """Timestamps here are UTC. A column created as plain TIMESTAMP comes back
    naive, and naive minus aware raises — which would 503 the whole route."""
    if isinstance(ts, datetime.datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _utc_date(ts):
    return ts.astimezone(datetime.timezone.utc).date()


def _earliest(*ts):
    vals = [t for t in ts if t is not None]
    return min(vals) if vals else None


def _key_record(row):
    """One SQL row -> one key's first-use facts. Probe rows are already out of
    mcp_first / rest_first; the *_any_row columns keep them for the instrument
    check and for the exclusion count, and are never read as use."""
    (client, minted_at, tracker_sees_format, mcp_first, rest_first,
     log_first, meter_day, mcp_any_row, rest_any_row, rest_settled,
     not_use_rows, unclassified_rows) = row
    minted_at, mcp_first, rest_first, log_first = (
        _aware(t) for t in (minted_at, mcp_first, rest_first, log_first))
    rest_at = _earliest(rest_first, log_first)
    first_call = _earliest(mcp_first, rest_at)
    minted_day = _utc_date(minted_at)
    if meter_day is not None and (first_call is None
                                  or meter_day < _utc_date(first_call)):
        grain, hours, days = "day", None, (meter_day - minted_day).days
    elif first_call is not None:
        grain = "call"
        hours = (first_call - minted_at).total_seconds() / 3600.0
        days = (_utc_date(first_call) - minted_day).days
    else:
        grain, hours, days = None, None, None
    return {
        "client": client,
        "minted_at": minted_at,
        "mcp": mcp_first is not None,
        "rest": rest_at is not None or meter_day is not None,
        "grain": grain, "hours": hours, "days": days,
        "tracker_sees_format": bool(tracker_sees_format),
        "in_endpoint_log": log_first is not None,
        "in_meter": meter_day is not None,
        "rest_in_call_log": rest_first is not None,
        "mcp_any_row": mcp_any_row is not None,
        "rest_any_row": rest_any_row is not None,
        "rest_settled": rest_settled is not None,
        "not_use_rows": int(not_use_rows or 0),
        "unclassified_rows": int(unclassified_rows or 0),
    }


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _window_summary(recs):
    """Channel split + time to first use over one mint cohort."""
    used = [r for r in recs if r["mcp"] or r["rest"]]
    hours = [r["hours"] for r in used if r["grain"] == "call"]
    days = [r["days"] for r in used]
    return {
        "minted":        len(recs),
        "first_use_any": len(used),
        "mcp_only":      sum(1 for r in recs if r["mcp"] and not r["rest"]),
        "rest_only":     sum(1 for r in recs if r["rest"] and not r["mcp"]),
        "both":          sum(1 for r in recs if r["mcp"] and r["rest"]),
        "never_used":    len(recs) - len(used),
        "time_to_first_use": {
            "keys": len(used),
            "hours_call_grain": {
                "keys":   len(hours),
                "median": None if not hours else round(_median(hours), 2),
                "max":    None if not hours else round(max(hours), 2),
            },
            "days": {
                "keys":   len(days),
                "median": _median(days),
                "same_day":    sum(1 for d in days if d <= 0),
                "1_to_6":      sum(1 for d in days if 1 <= d <= 6),
                "7_to_29":     sum(1 for d in days if 7 <= d <= 29),
                "30_plus":     sum(1 for d in days if d >= 30),
            },
            "day_grain_only_keys": sum(1 for r in used if r["grain"] == "day"),
            "first_use_before_mint": sum(1 for d in days if d < 0),
        },
    }


def _population(prefix, recs, now):
    """Windows are MINT cohorts: keys minted in the last N days, and whether
    each has been used at all since. A 7d cohort is right-censored — its keys
    have had at most 7 days to be used."""
    windows = {}
    for label, days in _FIRST_USE_WINDOWS:
        cut = None if days is None else now - datetime.timedelta(days=days)
        windows[label] = _window_summary(
            [r for r in recs if cut is None or r["minted_at"] >= cut])
    by_client = {}
    for r in recs:
        c = by_client.setdefault(r["client"], {
            "client": r["client"], "minted": 0, "first_use_any": 0,
            "mcp": 0, "rest": 0})
        c["minted"] += 1
        c["first_use_any"] += int(r["mcp"] or r["rest"])
        c["mcp"] += int(r["mcp"])
        c["rest"] += int(r["rest"])
    return {
        "prefix": prefix,
        "label": "probe-excluded",
        "windows": windows,
        "by_client_all_time": sorted(
            by_client.values(), key=lambda c: (-c["minted"], c["client"] or "")),
        "rest_visibility": {
            "keys": len(recs),
            "keys_in_a_format_the_rest_tracker_records": sum(
                1 for r in recs if r["tracker_sees_format"]),
            "keys_seen_in_api_endpoint_log": sum(1 for r in recs if r["in_endpoint_log"]),
            "keys_seen_in_api_usage_meter": sum(1 for r in recs if r["in_meter"]),
            "keys_with_rest_rows_in_mcp_call_log": sum(
                1 for r in recs if r["rest_in_call_log"]),
        },
        "excluded_by_row_rules": {
            "keys_whose_only_mcp_rows_were_probe_rows": sum(
                1 for r in recs if r["mcp_any_row"] and not r["mcp"]),
            "keys_whose_only_rest_rows_were_probe_rows": sum(
                1 for r in recs if r["rest_any_row"] and not r["rest_in_call_log"]),
        },
        "mcp_call_log_rows_not_counted_as_use": {
            "not_use_rows": sum(r["not_use_rows"] for r in recs),
            "unclassified_rows": sum(r["unclassified_rows"] for r in recs),
        },
    }


def _instrument(all_recs, control_recs):
    """Per-arm verdict, DERIVED from what the same SQL returned.

    For the two tables the REST tracker writes, the cross-check decides first:
    a keyed REST request mcp_call_log recorded (older than the flush grace)
    reached the origin, so the tracker's table should hold that key too.
    blind    it holds NONE of the keys known to have made a REST request
    partial  it holds some of them
    live     it holds all of them — or, with no such key to test, rows exist
             for some key, so a zero elsewhere is at least a count
    unproven nothing to test against and nothing held
    ★ Rows for OTHER keys never rescue a failed cross-check: the tracker is
    live for dchub_ keys and still blind to every dch_live_ one.
    """
    known = [r for r in all_recs if r["rest_settled"]]

    def _tracker_arm(field):
        seen = sum(1 for r in known if r[field])
        held = sum(1 for r in all_recs if r[field])
        if known:
            verdict = ("blind" if seen == 0 else
                       "partial" if seen < len(known) else "live")
        else:
            verdict = "live" if held else "unproven"
        return {"verdict": verdict,
                "keys_known_to_have_made_a_rest_request": len(known),
                "of_those_seen_here": seen,
                "keys_with_rows_here": held}

    mcp_live = any(r["mcp_any_row"] for r in all_recs)
    rest_live = any(r["rest_any_row"] for r in all_recs)
    return {
        "mcp_call_log.mcp": {
            "verdict": "live" if mcp_live else "unproven",
            "control_keys_with_mcp_rows": sum(1 for r in control_recs if r["mcp_any_row"]),
        },
        "mcp_call_log.rest": {
            "verdict": "live" if rest_live else "unproven",
            "control_keys_with_rest_rows": sum(1 for r in control_recs if r["rest_any_row"]),
        },
        "api_endpoint_log": _tracker_arm("in_endpoint_log"),
        "api_usage_meter": _tracker_arm("in_meter"),
        "keys_in_a_format_the_rest_tracker_records": sum(
            1 for r in all_recs if r["tracker_sees_format"]),
        "keys_checked": len(all_recs),
    }


# (label, days). None = all time.
_FIRST_USE_WINDOWS = (("7d", 7), ("30d", 30), ("all_time", None))
_FIRST_USE_POPULATIONS = (_CONTROL_PREFIX, _INSTALL_PREFIX)


def _population_reading(pop, inst):
    """One derived sentence per population. It states what was measured and
    what could not be; it never names a channel dead."""
    w = pop["windows"]["all_time"]
    rest_seen = inst["api_endpoint_log"]["verdict"] == "live"
    head = (
        "Probe-excluded, all time: %d minted, %d used at all — %d MCP only, "
        "%d REST only, %d both." % (w["minted"], w["first_use_any"],
                                    w["mcp_only"], w["rest_only"], w["both"]))
    if rest_seen:
        return head + " The per-call REST tracker sees keys of this kind, so the REST figures are counts."
    return head + (
        " REST figures here are a FLOOR: the per-call REST tracker is %s for these "
        "keys, so REST use is visible only on the bulk-brief and onboarding-test "
        "slice of mcp_call_log. A REST zero is not evidence that REST is unused."
        % inst["api_endpoint_log"]["verdict"])


@install_stats_bp.route("/api/v1/ops/install-stats/first-use", methods=["GET"])
def install_first_use():
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            runs = {p: [_key_record(r) for r in _first_use(cur, p)]
                    for p in _FIRST_USE_POPULATIONS}
            # Our own probes, through the identical SQL, excluding nothing.
            control = [_key_record(r)
                       for r in _first_use(cur, _PROBE_PREFIX, _EXCLUDE_NOTHING)]
    except Exception as e:  # noqa: BLE001
        log.warning("install_first_use query failed: %s", e)
        return jsonify(ok=False, error="query_failed", detail=str(e)[:200]), 503

    inst = _instrument([r for recs in runs.values() for r in recs] + control, control)
    populations = {p: _population(p, recs, now) for p, recs in runs.items()}
    for p in populations.values():
        p["reading"] = _population_reading(p, inst)
    tracker_blind = inst["api_endpoint_log"]["verdict"] == "blind"

    resp = jsonify(
        ok=True,
        generated_at=now.isoformat(),
        label="probe-excluded",
        basis={
            "population": (
                "distinct api_keys in mcp_dev_keys whose metadata->>'client_name' "
                "matches each prefix in .populations, minus the reserved "
                "install-verify-% probe namespace. The key is the unit; sessions "
                "and IPs are never counted."
            ),
            "windows": (
                "MINT cohorts: keys minted in the last 7 / 30 days, or ever, and "
                "whether each has been used since. Short cohorts are "
                "right-censored — a key minted yesterday has had one day."
            ),
            "channels": {
                "mcp": (
                    "mcp_call_log rows written by the MCP server's "
                    "/api/v1/mcp/track callback: event_type in "
                    + ", ".join(_MCP_EVENT_TYPES) + ", or NULL (before r47)."
                ),
                "rest": (
                    "keyed REST requests: mcp_call_log rows the REST writers log "
                    "(event_type 'bulk:<tier>' from /api/v1/market-brief bulk; "
                    "'key_first_use' from the onboarding test button), plus "
                    "api_endpoint_log (per call) and api_usage_meter (per day) "
                    "joined on the first " + str(STORED_PREFIX_LEN) + " chars of "
                    "the key."
                ),
                "not_use": (
                    "event_type 'key_issued' is an onboarding page view — the "
                    "page was used, not the key. Counted in "
                    ".mcp_call_log_rows_not_counted_as_use, never as use."
                ),
                "unclassified": (
                    "any other event_type counts as use on neither channel and is "
                    "published, so a new writer surfaces as a number."
                ),
            },
            "rest_coverage": (
                "routes/api_usage_tracker.py writes api_endpoint_log and "
                "api_usage_meter only for an X-API-Key starting '"
                + TRACKED_KEY_PREFIX + "'. Self-serve keys from "
                "/api/v1/keys/claim are dch_live_ keys, and web-map sends its key "
                "on REST, so most REST use by these populations reaches no per-key "
                "table. .instrument measures this instead of assuming it."
            ),
            "grain": (
                "hours_call_grain is computed only where a per-call timestamp "
                "(mcp_call_log, api_endpoint_log) supplies the first use. "
                "api_usage_meter knows only the UTC day, so a first use seen only "
                "there is in days: .days covers every used key, "
                ".day_grain_only_keys counts those with no finer timestamp."
            ),
            "probes_excluded": {
                "keys": "client_name matching the reserved install-verify-% namespace (read separately as .control)",
                "mcp_call_log_rows": (
                    "rows failing any shared self-traffic predicate: "
                    "mcp_calls_deloop.real_ua_predicate (scripting/internal "
                    "user-agents, incl. anything naming dchub), "
                    "external_session_predicate (operator self-traffic "
                    "sessions), internal_tag_regex_predicate (internal/QA "
                    "platform tags)."
                ),
                "not_applicable": (
                    "api_endpoint_log and api_usage_meter carry no user-agent, "
                    "session or platform column, so no row rule can run there."
                ),
            },
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
                "note": "Direct counts by the definitions in .basis, probe-excluded.",
            },
            "mcp_channel_is_measured": {
                "status": ("observed" if inst["mcp_call_log.mcp"]["verdict"] == "live"
                           else "hypothesis"),
                "note": (
                    "Stamped observed only while the same SQL returns MCP rows for "
                    "some key; otherwise an MCP zero is uninterpretable."
                ),
            },
            "rest_tracker_cannot_see_these_keys": {
                "status": "observed" if tracker_blind else "hypothesis",
                "note": (
                    "Observed only while keyed REST requests that mcp_call_log "
                    "recorded are absent from api_endpoint_log — see "
                    ".instrument.api_endpoint_log."
                ),
            },
            "rest_figures_are_a_floor": {
                "status": "observed" if tracker_blind else "hypothesis",
                "note": (
                    "While the tracker arm is not live, REST use is visible only "
                    "on the bulk-brief and onboarding-test slice. rest_only = 0 "
                    "does not mean REST is unused."
                ),
            },
        },
        populations=populations,
        instrument=inst,
        control={
            "prefix": _PROBE_PREFIX,
            "why": (
                "Our own probe keys, read by the IDENTICAL _first_use() SQL with "
                "nothing excluded. A probe key deliberately used on MCP and on "
                "REST is the positive control for each arm: an arm that stays "
                "empty for it cannot see that channel."
            ),
            "keys": len(control),
            "keys_with_mcp_rows": sum(1 for r in control if r["mcp_any_row"]),
            "keys_with_rest_rows_in_mcp_call_log": sum(
                1 for r in control if r["rest_any_row"]),
            "keys_seen_in_api_endpoint_log": sum(
                1 for r in control if r["in_endpoint_log"]),
            "is_not_a_population": "never added to any figure in .populations",
        },
    )
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_install_stats(app):
    try:
        app.register_blueprint(install_stats_bp)
        log.info("install_stats registered (/api/v1/ops/install-stats)")
    except Exception as e:  # noqa: BLE001
        log.warning("install_stats register failed: %s", e)
