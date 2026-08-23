"""
changes_feed.py — Phase GG (2026-05-14): `get_changes_since` diff feed.

The agent persists the `generated_at` timestamp from a previous /agent/index
call, then calls this endpoint with `?since=<that-timestamp>`. We return a
diff across the major domains so the agent can skip re-pulling everything
just to detect what's new.

Endpoints:
    GET /api/v1/changes/since?since=<ISO-8601>&limit=N

If `since` is omitted, defaults to 24h ago. Hard ceiling of 30 days back
(any older and the agent should just do a fresh full pull).

All read-only. Best-effort per domain — if one table errors, we still
return the others.
"""
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request, g

from util.capacity_pipeline import CP_OK
from util.db_honesty import (DEAL_DATE, close_quietly, open_conn,
                             try_fetchall)
from util.deals import DEALS_OK

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
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

changes_feed_bp = Blueprint("changes_feed", __name__)

# ★2026-08-22 Claim Loop step 5 — the claim ledger's OUTCOMES ride this feed,
# so `get_changes` carries them with no mcp-server tool change (no new tool
# arg, no outputSchema). Lazy import: the ledger may be absent on an old
# deploy, and the literal is its SOURCE_LAYER.
try:
    from routes.claim_ledger import SOURCE_LAYER as _CLAIM_LAYER
except Exception:
    _CLAIM_LAYER = "CLAIM"

_LEDGER_UNAVAILABLE = {"count": 0, "basis": "ledger unavailable"}


def _claims_block(cur, since, limit):
    """Claims whose OUTCOME landed since `since` — confirmed (the claim held
    at horizon) and retracted (the owner withdrew it) — for an agent that
    wants to know what DC Hub said about itself and whether it stood.
    Refuted and unobserved claims are on the full, keyless public ledger at
    /api/v1/ops/claims. Fail-soft: a read that fails publishes
    {"count": 0, "basis": "ledger unavailable"} — the basis is the marker
    that this 0 is not a measurement."""
    rows, err = try_fetchall(cur, """
        SELECT id, kind, subject, statement, outcome, outcome_at, superseded_by
          FROM brain_predictions_log
         WHERE source_layer = %s AND outcome IN ('confirmed', 'retracted')
           AND outcome_at IS NOT NULL AND outcome_at >= %s
         ORDER BY outcome_at DESC LIMIT %s""", (_CLAIM_LAYER, since, limit * 2))
    if err:
        return dict(_LEDGER_UNAVAILABLE, error=err[:160])
    out = {"confirmed": [], "retracted": []}
    for r in rows:
        bucket = out.get(r[4])
        if bucket is None or len(bucket) >= limit:
            continue
        bucket.append({
            "id": r[0], "kind": r[1], "subject": r[2], "statement": r[3],
            "outcome_at": r[5].isoformat() if r[5] is not None and hasattr(r[5], "isoformat") else r[5],
            "superseded_by": r[6],
        })
    out["count"] = len(out["confirmed"]) + len(out["retracted"])
    out["basis"] = ("claims whose outcome_at >= since: confirmed = held at "
                    "horizon, retracted = the owner withdrew it (superseded_by "
                    "names the replacement). The full ledger, refuted and "
                    "unobserved included, is keyless at /api/v1/ops/claims.")
    return out


def _conn():
    """★ DO NOT write `with _conn() as c:` — see util/db_honesty.open_conn.

    The old comment here ("autocommit so one failed domain query doesn't poison
    the rest") was false while this handler used `with _conn() as c`: psycopg2's
    connection context manager opens an explicit transaction that autocommit
    does not override. `_safe`'s rollback was what actually kept the domains
    independent — the autocommit flag never did.
    """
    return open_conn()


# WS6 (2026-07-29) — ★ ZERO IS NOT EVIDENCE. The `_safe` helper that used to
# live here swallowed every SQL error into [], so a missing column, a wrong
# table and a genuinely quiet week all rendered identically as 0 and the
# response told agents "nothing changed". It grew an error-recording block and
# a rollback over two rounds of fixes, and still published the 0.
#
# ★ 2026-08-01 — replaced by the `lane()` closure in changes_since(), which
# publishes NULL for a lane it could not read. Recording the error next to a 0
# was never enough: the 0 is what a consumer parses, and `domain_errors` is
# what a consumer has to remember to check. The rollback survives inside
# util.db_honesty.try_fetchall — it is what actually kept the domains
# independent here, not the autocommit flag the old comment credited.


def _parse_since(raw):
    """Parse a since= param robustly. Returns a tz-aware datetime."""
    now = datetime.now(timezone.utc)
    if not raw:
        return now - timedelta(hours=24), "default-24h"
    try:
        # Allow plain "24h" / "7d" shorthand
        s = raw.strip().lower()
        if s.endswith("h") and s[:-1].isdigit():
            return now - timedelta(hours=int(s[:-1])), f"shorthand-{s}"
        if s.endswith("d") and s[:-1].isdigit():
            return now - timedelta(days=int(s[:-1])), f"shorthand-{s}"
        # ISO-8601 with or without 'Z'
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Clamp: no further back than 30 days
        floor = now - timedelta(days=30)
        if dt < floor:
            return floor, "clamped-30d"
        return dt, "iso"
    except Exception:
        return now - timedelta(hours=24), "parse-error-fallback-24h"


@changes_feed_bp.route("/api/v1/changes/since", methods=["GET"])
def changes_since():
    """Cross-domain diff feed.

    Query params:
        since   ISO-8601 timestamp OR shorthand "24h" / "7d". Defaults to 24h.
        limit   max rows per domain (default 25, max 200)
    """
    raw_since = request.args.get("since") or request.args.get("ts") or ""
    since, since_mode = _parse_since(raw_since)
    try:
        limit = int(request.args.get("limit") or 25)
    except Exception:
        limit = 25
    limit = max(1, min(limit, 200))

    payload = {"ok": True, "since": since.isoformat(),
               "since_mode": since_mode, "limit_per_domain": limit}
    diff = {}
    counts = {}
    sources = []
    # Lanes whose read FAILED. `counts[lane]` and `diff[lane]` publish null for
    # these, never 0/[] — an agent that cannot tell "nothing changed" from "we
    # could not look" will skip a pull it needed to make.
    dead_lanes = set()

    def lane(key, sql, params, build, source_label=None, source_table=None):
        """Run one domain lane. On failure: null, not zero."""
        rows, err = try_fetchall(cur, sql, params)
        if err:
            dead_lanes.add(key)
            diff[key] = None
            counts[key] = None
            try:
                errs = getattr(g, "_changes_feed_errors", None)
                if errs is None:
                    errs = {}
                    g._changes_feed_errors = errs
                errs[source_table or key] = err
            except Exception:
                pass
            return []
        items = [build(r) for r in rows]
        diff[key] = items
        counts[key] = len(items)
        if items and source_label and source_table:
            sources.append(src(source_label(len(items)), source_table, now_iso()))
        return items

    # Claim Loop step 5: the block is fail-soft by construction — it holds
    # the "unavailable" marker until a read replaces it.
    claims = dict(_LEDGER_UNAVAILABLE)
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # Pipeline: new projects.
            # ★ 2026-07-31 audit — DEAD READ. `capacity_pipeline` has no
            # `first_seen` column; UndefinedColumn is swallowed by `_safe` and
            # the feed has always reported `pipeline_new: []` with
            # `counts.pipeline_new = 0`. An under-claim, so not auto-repaired.
            # 2026-08-01 made it honest: null lane + named in `domain_errors`.
            # ★ 2026-08-02 — REPAIRED, and the framing that made this an owner
            # call was a false dichotomy. It was posed as "cast created_at
            # (TEXT — throws and gets swallowed)" vs "use extracted_at (real
            # timestamptz, but 529 of 1,973 rows → under-reports 73%)". There
            # is a third option, and it is already PROVEN in this codebase:
            # ai_tracking.get_cumulative_totals() solved the identical
            # TEXT-column-compared-as-timestamp problem with a regex-guarded
            # CASE, where CASE guarantees the shape check runs BEFORE the cast
            # can throw. Same pattern here, preferring the real timestamptz
            # where it exists:
            #
            #   COALESCE(extracted_at, <guarded created_at cast>)
            #
            # created_at carries `DEFAULT CURRENT_TIMESTAMP`, so it is
            # populated on every row — no 73% hole — and it is also the
            # SEMANTICALLY correct signal for this lane: "what appeared in DC
            # Hub since your timestamp" is a row-arrival question, which is
            # exactly what first_seen was meant to answer. A row whose
            # created_at is malformed resolves to NULL and is excluded rather
            # than throwing the whole lane; `_row_ts` is the single expression
            # so the WHERE, the ORDER BY and the returned field can never
            # disagree about which clock they are on.
            _row_ts = ("COALESCE(extracted_at, CASE WHEN created_at ~ "
                       r"'^\d{4}-\d{2}-\d{2}' THEN created_at::timestamptz END)")
            lane("pipeline_new", f"""
                    SELECT operator, market, capacity_mw, phase, status,
                           completion_date, {_row_ts} AS first_seen
                      FROM capacity_pipeline
                     WHERE {_row_ts} > %s
                       AND {CP_OK}
                     ORDER BY {_row_ts} DESC LIMIT %s""", (since, limit),
                 lambda r: {
                     "operator": r[0], "market": r[1],
                     "capacity_mw": float(r[2]) if r[2] is not None else None,
                     "phase": r[3], "status": r[4],
                     "completion_date": r[5].isoformat() if r[5] and hasattr(r[5], 'isoformat') else r[5],
                     "first_seen": r[6].isoformat() if r[6] and hasattr(r[6], 'isoformat') else r[6],
                 },
                 lambda n: f"{n} new pipeline projects", "capacity_pipeline")

            # News: new articles.
            # ★ 2026-08-23 — WRONG TABLE, and it read as "nothing happened".
            # This lane queried `news` (the legacy radar table) while get_news
            # and /api/v1/news serve `news_articles`. Measured on prod: `news`
            # held 3 rows in 14 days, newest 2026-08-21T23:00, so the DEFAULT
            # 24h window returned `news_new: 0` while news_articles carried 34
            # rows in that same 24h (newest 3h old, 15,009 rows since 05-25).
            # discover_tools names get_changes as THE refresh path, so every
            # returning agent following our own documented sync pattern was
            # told the corpus was static. Under-report, so never auto-repaired.
            #
            # ★ The repoint ALONE would have swapped a silent 0 for a dead
            # lane. `news_articles.published_at` is TEXT on prod and TIMESTAMP
            # elsewhere (routes/deals_routes.py:1811 documents the same split),
            # and prod holds MIXED formats — "2026-08-22T21:01:00" beside
            # "2026-05-25 04:14:46+00". Binding the tz-aware `since` against a
            # TEXT column raises `operator does not exist: text > timestamptz`,
            # which lane() would publish as null + a domain_error.
            # So the expression, in order: `::text` (no-op on TEXT, renders the
            # TIMESTAMP/TIMESTAMPTZ variants), a regex guard so the shape check
            # runs BEFORE the cast can throw — the same CASE pattern `_row_ts`
            # above uses — then `AT TIME ZONE 'UTC'` to pin the naive strings to
            # the clock they were written on rather than inheriting the session
            # TimeZone. ONE expression drives WHERE, ORDER BY and the returned
            # field so they can never disagree about which clock they are on.
            # A row that does not match the shape resolves to NULL and is
            # excluded, rather than throwing the whole lane.
            #
            # Upper bound = the same 6h future grace the news domain declares
            # for itself on the REST path: without it one future-dated row sits
            # at the top of "what changed" permanently. Output key stays
            # `published_date` — that is the feed's wire contract, not the
            # column name.
            _news_ts = (r"CASE WHEN published_at::text ~ '^\d{4}-\d{2}-\d{2}' "
                        r"THEN (published_at::text::timestamp AT TIME ZONE 'UTC') "
                        r"END")
            lane("news_new", f"""
                    SELECT title, url, {_news_ts} AS published_date, source
                      FROM news_articles
                     WHERE {_news_ts} > %s
                       AND {_news_ts} <= %s
                     ORDER BY {_news_ts} DESC LIMIT %s""",
                 (since, datetime.now(timezone.utc) + timedelta(hours=6), limit),
                 lambda r: {
                     "title": r[0], "url": r[1],
                     "published_date": r[2].isoformat() if r[2] and hasattr(r[2], 'isoformat') else r[2],
                     "source": r[3],
                 },
                 lambda n: f"{n} new articles", "news_articles")

            # DCPI: markets that MOVED 1+ excess-power pt over the last 7 days.
            # 2026-06-06 fix: was diffing market_power_scores.computed_at, but
            # that table is RE-STAMPED on every recompute so the diff was always
            # empty (total_changes stuck at 0). Read real deltas from the
            # history-preserving dcpi_daily_snapshots instead — same verified
            # query the digest/brief movers use. No `since` param → no tz risk.
            lane("dcpi_movers", """
                    WITH latest AS (
                      SELECT DISTINCT ON (market_slug) market_slug, market_name,
                             excess_power_score AS now_e
                      FROM dcpi_daily_snapshots
                      ORDER BY market_slug, snapshot_date DESC
                    ), prev AS (
                      SELECT DISTINCT ON (market_slug) market_slug,
                             excess_power_score AS prev_e
                      FROM dcpi_daily_snapshots
                      WHERE snapshot_date <= CURRENT_DATE - 7
                      ORDER BY market_slug, snapshot_date DESC
                    )
                    SELECT l.market_slug, l.market_name, l.now_e,
                           (l.now_e - p.prev_e) AS delta
                    FROM latest l JOIN prev p ON l.market_slug = p.market_slug
                    WHERE ABS(l.now_e - p.prev_e) >= 1
                    ORDER BY ABS(l.now_e - p.prev_e) DESC LIMIT %s""", (limit,),
                 lambda r: {
                     "market_slug": r[0], "market": r[1],
                     "excess_power_score": round(float(r[2]), 1) if r[2] is not None else None,
                     "delta_7d": round(float(r[3]), 1) if r[3] is not None else None,
                 },
                 lambda n: f"{n} DCPI markets moved 1+pt (7d)", "dcpi_daily_snapshots")

            # Transactions: new deals
            # ★ 2026-08-01 — read `transactions`, a table that has NEVER
            # existed in this database. Same never-existed table as the
            # `ma_transactions: 0` closed by #2071, and the same lie: an agent
            # polling this feed to learn what M&A had happened since its last
            # session was told "nothing" every single time. Live table is
            # `deals`; DEALS_OK/DEAL_DATE are the canonical guard + date cast.
            lane("transactions_new", f"""
                    SELECT seller, buyer, value, {DEAL_DATE} AS announced_on,
                           market, type
                      FROM deals
                     WHERE {DEALS_OK}
                       AND {DEAL_DATE} IS NOT NULL AND {DEAL_DATE} > %s
                     ORDER BY announced_on DESC LIMIT %s""",
                 (since.date(), limit),
                 lambda r: {
                     "target": r[0], "acquirer": r[1],
                     # millions, not raw USD — `deals.value` is a millions
                     # column. The dead query called this `value_usd`; keeping
                     # that name would publish a figure 1,000,000x too small.
                     "value_usd_millions": float(r[2]) if r[2] is not None else None,
                     "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                     "market": r[4], "deal_type": r[5],
                 },
                 lambda n: f"{n} new transactions", "deals")

            # Pocket listings: newly created
            lane("pocket_listings_new", """
                    SELECT slug, title, market, state, capacity_mw, status,
                           tier_required, created_at
                      FROM exclusive_listings
                     WHERE created_at IS NOT NULL AND created_at > %s
                       AND status IN ('public', 'pocket')
                     ORDER BY created_at DESC LIMIT %s""", (since, limit),
                 lambda r: {
                     "slug": r[0], "title": r[1], "market": r[2],
                     "state": r[3],
                     "capacity_mw": float(r[4]) if r[4] is not None else None,
                     "status": r[5], "tier_required": r[6],
                     "created_at": r[7].isoformat() if r[7] else None,
                 },
                 lambda n: f"{n} new pocket listings", "exclusive_listings")

            # Facilities: newly discovered. 2026-06-06 fix: canonical table is
            # discovered_facilities (23k+ rows) — NOT the small curated
            # `facilities` table the old query used (few/no recent rows).
            # ★ 2026-08-01 — that 06-06 fix moved to the right TABLE and then
            # named two columns it does not have: `created_at` and
            # `capacity_mw`. discovered_facilities carries `first_seen`
            # (timestamptz, populated on all 23,515 rows) and `power_mw`. So
            # the lane it was written to repair went on returning [] anyway —
            # a fix that looked applied and never was, which is precisely why
            # a green read is not evidence. Verified against information_schema
            # on the live DB, 2026-08-01.
            lane("facilities_new", """
                    SELECT id, name, state, power_mw, first_seen
                      FROM discovered_facilities
                     WHERE first_seen IS NOT NULL AND first_seen > %s
                     ORDER BY first_seen DESC LIMIT %s""", (since, limit),
                 lambda r: {
                     "id": r[0], "name": r[1], "state": r[2],
                     "capacity_mw": float(r[3]) if r[3] is not None else None,
                     "discovered_at": r[4].isoformat() if r[4] and hasattr(r[4], 'isoformat') else r[4],
                 },
                 lambda n: f"{n} newly discovered facilities", "discovered_facilities")

            # Claim Loop step 5 — what DC Hub claimed about itself and
            # whether it stood, since the same `since`.
            claims = _claims_block(cur, since, limit)
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
    finally:
        close_quietly(c)

    # r-portfolio (2026-07-11): the feed was 100% GLOBAL — an agent with a
    # saved Phoenix site got California movers and no mention its own
    # portfolio existed. For identified callers, overlay per-saved-site
    # deltas (verdict flips, excess-power moves, alerts fired, new
    # facilities nearby) since the SAME `since` the agent passed. This is
    # the retention answer nothing else provides: "did MY sites move?".
    # Fail-soft: any failure and the response is exactly the global feed.
    portfolio = None
    try:
        from routes.lp_sites import _user_id_from_request, portfolio_snapshot
        uid = _user_id_from_request()
        if uid:
            pf = portfolio_snapshot(uid, since_dt=since)
            if pf is not None:
                if pf.get("saved_sites"):
                    pf.pop("sites", None)   # feed carries the movers, not the roster
                    portfolio = pf
                    counts["portfolio_sites_moved"] = int(pf.get("moved_count") or 0)
                else:
                    portfolio = {
                        "saved_sites": 0,
                        "hint": ("You have no saved sites yet — save_site "
                                 "persists candidates to your key, and this "
                                 "feed will then lead with per-site deltas "
                                 "(verdict flips, score moves, alerts fired) "
                                 "instead of only global movers."),
                    }
    except Exception:
        portfolio = None

    # WS6 (2026-07-29): publish WHICH domains actually answered. `_safe`
    # returns [] on any SQL error, so before this a broken lane and a quiet
    # window were indistinguishable and total_changes=0 was served to agents
    # as "nothing changed". Additive only — no existing field renamed.
    _domain_errors = {}
    try:
        _domain_errors = dict(getattr(g, "_changes_feed_errors", None) or {})
    except Exception:
        _domain_errors = {}
    payload["counts"] = counts
    # ★ A failed lane is null in `counts`, so it cannot be summed — sum() over
    # a dict containing None raises, and silently coercing it to 0 is the exact
    # bug this file keeps re-learning. total_changes is a floor over the lanes
    # that actually answered.
    payload["total_changes"] = sum(v for v in counts.values() if isinstance(v, int))
    payload["counts_complete"] = not _domain_errors
    if _domain_errors:
        payload["domain_errors"] = _domain_errors
        payload["unreadable_lanes"] = sorted(dead_lanes)
        payload["counts_basis"] = (
            f"PARTIAL - {len(_domain_errors)} source table(s) failed to "
            "answer. total_changes is a FLOOR over the lanes that ran; the "
            "lanes in `unreadable_lanes` are null (UNKNOWN), not 0.")
    _at_limit = sorted(k for k, v in counts.items()
                       if isinstance(v, int) and v >= limit)
    if _at_limit:
        payload["counts_at_limit"] = _at_limit
        payload["counts_at_limit_note"] = (
            "these domains returned exactly limit_per_domain rows - their "
            "count is a FLOOR, not a total. Raise `limit` to see more.")
    if portfolio is not None:
        payload["portfolio"] = portfolio
    payload["diff"] = diff
    payload["claims"] = claims
    _tip = (
        "Cache this response's `generated_at`. On your next session, "
        "pass it back as `?since=<that-value>` to get only what's new. "
        + ("Some domains could not be read on this call (see "
           "`domain_errors`) - do NOT read total_changes as 'nothing "
           "changed'." if _domain_errors else
           "If total_changes is 0, your previous full pull is still fresh."))
    if portfolio and portfolio.get("moved_count"):
        _names = [m["name"] for m in portfolio.get("moved", [])[:3] if m.get("name")]
        _tip = (f"{portfolio['moved_count']} of your {portfolio['saved_sites']} "
                f"saved site(s) moved since {payload['since'][:10]}"
                + (f" — {', '.join(_names)}" if _names else "")
                + ". See `portfolio.moved` for verdict/score deltas. " + _tip)
    elif portfolio and portfolio.get("saved_sites"):
        _tip = (f"None of your {portfolio['saved_sites']} saved site(s) moved "
                f"in this window (alerts stay armed). " + _tip)
    payload["agent_tip"] = _tip
    payload["drill_deeper"] = {
        "pipeline_full":      "/api/v1/pipeline",
        "news_full":          "/api/v1/news",
        "dcpi_full":          "/api/v1/dcpi/scores",
        "transactions_full":  "/api/v1/transactions",
        "listings_full":      "/api/v1/listings",
        "claims_full":        "/api/v1/ops/claims",
    }
    return jsonify(attach_sources(payload, sources)), 200
