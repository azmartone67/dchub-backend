"""
agent_index.py — Phase GG (2026-05-14): the AI-agent session warm-up tool.

The user framed it: "make our site more useful to every AI agent... what
can we aggregate to incent them time?" The funnel showed 38K tool calls →
4 conversions — agents do a lot of work but rarely close the loop. This
bundle reduces agent calls-per-session by 30-50% by pre-answering the
discovery questions every agent asks first.

Two endpoints:
    GET /api/v1/agent/index        — one-call session warm-up:
                                     enums, freshness window, radar issues,
                                     coverage inventory, drill-deeper map.
    GET /api/v1/agent/coverage     — negative-space inventory:
                                     "we have X for region Y; we DON'T
                                     have W or Z". Filterable by domain.

Both are pure reads. Every bundle attaches `sources: [...]` provenance so
the calling agent can show evidence to its human user.
"""
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from util.dcpi_score_row import PUBLISHED_ONLY

from util.capacity_pipeline import CP_OK
from util.deals import DEALS_OK

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
    # Defensive fallback so this module imports even if util/ is missing.
    def src(claim, source, observed_at=None, url=None):
        return {"claim": claim, "source": source,
                "observed_at": observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at,
                "url": url}
    def attach_sources(p, s, generated_at=None):
        out = dict(p) if isinstance(p, dict) else {"result": p}
        out["sources"] = [x for x in (s or []) if x]
        out["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
        return out
    def now_iso():
        return datetime.now(timezone.utc).isoformat()

agent_index_bp = Blueprint("agent_index", __name__)


def _conn():
    """A connection whose autocommit actually holds.

    ★ DO NOT wrap the result in `with _conn() as c:`. psycopg2's connection
    context manager is a TRANSACTION manager, not a closer: entering it opens
    an explicit transaction block even when `autocommit` is True. The
    attribute keeps reporting True while the session sits in
    TRANSACTION_STATUS_INTRANS, so the lie is invisible from Python. That is
    exactly how this endpoint published an all-zero coverage inventory for
    months — see the block comment in agent_index(). Use try/finally + close.
    """
    import psycopg2
    # autocommit=True so a single failed query (e.g., a dropped column) doesn't
    # abort the whole transaction and poison every subsequent query. Each
    # _try_fetchall is independent -- but ONLY outside a `with` block.
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _unpoison(cur):
    """Roll back an aborted transaction so the NEXT query can still run.

    Belt-and-braces for the trap above: if anything ever re-opens an explicit
    transaction, one bad query would otherwise take down every later query on
    the same connection with InFailedSqlTransaction -- across cursors, since a
    transaction belongs to the connection, not the cursor. rollback() on an
    idle autocommit connection is a no-op, so this is always safe.
    """
    try:
        cur.connection.rollback()
    except Exception:
        pass


def _try_fetchall(cur, sql, params=()):
    """Run a query. Return (rows, None) on success, ([], "Type: msg") on failure.

    Use this anywhere an empty result would be PUBLISHED as a fact. `[]` and
    "the query blew up" are not the same answer, and a caller that cannot tell
    them apart will happily report a broken read as a confident zero.
    """
    try:
        cur.execute(sql, params)
        return cur.fetchall(), None
    except Exception as e:
        _unpoison(cur)
        return [], f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"


def _safe_fetchall(cur, sql, params=()):
    """Rows or []. Never raises.

    ★ Only for reads where [] is a legitimate published answer. If the value
    ends up in a response body, use _try_fetchall and report the error --
    this helper structurally cannot tell "no rows" from "no database".
    """
    rows, _ = _try_fetchall(cur, sql, params)
    return rows


def _enums(cur):
    """Pull the catalog enums an agent needs to use IDs correctly.

    A failed enum query yields an EMPTY list plus a named entry in
    `enum_errors` -- never a silently short list the agent would read as
    "these are all the valid values".
    """
    out = {}
    errors = {}

    def enum(key, sql, limit=None):
        rows, err = _try_fetchall(cur, sql)
        if err:
            errors[key] = err
            out[key] = []
            return
        vals = sorted([r[0] for r in rows])
        out[key] = vals[:limit] if limit else vals

    enum("iso_codes",
         "SELECT DISTINCT iso FROM market_power_scores WHERE iso IS NOT NULL AND iso <> ''"
         f" AND {PUBLISHED_ONLY}")
    out["dcpi_verdicts"] = ["BUILD", "CAUTION", "AVOID", "LOW_SIGNAL"]
    enum("dcpi_market_slugs",
         f"""SELECT DISTINCT market_slug FROM market_power_scores
             WHERE market_slug IS NOT NULL AND {PUBLISHED_ONLY}
             ORDER BY market_slug""", limit=120)
    enum("us_states_covered",
         "SELECT DISTINCT state FROM market_power_scores WHERE state IS NOT NULL"
         f" AND char_length(state) = 2 AND {PUBLISHED_ONLY}")
    out["listing_statuses"] = ["public", "pocket", "draft"]
    enum("facility_status_values",
         "SELECT DISTINCT status FROM facilities WHERE status IS NOT NULL LIMIT 20")

    if errors:
        out["enum_errors"] = errors
    return out


def _freshness_window(cur):
    """Per-domain freshness as recorded in freshness_checks.

    Raises on a failed read so agent_index() records it in `section_errors`.
    An empty list here means "we checked and nothing is recorded"; it must not
    also mean "the query failed".
    """
    rows, err = _try_fetchall(cur,
        """SELECT surface, last_updated, stale_after_hours, status,
                  EXTRACT(EPOCH FROM (NOW() - last_updated)) / 3600 AS age_hours
             FROM freshness_checks
            WHERE last_updated IS NOT NULL
            ORDER BY surface""")
    if err:
        raise RuntimeError(err)
    return [{
        "domain": r[0],
        "last_updated": r[1].isoformat() if r[1] else None,
        "stale_after_hours": r[2],
        "status": r[3],
        "age_hours": round(float(r[4]), 2) if r[4] is not None else None,
    } for r in rows]


def _radar_issues(cur):
    """Active radar (anything not fresh).

    ★ 2026-08-01: this query selected `last_seen_at`, `severity` and `note` --
    three columns data_domain_freshness has never had. It raised UndefinedColumn
    on every single request, _safe_fetchall swallowed it, and the endpoint
    published `radar: []`, i.e. "no data-freshness issues", as a fact. It was
    also the query that aborted the shared transaction and zeroed the entire
    coverage block downstream. Live columns are: domain, source_table,
    source_ts_column, last_record_at, row_count, sla_hours, age_hours, status,
    detail, checked_at.
    """
    rows, err = _try_fetchall(cur,
        """SELECT domain, status, last_record_at, age_hours, sla_hours,
                  detail, row_count
             FROM data_domain_freshness
            WHERE status IS NOT NULL
              AND LOWER(status) NOT IN ('green', 'fresh', 'ok')
            ORDER BY age_hours DESC NULLS LAST, last_record_at DESC NULLS LAST
            LIMIT 20""")
    if err:
        raise RuntimeError(err)
    return [{
        "domain": r[0],
        "status": r[1],
        "last_record_at": r[2].isoformat() if r[2] else None,
        "age_hours": round(float(r[3]), 2) if r[3] is not None else None,
        "sla_hours": r[4],
        "detail": (r[5] or '')[:200],
        "row_count": r[6],
    } for r in rows]


def _coverage_summary(cur):
    """Top-line coverage inventory: rows-by-domain, geographic spread.

    ★ A COUNT THAT COULD NOT BE READ IS `None`, NEVER 0.

    This endpoint exists to tell an AI agent what data we hold. A zero here is
    not a neutral placeholder -- it is an affirmative claim that we have
    nothing, which is the single most damaging thing this surface can say. So
    a failed read publishes JSON `null` and names itself in `coverage_errors`,
    and `complete` goes False. An agent can branch on null; it cannot detect a
    lie told as 0.
    """
    out = {}
    errors = {}

    def count(key, table, where=""):
        sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        rows, err = _try_fetchall(cur, sql)
        if err:
            errors[key] = err
            return None
        if not rows:
            # COUNT(*) always returns exactly one row. No rows means the read
            # did not really happen, whatever the driver reported.
            errors[key] = "count query returned no rows"
            return None
        return int(rows[0][0])

    # 2026-07-31: `pipeline_projects` counted all 1,973 rows including the 725
    # quarantined ones, so the agent-facing coverage inventory disagreed with
    # the `pipeline` domain bundle below. Both now report 1,248.
    # See util/capacity_pipeline.
    #
    # 2026-08-01: `ma_transactions` counted a table named `transactions` that
    # has never existed in this database (the live M&A table is `deals`), so it
    # was a hard 0 on every request independent of the transaction-abort bug.
    # DEALS_OK is the same predicate routes/deals_routes.py and
    # routes/graph_spine_master_shell.py apply -- `deals` carries a legitimate
    # non-quarantine flag (cumulative_capex), hence the LEFT() prefix test
    # rather than capacity_pipeline's strict `= ''`. Guarded: 1,843 of 4,711.
    # It was a function-LOCAL here, which is the shape that let the 07-27
    # capacity_pipeline audit claim "every read" while fourteen had no guard;
    # it now imports from util/deals.py, which owns that reasoning and the
    # LEFT()-not-LIKE rule (a literal % beside a params tuple 500s psycopg2).
    for tbl, key, where in [
        ("facilities",            "facilities",              ""),
        ("discovered_facilities", "discovered_facilities",   ""),
        ("capacity_pipeline",     "pipeline_projects",       CP_OK),
        ("market_power_scores",   "dcpi_scored_markets",     ""),
        ("news",                  "news_articles",           ""),
        ("deals",                 "ma_transactions",         DEALS_OK),
        ("exclusive_listings",    "pocket_listings",         ""),
    ]:
        out[key] = count(key, tbl, where)

    # ★ discovered_facilities, NOT facilities. The two disagree -- 178 vs 186 --
    # and 178 is canon (#1958/#1966 closed the legacy-186 class). Reading
    # `facilities` here would have quietly republished 186 to AI agents the
    # moment the zeros were unblocked.
    rows, err = _try_fetchall(cur,
        """SELECT DISTINCT country FROM discovered_facilities
           WHERE country IS NOT NULL AND country <> ''
           ORDER BY country""")
    if err:
        errors["countries_covered"] = err
        out["countries_covered"] = None
    else:
        out["countries_covered"] = [r[0] for r in rows]

    out["complete"] = not errors
    if errors:
        out["coverage_errors"] = errors
    return out


@agent_index_bp.route("/api/v1/agent/index", methods=["GET"])
def agent_index():
    """One-call session warm-up. The first endpoint an AI agent should
    hit to learn what we have, what we know, and how fresh it is —
    so it stops guessing identifiers and re-pulling things it already
    has fresh."""
    out = {
        "ok": True,
        "purpose": ("Call this first. It returns valid enums, per-domain "
                    "freshness, radar issues, coverage inventory, and a "
                    "drill-deeper map so you don't waste tool calls "
                    "fumbling identifiers or re-pulling stale data."),
        "version": "1.0",
    }
    # Phase GG (2026-05-15) — Bundle 6A: ONE connection with autocommit,
    # FOUR independent cursors. The previous "fresh connection per section"
    # approach (PR #156) was bulletproof but cost ~500ms × 4 = 2s in
    # Postgres connect handshakes, making /agent/index a 2.5s endpoint.
    # With autocommit=True on the connection, each cursor's queries are
    # independent transactions, so a failure in one section can't poison
    # the others. Brings /agent/index from 2.5s → ~400ms.
    #
    # ★ 2026-08-01 — that last claim was false, and it cost us the whole
    # coverage block. The code said `with _conn() as c:`, and psycopg2's
    # connection context manager is a TRANSACTION manager: entering it opens an
    # explicit transaction that autocommit does not override. `c.autocommit`
    # still read True while the session sat in TRANSACTION_STATUS_INTRANS, so
    # nothing in Python could see it. The cascade, measured live:
    #
    #   enums     ok
    #   freshness ok
    #   radar     UndefinedColumn (last_seen_at) -> txn now INERROR, swallowed
    #   coverage  all 8 queries -> InFailedSqlTransaction -> [] -> 0 and []
    #
    # A transaction belongs to the CONNECTION, so per-section cursors bought
    # nothing. Hold the connection open by hand and close it in finally; the
    # four-cursor speed win is unaffected. _unpoison() in _try_fetchall is the
    # second line of defence if this ever regresses.
    c = None
    try:
        c = _conn()
        for section, fn in [("enums", _enums),
                            ("freshness", _freshness_window),
                            ("radar", _radar_issues),
                            ("coverage", _coverage_summary)]:
            try:
                with c.cursor() as cur:
                    out[section] = fn(cur)
            except Exception as e:
                out[section] = {} if section != "radar" else []
                out.setdefault("section_errors", {})[section] = str(e)[:200]
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    # Honest top-line marker: an agent should not have to descend into every
    # section to find out that part of this bundle is missing.
    _cov = out.get("coverage")
    if (out.get("section_errors") or out.get("error_partial")
            or (isinstance(_cov, dict) and not _cov.get("complete", True))):
        out["degraded"] = True

    out["drill_deeper"] = {
        "dcpi_scores":      "/api/v1/dcpi/scores",
        "iso_comparison":   "/api/v1/iso/comparison",
        "market_brief":     "/api/v1/brief/market?market=<slug>",
        "site_report":      "/api/v1/sites/<id_or_slug>/capacity-report",
        "pocket_listings":  "/api/v1/listings",
        "changes_since":    "/api/v1/changes/since?ts=<iso>",
        "coverage_detail":  "/api/v1/agent/coverage?domain=<d>&region=<r>",
        "openapi":          "/api/v1/openapi.json",
    }
    out["agent_tips"] = [
        "Use enums.dcpi_market_slugs as authoritative slug list — don't transliterate.",
        "If a domain appears in radar, flag your downstream answer with that caveat — "
        "`status` is the verdict and `age_hours` vs `sla_hours` is how far past due it is.",
        "A coverage value of null means we could NOT read that count — it does not "
        "mean zero. Check coverage.complete and coverage.coverage_errors before "
        "telling a human we hold no data for a domain.",
        "Pass back the `sources` block to the human — DC Hub citations build trust.",
        "Call /api/v1/changes/since with the last `generated_at` you cached to skip re-pulls.",
    ]
    sources = [
        src("Enum catalog", "market_power_scores + facilities + exclusive_listings", now_iso()),
        src("Per-domain freshness", "freshness_checks", now_iso()),
        src("Active issues", "data_domain_freshness", now_iso()),
        src("Coverage totals", "facilities + discovered_facilities + capacity_pipeline "
                               "+ market_power_scores + news + deals", now_iso()),
    ]
    try:
        from util.cache import with_edge_cache
        return with_edge_cache(jsonify(attach_sources(out, sources)), max_age=300, swr=600), 200
    except Exception:
        return jsonify(attach_sources(out, sources)), 200


@agent_index_bp.route("/api/v1/agent/coverage", methods=["GET"])
def agent_coverage():
    """Negative-space inventory. Pass `domain` (facilities, dcpi, pipeline,
    news, transactions, listings) and optionally `region` (state code or
    country). Returns what we have AND an explicit list of what we
    don't track for that region.

    Use this BEFORE making 5 calls to discover we don't have the data —
    one call here tells you the truth up front.
    """
    domain = (request.args.get("domain") or "").strip().lower()
    region = (request.args.get("region") or "").strip().upper()

    KNOWN = ["facilities", "dcpi", "pipeline", "news", "transactions",
             "listings", "fiber", "water", "tax", "grid"]

    if not domain:
        return jsonify(ok=True,
                       hint="pass ?domain=<name> and optionally ?region=<state-or-country-code>",
                       known_domains=KNOWN,
                       generated_at=now_iso()), 200

    if domain not in KNOWN:
        return jsonify(ok=False, error="unknown_domain",
                       known_domains=KNOWN), 200

    payload = {"ok": True, "domain": domain, "region": region or None}
    have = {}
    dont_have = []
    errors = {}

    def _q(key, sql, params=()):
        """Read, or name the failure. Same rule as _coverage_summary: this
        bundle is what an agent cites, so a broken read must not render as 0."""
        rows, err = _try_fetchall(cur, sql, params)
        if err:
            errors[key] = err
            return None
        return rows

    c = None
    try:
        c = _conn()          # ★ not `with` — see _conn()'s docstring
        with c.cursor() as cur:
            if domain == "facilities":
                if region:
                    rows = _q("count",
                        """SELECT COUNT(*), COALESCE(SUM(power_mw), 0)
                             FROM facilities
                            WHERE UPPER(state) = %s OR UPPER(country) = %s""",
                        (region, region))
                    have["count"] = int(rows[0][0]) if rows else None
                    have["total_mw"] = float(rows[0][1]) if rows and rows[0][1] else (
                        None if rows is None else 0)
                else:
                    rows = _q("count", "SELECT COUNT(*) FROM discovered_facilities")
                    have["count"] = int(rows[0][0]) if rows else None
                have["fields"] = ["id", "name", "provider", "city", "state",
                                  "country", "latitude", "longitude", "status",
                                  "power_mw", "source", "first_seen"]
                dont_have = ["PUE", "water source", "tenant list",
                             "lease terms", "actual energy mix",
                             "rack count", "cooling type"]

            elif domain == "dcpi":
                if region:
                    rows = _q("scored_markets",
                        f"""SELECT COUNT(DISTINCT market_slug)
                              FROM market_power_scores
                             WHERE (UPPER(state) = %s OR UPPER(iso) = %s)
                               AND {PUBLISHED_ONLY}""",
                        (region, region))
                else:
                    rows = _q("scored_markets",
                        "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores"
                        f" WHERE {PUBLISHED_ONLY}")
                have["scored_markets"] = int(rows[0][0]) if rows else None
                have["fields"] = ["verdict", "excess_power_score",
                                  "constraint_score", "time_to_power_months",
                                  "queue_wait_months", "computed_at"]
                dont_have = ["water-stress overlay (separate domain)",
                             "carbon-intensity overlay",
                             "real-time LMP (call get_grid_data)"]

            elif domain == "pipeline":
                # 2026-07-31: unguarded this told agents 1,973 projects /
                # 2,680,616 MW. Guarded: 1,248 / 586,597. This bundle is
                # explicitly built for AI agents to cite, so it is the last
                # place an inflated total belongs. See util/capacity_pipeline.
                rows = _q("projects", f"""SELECT COUNT(*),
                                                 COALESCE(SUM(capacity_mw), 0)
                                            FROM capacity_pipeline
                                           WHERE {CP_OK}""")
                have["projects"] = int(rows[0][0]) if rows else None
                have["total_mw"] = float(rows[0][1]) if rows and rows[0][1] else (
                    None if rows is None else 0)
                have["fields"] = ["operator", "market", "capacity_mw",
                                  "phase", "status", "completion_date", "notes"]
                dont_have = ["construction-cost estimate", "tenant pre-lease",
                             "interconnection-cost detail"]

            elif domain == "news":
                rows = _q("articles",
                    """SELECT COUNT(*), MIN(published_date), MAX(published_date)
                         FROM news""")
                if rows and rows[0]:
                    have["articles"] = int(rows[0][0] or 0)
                    have["earliest"] = rows[0][1].isoformat() if rows[0][1] else None
                    have["latest"] = rows[0][2].isoformat() if rows[0][2] else None
                else:
                    have["articles"] = None
                have["fields"] = ["title", "url", "published_date", "source", "body"]
                dont_have = ["sentiment scoring", "deal-impact rating"]

            elif domain == "transactions":
                # ★ 2026-08-01: this counted `transactions`, a table that has
                # never existed here, so it served a flat 0 to every agent that
                # asked whether we track M&A. The live table is `deals`; the
                # guard matches routes/deals_routes.py (LEFT(), not LIKE — a
                # literal % alongside a params tuple 500s psycopg2), and is
                # now the same string, imported from util/deals.py.
                rows = _q("count", f"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}")
                have["count"] = int(rows[0][0]) if rows else None
                have["fields"] = ["buyer", "seller", "value", "mw", "date",
                                  "market", "region", "type"]
                dont_have = ["pre-money valuation", "earnout terms",
                             "post-close performance"]

            elif domain == "listings":
                rows = _q("by_status",
                    "SELECT COUNT(*), status FROM exclusive_listings GROUP BY status")
                have["by_status"] = None if rows is None else {
                    r[1]: int(r[0]) for r in rows}
                have["fields"] = ["slug", "title", "status", "tier_required",
                                  "market", "capacity_mw", "asking_price",
                                  "detail", "contact"]
                dont_have = ["historical sale prices",
                             "off-platform listings",
                             "broker commissions"]

            elif domain == "fiber":
                have["fields"] = ["routes", "carriers", "POP locations"]
                dont_have = ["per-strand pricing", "SLA tier breakdown"]

            elif domain == "water":
                have["fields"] = ["stress score", "drought risk", "source basin"]
                dont_have = ["per-facility withdrawals", "wastewater discharge"]

            elif domain == "tax":
                have["fields"] = ["state incentive programs",
                                  "abatement details", "minimum investment"]
                dont_have = ["per-county property tax",
                             "local utility-tax exemptions"]

            elif domain == "grid":
                have["fields"] = ["heartbeat freshness", "ISO snapshot",
                                  "LMP via get_grid_data", "queue wait"]
                dont_have = ["sub-hour LMP", "node-level outage data"]
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    payload["have"] = have
    payload["dont_have"] = dont_have
    payload["complete"] = not (errors or payload.get("error_partial"))
    if errors:
        payload["query_errors"] = errors
    if not payload["complete"]:
        payload["degraded"] = True
    payload["recommendation"] = (
        f"For '{domain}' we ship the fields under `have.fields`. " +
        ("For " + region + ", " if region else "") +
        "do not waste tool calls asking for " +
        (", ".join(dont_have[:3]) if dont_have else "fields not listed in `have.fields`") +
        " — we don't track them today.")

    sources = [src(f"Coverage inventory for domain={domain}",
                   "agent_coverage (synthesized from row counts + schema)", now_iso())]
    return jsonify(attach_sources(payload, sources)), 200
