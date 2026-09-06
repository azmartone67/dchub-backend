"""
persona_briefs.py — Phase GG (2026-05-14): 4 persona-shaped briefs.

Today an agent has to call 6+ endpoints and do its own synthesis. We do
the synthesis. Each brief is a SINGLE call that answers a SHAPED question
for a SPECIFIC user persona — developer, buyer, investor, or policy.

Endpoints:
    GET /api/v1/brief/developer?load_mw=&state=&deadline_months=
    GET /api/v1/brief/buyer?market=&min_mw=&max_price=
    GET /api/v1/brief/investor?operator=
    GET /api/v1/brief/policy?state=

All pure reads, best-effort per section, provenance attached so the
agent can pass citations to its human.
"""
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from util.capacity_pipeline import CP_OK
from util.dcpi_score_row import PUBLISHED_ONLY
from util.deployability_rank import RANKINGS as _DEPLOY_RANKINGS
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

# Phase GG (2026-05-15) Bundle 6F item 19: soft-paywall decorator.
# Anonymous + identified users still get a useful preview of each persona
# brief (top 3 items in lists) with a tier-aware upgrade CTA appended.
# Developer/Pro/Enterprise see full output. Safer than gating with 401
# because the AI assistant + human can still cite "3 of N items shown".
try:
    from util.tier_gate import soft_gate, Tier as RestTier
    _HAS_SOFT_GATE = True
except Exception:
    _HAS_SOFT_GATE = False
    # No-op stand-in so endpoint decorators below don't crash on import.
    def soft_gate(*a, **kw):
        def deco(fn): return fn
        return deco
    class RestTier:  # minimal shim
        DEVELOPER = 2

#: This brief's ranking — named, so `rank_score` never reads as a DCPI
#: score beside the two real DCPI columns in the same row.
_RANK = _DEPLOY_RANKINGS["developer_brief"]

persona_briefs_bp = Blueprint("persona_briefs", __name__)


def _conn():
    """★ DO NOT write `with _conn() as c:` — see util/db_honesty.open_conn.

    The comment that used to sit here said "autocommit so one failed sub-query
    doesn't poison the rest of the bundle". That was false, and every brief in
    this file paid for it: psycopg2's connection context manager opens an
    explicit transaction that autocommit does not override, so one dead column
    aborted the transaction and every LATER query in the same brief — valid or
    not — died with InFailedSqlTransaction and was swallowed into [].
    """
    return open_conn()


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _read(cur, sql, errors, key, params=()):
    """A read whose failure is PUBLISHED rather than swallowed.

    Returns rows, and on failure records `errors[key]` so the handler can
    publish null + a named error instead of a confident zero. Rolls back, so
    one dead read can no longer take the rest of the brief down with it.
    """
    rows, err = try_fetchall(cur, sql, params)
    if err:
        errors[key] = err
    return rows


def _finish(payload, errors, sources):
    """Attach the honesty envelope every brief in this file now carries."""
    payload["complete"] = not errors
    if errors:
        payload["query_errors"] = errors
        payload["completeness_note"] = (
            f"PARTIAL - {len(errors)} section(s) could not be read. Those keys "
            "are null, NOT 0: null means UNKNOWN, and a 0 or an empty list "
            "elsewhere in this response is a measured result.")
    return jsonify(attach_sources(payload, sources)), 200


# ─────────────────────────────────────────────────────────────────────
# DEVELOPER BRIEF — "Where should I build?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/developer", methods=["GET"])
@soft_gate(min_tier=RestTier.DEVELOPER,
           teaser="full ranked shortlist with operator-level rationale + competing-construction MW + 20-source citation chain",
           truncate_to=3,
           truncate_keys=["shortlist", "sources"])
def developer_brief():
    """Ranked site-selection shortlist with rationale.

    Query params:
        load_mw          target capacity in MW (filters to markets that can absorb)
        state            optional 2-letter state filter (e.g. "TX")
        deadline_months  desired time-to-power; markets above this score lower
    """
    try:
        load_mw = float(request.args.get("load_mw") or 100)
    except Exception:
        load_mw = 100
    state = (request.args.get("state") or "").strip().upper()
    try:
        deadline_months = float(request.args.get("deadline_months") or 36)
    except Exception:
        deadline_months = 36

    payload = {"ok": True, "persona": "developer",
               "input": {"load_mw": load_mw, "state": state or None,
                         "deadline_months": deadline_months}}
    shortlist = []
    sources = []
    errors = {}

    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # r-deployability-rank (2026-09-06): PUBLISHED_ONLY was
            # missing. The retired alias-twins (r-twin-unpublish) were
            # unpublished, NOT deleted, so this shortlist could rank a row
            # frozen since 2026-07-19 beside daily-recomputed ones and hand a
            # paying developer a market under a slug every other surface now
            # 301s away from. Same read-side hole closed on /markets,
            # /pockets and the market brief; the same spelling, character for
            # character, as /dcpi and /api/v1/dcpi/scores serve on — which is
            # the point of importing it rather than writing `published = true`
            # a fifth time.
            where = ["computed_at IS NOT NULL", "verdict != 'AVOID'",
                     PUBLISHED_ONLY]
            params = []
            if state:
                where.append("UPPER(state) = %s")
                params.append(state)

            sql = f"""
              SELECT DISTINCT ON (market_slug)
                     market_slug, market_name, iso, state, verdict,
                     excess_power_score, constraint_score,
                     time_to_power_months, computed_at
                FROM market_power_scores
               WHERE {' AND '.join(where)}
            ORDER BY market_slug, computed_at DESC"""
            rows = _read(cur, sql, errors, "shortlist", params)

            # In-memory ranking. Score = excess_power - constraint - deadline_penalty.
            ranked = []
            for r in rows:
                slug, name, iso, st, verdict, excess, constraint, ttp, cat = r
                excess_v = float(excess) if excess is not None else 0
                constraint_v = float(constraint) if constraint is not None else 0
                ttp_v = float(ttp) if ttp is not None else 36
                # penalty: 0 if under deadline, else 5pts per month over
                #
                # r-deployability-rank (2026-09-06): this is a DEPLOYABILITY
                # RANKING, not the DCPI composite. It is deadline-relative —
                # the penalty is measured against what THIS caller asked for —
                # which is why it is not shared with /pockets (fixed 24-month
                # penalty) or /api/v1/dcpi/recommend (urgency bonus, no
                # penalty). Three questions, three formulas, all legitimate.
                # What they must share is the NAME, so an agent never reads
                # one of them as the 0-100 DCPI composite; see
                # util/deployability_rank.py.
                deadline_penalty = max(0, (ttp_v - deadline_months)) * 5
                score = excess_v - (constraint_v * 0.5) - deadline_penalty
                if verdict == "BUILD":
                    score += 10  # tiebreaker
                ranked.append({
                    "rank_score": round(score, 1),
                    "market_slug": slug, "market_name": name,
                    "iso": iso, "state": st, "verdict": verdict,
                    "excess_power_score": _as_float(excess),
                    "constraint_score": _as_float(constraint),
                    "time_to_power_months": _as_float(ttp),
                    "rationale": _developer_rationale(verdict, excess_v, constraint_v, ttp_v, deadline_months),
                    "computed_at": cat.isoformat() if cat else None,
                })
                if cat:
                    sources.append(src(f"DCPI snapshot for {slug}",
                                       "market_power_scores", cat))
            ranked.sort(key=lambda r: -r["rank_score"])
            shortlist = ranked[:10]

            # Pipeline pressure check: total in-construction MW in each market
            for entry in shortlist:
                rows, err = try_fetchall(cur, f"""
                    SELECT COALESCE(SUM(capacity_mw), 0)
                      FROM capacity_pipeline
                     WHERE market ILIKE %s
                       AND LOWER(COALESCE(phase, status, '')) LIKE '%%construct%%'
                       AND {CP_OK}""",
                    (f"%{entry['market_slug']}%",))
                if err:
                    # null, not 0 — "we could not measure competing construction
                    # here" is the opposite claim to "nothing is being built".
                    entry["competing_construction_mw"] = None
                    errors.setdefault("competing_construction_mw", err)
                elif rows and rows[0][0]:
                    entry["competing_construction_mw"] = _as_float(rows[0][0])
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    # null, not [] — an empty shortlist is a real answer ("no market clears
    # your deadline"); a failed read is not, and the two must not render alike.
    payload["shortlist"] = None if "shortlist" in errors else shortlist
    # ADDITIVE (r-deployability-rank 2026-09-06): `rank_score` sits in every
    # shortlist row beside excess_power_score and constraint_score, which are
    # real DCPI columns — so an unnamed third number reads as a DCPI score.
    # The key stays; the naming arrives beside it, once per response rather
    # than once per row.
    payload["rank_score_label"] = _RANK.label
    payload["rank_score_basis"] = _RANK.basis
    payload["methodology"] = (
        "Score = excess_power_score − (constraint_score × 0.5) − "
        "max(0, time_to_power − deadline) × 5; +10 bonus for BUILD verdict. "
        "Markets with AVOID verdict are excluded.")
    payload["drill_deeper"] = {
        "site_planner":  "/api/v1/site-planner/recommend",
        "market_brief":  "/api/v1/brief/market?market=<slug>",
        "site_report":   "/api/v1/sites/<id_or_slug>/capacity-report",
    }
    return _finish(payload, errors, sources)


def _developer_rationale(verdict, excess, constraint, ttp, deadline):
    parts = []
    if verdict == "BUILD":
        parts.append("DCPI says BUILD")
    if excess >= 70:
        parts.append(f"strong excess power ({excess:.0f})")
    elif excess >= 50:
        parts.append(f"adequate excess power ({excess:.0f})")
    else:
        parts.append(f"tight power supply ({excess:.0f})")
    if constraint >= 60:
        parts.append(f"high grid constraint risk ({constraint:.0f})")
    if ttp > deadline:
        parts.append(f"time-to-power {ttp:.0f}mo exceeds {deadline:.0f}mo deadline")
    elif ttp <= deadline * 0.6:
        parts.append(f"fast TTP ({ttp:.0f}mo)")
    return "; ".join(parts) or "neutral"


# ─────────────────────────────────────────────────────────────────────
# BUYER BRIEF — "What's available to buy?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/buyer", methods=["GET"])
@soft_gate(min_tier=RestTier.DEVELOPER,
           teaser="full candidate facility roster + pocket-listing inventory (Pro+) + 10-deal transaction comparable history",
           truncate_to=3,
           truncate_keys=["candidate_facilities", "pocket_listings", "recent_comparables"])
def buyer_brief():
    """On-market + pocket inventory + recent comparables for buyers.

    Query params:
        market    optional market slug filter
        state     optional 2-letter state filter
        min_mw    optional minimum capacity (default 50)
    """
    market = (request.args.get("market") or "").strip()
    state = (request.args.get("state") or "").strip().upper()
    try:
        min_mw = float(request.args.get("min_mw") or 50)
    except Exception:
        min_mw = 50

    payload = {"ok": True, "persona": "buyer",
               "input": {"market": market or None, "state": state or None,
                         "min_mw": min_mw}}
    sources = []
    errors = {}

    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # On-market: facilities flagged status='for sale' or similar.
            sql_fac = """SELECT id, name, provider, city, state, country, power_mw, status
                           FROM facilities
                          WHERE power_mw >= %s"""
            params = [min_mw]
            if state:
                sql_fac += " AND UPPER(state) = %s"
                params.append(state)
            if market:
                sql_fac += " AND city ILIKE %s"
                params.append(f"%{market}%")
            sql_fac += " ORDER BY power_mw DESC NULLS LAST LIMIT 20"
            on_market = []
            for r in _read(cur, sql_fac, errors, "candidate_facilities", params):
                on_market.append({
                    "id": r[0], "name": r[1], "provider": r[2],
                    "city": r[3], "state": r[4], "country": r[5],
                    "power_mw": _as_float(r[6]), "status": r[7],
                })
            payload["candidate_facilities"] = (
                None if "candidate_facilities" in errors else on_market)
            if on_market:
                sources.append(src(
                    f"Candidate facility list ({len(on_market)} matching capacity ≥{min_mw} MW)",
                    "facilities", now_iso()))

            # Pocket listings matching same criteria.
            sql_li = """SELECT id, slug, title, market, state, capacity_mw,
                               asking_price, asking_currency, tier_required, status
                          FROM exclusive_listings
                         WHERE status IN ('public', 'pocket')
                           AND (capacity_mw IS NULL OR capacity_mw >= %s)"""
            li_params = [min_mw]
            if state:
                sql_li += " AND UPPER(state) = %s"
                li_params.append(state)
            if market:
                sql_li += " AND market ILIKE %s"
                li_params.append(f"%{market}%")
            sql_li += " ORDER BY capacity_mw DESC NULLS LAST LIMIT 10"
            listings = []
            for r in _read(cur, sql_li, errors, "pocket_listings", li_params):
                listings.append({
                    "id": r[0], "slug": r[1], "title": r[2],
                    "market": r[3], "state": r[4],
                    "capacity_mw": _as_float(r[5]),
                    "asking_price": _as_float(r[6]),
                    "asking_currency": r[7],
                    "tier_required": r[8], "status": r[9],
                })
            payload["pocket_listings"] = (
                None if "pocket_listings" in errors else listings)
            if listings:
                sources.append(src(
                    f"Pocket-listing inventory ({len(listings)} entries)",
                    "exclusive_listings", now_iso()))

            # Comparables: recent deals in the same geography.
            # ★ 2026-08-01 — this read `transactions`, a table that has NEVER
            # existed in this database, so `recent_comparables` has been a hard
            # [] on every request since it shipped. Same never-existed table as
            # the `ma_transactions: 0` closed by #2071; the live M&A table is
            # `deals`. DEALS_OK is the canonical publish guard (1,843 of 4,711).
            # DEAL_DATE regex-guards the TEXT `date` column: `deal_date` is a
            # real timestamp but covers only 280 rows and stops at 2026-03-02,
            # while `date` covers 813 and is current to today.
            sql_tx = f"""SELECT seller, buyer, value, {DEAL_DATE} AS announced_on,
                                market, type
                           FROM deals
                          WHERE {DEALS_OK} AND {DEAL_DATE} IS NOT NULL"""
            tx_params = []
            if market:
                sql_tx += " AND market ILIKE %s"
                tx_params.append(f"%{market}%")
            sql_tx += " ORDER BY announced_on DESC NULLS LAST LIMIT 10"
            comps = []
            for r in _read(cur, sql_tx, errors, "recent_comparables", tx_params):
                comps.append({
                    "target": r[0], "acquirer": r[1],
                    # ★ MILLIONS, not raw USD. `deals.value` is a millions
                    # column; the dead query called this `value_usd`, so
                    # reviving it under that name would have published a figure
                    # 1,000,000x too small. The key is renamed deliberately —
                    # nothing ever consumed the old one, it never returned.
                    "value_usd_millions": _as_float(r[2]),
                    "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                    "market": r[4], "deal_type": r[5],
                })
            payload["recent_comparables"] = (
                None if "recent_comparables" in errors else comps)
            if comps:
                sources.append(src(
                    f"Transaction comparables ({len(comps)} recent deals)",
                    "deals", now_iso()))
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    payload["drill_deeper"] = {
        "site_report":    "/api/v1/sites/<id>/capacity-report",
        "pocket_detail":  "/api/v1/listings/<id>",
        "tx_database":    "/api/v1/transactions",
    }
    return _finish(payload, errors, sources)


# ─────────────────────────────────────────────────────────────────────
# INVESTOR BRIEF — "What's this operator's trajectory?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/investor", methods=["GET"])
@soft_gate(min_tier=RestTier.DEVELOPER,
           teaser="full operator M&A history + pipeline contribution + 8-peer benchmark + recent news mention chain",
           truncate_to=3,
           truncate_keys=["ma_history", "recent_news", "peer_operators"])
def investor_brief():
    """Operator scorecard: footprint, growth, M&A history, peer comparables.

    Query params:
        operator   operator name (required) — fuzzy matched
    """
    operator = (request.args.get("operator") or "").strip()
    if not operator:
        return jsonify(ok=False,
                       error="missing 'operator' query param",
                       example="/api/v1/brief/investor?operator=Equinix"), 400

    payload = {"ok": True, "persona": "investor", "input": {"operator": operator}}
    sources = []
    errors = {}

    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # Footprint
            rows = _read(cur, """
                SELECT COUNT(*), COALESCE(SUM(power_mw), 0),
                       COUNT(DISTINCT country), COUNT(DISTINCT state)
                  FROM facilities
                 WHERE provider ILIKE %s""", errors, "footprint", (f"%{operator}%",))
            if "footprint" in errors:
                payload["footprint"] = None
            elif rows and rows[0]:
                payload["footprint"] = {
                    "facility_count": int(rows[0][0] or 0),
                    "total_mw": _as_float(rows[0][1]),
                    "countries": int(rows[0][2] or 0),
                    "us_states": int(rows[0][3] or 0),
                }
                if rows[0][0]:
                    sources.append(src(
                        f"{operator} footprint ({rows[0][0]} facilities, {rows[0][1]:.0f} MW)",
                        "facilities", now_iso()))

            # Growth: pipeline contribution
            # 2026-07-31: unguarded this reported Google as 79 projects /
            # 317,268 MW — the 150,000 MW Nevada quarantine_aggregate alone was
            # 47% of it. See util/capacity_pipeline.
            rows = _read(cur, f"""
                SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0)
                  FROM capacity_pipeline
                 WHERE operator ILIKE %s
                   AND {CP_OK}""", errors, "pipeline", (f"%{operator}%",))
            if "pipeline" in errors:
                payload["pipeline"] = None
            elif rows and rows[0]:
                payload["pipeline"] = {
                    "projects": int(rows[0][0] or 0),
                    "total_mw": _as_float(rows[0][1]),
                }

            # M&A history
            # ★ 2026-08-01 — read `transactions`, which has never existed here
            # (see the buyer brief above and #2071). `ma_history: []` therefore
            # told every caller "this operator has made no acquisitions" as a
            # fact, on every request since it shipped. Live table is `deals`.
            rows = _read(cur, f"""
                SELECT seller, buyer, value, {DEAL_DATE} AS announced_on, market, type
                  FROM deals
                 WHERE {DEALS_OK} AND (buyer ILIKE %s OR seller ILIKE %s)
                 ORDER BY announced_on DESC NULLS LAST LIMIT 15""",
                errors, "ma_history", (f"%{operator}%", f"%{operator}%"))
            ma = []
            for r in rows:
                ma.append({
                    "target": r[0], "acquirer": r[1],
                    "value_usd_millions": _as_float(r[2]),  # millions — see buyer brief
                    "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                    "market": r[4], "deal_type": r[5],
                    "role": ("acquirer" if r[1] and operator.lower() in (r[1] or '').lower()
                             else "target"),
                })
            payload["ma_history"] = None if "ma_history" in errors else ma
            if ma:
                sources.append(src(f"M&A history ({len(ma)} deals)",
                                   "deals", now_iso()))

            # Recent news mentions
            # ★ 2026-08-01 — `news.body` does not exist and never has; the
            # article text column is `description` (2,890 of 2,891 rows carry
            # it). UndefinedColumn on every request -> `recent_news: []`, and
            # under the old `with _conn()` this ALSO aborted the transaction
            # and took `peer_operators` below down with it.
            rows = _read(cur, """
                SELECT title, url, published_date, source
                  FROM news
                 WHERE title ILIKE %s OR description ILIKE %s
                 ORDER BY published_date DESC NULLS LAST LIMIT 5""",
                errors, "recent_news", (f"%{operator}%", f"%{operator}%"))
            news = []
            for r in rows:
                news.append({"title": r[0], "url": r[1],
                             "published_date": r[2].isoformat() if r[2] and hasattr(r[2], 'isoformat') else r[2],
                             "source": r[3]})
            payload["recent_news"] = None if "recent_news" in errors else news
            if news:
                sources.append(src(f"Recent news ({len(news)} articles)",
                                   "news", now_iso()))

            # Peer comparables: other top-MW operators.
            # ★ This query is and always was VALID. It returned [] anyway,
            # because the two dead reads above aborted the shared transaction
            # and it died with InFailedSqlTransaction — the #2071 shape exactly:
            # the lie and its cause sat in different sections of the response.
            rows = _read(cur, """
                SELECT provider, COUNT(*), COALESCE(SUM(power_mw), 0)
                  FROM facilities
                 WHERE provider IS NOT NULL AND provider <> ''
                   AND provider NOT ILIKE %s
                 GROUP BY provider
                 ORDER BY SUM(power_mw) DESC NULLS LAST
                 LIMIT 8""", errors, "peer_operators", (f"%{operator}%",))
            peers = []
            for r in rows:
                peers.append({"provider": r[0],
                              "facility_count": int(r[1] or 0),
                              "total_mw": _as_float(r[2])})
            payload["peer_operators"] = (
                None if "peer_operators" in errors else peers)
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    payload["drill_deeper"] = {
        "facility_search":  f"/api/v1/facilities/search?q={operator}",
        "transactions":     f"/api/v1/transactions?q={operator}",
        "news_filter":      f"/api/v1/news?q={operator}",
    }
    return _finish(payload, errors, sources)


# ─────────────────────────────────────────────────────────────────────
# POLICY BRIEF — "What's the impact on my state?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/policy", methods=["GET"])
@soft_gate(min_tier=RestTier.DEVELOPER,
           teaser="full state-level rollup with all DCPI verdict buckets + complete tax-incentive program list + jobs methodology",
           truncate_to=3,
           truncate_keys=["state_incentives", "sources"])
def policy_brief():
    """State-level rollup for policymakers / regulators.

    Query params:
        state    2-letter state code (required)
    """
    state = (request.args.get("state") or "").strip().upper()
    if not state or len(state) != 2:
        return jsonify(ok=False,
                       error="missing 'state' (2-letter code) query param",
                       example="/api/v1/brief/policy?state=VA"), 400

    payload = {"ok": True, "persona": "policy", "input": {"state": state}}
    sources = []

    errors = {}

    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # Facility footprint
            rows = _read(cur, """
                SELECT COUNT(*), COALESCE(SUM(power_mw), 0),
                       COUNT(DISTINCT provider)
                  FROM facilities
                 WHERE UPPER(state) = %s""", errors, "installed_base", (state,))
            if "installed_base" in errors:
                payload["installed_base"] = None
            elif rows and rows[0]:
                payload["installed_base"] = {
                    "facility_count": int(rows[0][0] or 0),
                    "total_operational_mw": _as_float(rows[0][1]),
                    "operator_diversity": int(rows[0][2] or 0),
                }
                if rows[0][0]:
                    sources.append(src(f"{state} installed base", "facilities", now_iso()))

            # Pipeline pressure
            # ★ 2026-08-01 — STILL DEAD, STILL DELIBERATELY UNREPAIRED (the
            # owner call recorded below stands). What changed: it no longer
            # fails SILENTLY. Two things were wrong beyond the dead column.
            # First, the failure was invisible — `pipeline_pressure` was simply
            # absent, and `impact_estimates` below then computed planned_mw
            # from a missing key as 0, publishing a DERIVED number that read as
            # "no pipeline in this state".
            #
            # ★ CORRECTION, same day, from watching the deployed fix: this
            # query was broken TWICE OVER, and the second one hid the first.
            # `LIKE '%construct%'` carried LONE percent signs in a call that
            # ALSO passes a params tuple, so psycopg2 tried to interpolate
            # `%c` and raised `IndexError: tuple index out of range` — the
            # exact trap util/capacity_pipeline documents — CLIENT-SIDE. The
            # statement never reached Postgres, which means the dead `state`
            # column was never even evaluated and the transaction was never
            # poisoned. So an earlier note here claiming this read zeroed
            # `grid_stress` was WRONG: the cascade reproduces on a hand-written
            # version of this query, not on the shipped one, and grid_stress
            # was returning AVOID: 19 in production all along. The real
            # cascade in this file is investor_brief's (`transactions` is a
            # dead TABLE with no lone %, so it does reach Postgres, does
            # poison, and did zero `peer_operators`) — measured 2026-08-01.
            # Percent signs escaped now, so the published error names the
            # actual data problem instead of a misleading client-side
            # IndexError.
            # ★ 2026-07-31 audit — THIS QUERY IS DEAD AND HAS ALWAYS BEEN DEAD.
            # `capacity_pipeline` has no `state` column (verified against
            # information_schema on the read replica: the location columns are
            # market / region / country). It raises UndefinedColumn, the read
            # helper swallowed it, and `pipeline_pressure` was never set — so
            # this brief has silently shipped without the block it advertises.
            # NOT auto-repaired, deliberately: the only surviving predicate is
            # `market ILIKE '%<state>%'`, which for VA also matches Nevada and
            # Savannah. Enabling that would publish a new figure computed by an
            # unreviewed fuzzy match — a data-modelling call for the owner, not
            # a mechanical fix. The guard is present so that whoever picks the
            # real predicate cannot reintroduce the unfiltered sum.
            rows = _read(cur, f"""
                SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0),
                       COUNT(*) FILTER (WHERE LOWER(COALESCE(phase, status, ''))
                                              LIKE '%%construct%%') AS under_const,
                       COALESCE(SUM(capacity_mw) FILTER (WHERE
                            LOWER(COALESCE(phase, status, '')) LIKE '%%construct%%'), 0)
                            AS under_const_mw
                  FROM capacity_pipeline
                 WHERE (UPPER(COALESCE(state, '')) = %s
                    OR market ILIKE %s)
                   AND {CP_OK}""",
                errors, "pipeline_pressure",
                (state, f"%{state.lower()}%"))
            if "pipeline_pressure" in errors:
                payload["pipeline_pressure"] = None
            elif rows and rows[0]:
                payload["pipeline_pressure"] = {
                    "projects": int(rows[0][0] or 0),
                    "total_planned_mw": _as_float(rows[0][1]),
                    "under_construction_projects": int(rows[0][2] or 0),
                    "under_construction_mw": _as_float(rows[0][3]),
                }

            # Grid stress via DCPI rollup. VALID QUERY — it was returning {}
            # only because pipeline_pressure above aborted the transaction.
            rows = _read(cur, """
                SELECT verdict, COUNT(DISTINCT market_slug),
                       AVG(excess_power_score), AVG(constraint_score),
                       AVG(time_to_power_months)
                  FROM market_power_scores
                 WHERE UPPER(state) = %s
                 GROUP BY verdict""", errors, "grid_stress", (state,))
            grid = {"by_verdict": {}, "avg_excess": None,
                    "avg_constraint": None, "avg_ttp": None}
            ex_n = ex_sum = co_n = co_sum = tt_n = tt_sum = 0
            for r in rows:
                cnt = int(r[1] or 0)
                grid["by_verdict"][r[0] or 'UNKNOWN'] = cnt
                if r[2] is not None:
                    ex_sum += float(r[2]) * cnt; ex_n += cnt
                if r[3] is not None:
                    co_sum += float(r[3]) * cnt; co_n += cnt
                if r[4] is not None:
                    tt_sum += float(r[4]) * cnt; tt_n += cnt
            if ex_n: grid["avg_excess"] = round(ex_sum / ex_n, 1)
            if co_n: grid["avg_constraint"] = round(co_sum / co_n, 1)
            if tt_n: grid["avg_ttp"] = round(tt_sum / tt_n, 1)
            payload["grid_stress"] = None if "grid_stress" in errors else grid
            if grid["by_verdict"]:
                sources.append(src(f"{state} DCPI rollup", "market_power_scores", now_iso()))

            # Tax incentive policy
            # ★ 2026-08-01 — read `tax_incentives`, which does not exist. The
            # live table is `tax_incentives_neon` (50 rows, one per state;
            # registered as canonical in routes/brain_rag.py) and it is keyed
            # `state_abbr`, not `state`. So `state_incentives: []` published
            # "this state offers no incentives" for all 50 states, on an
            # endpoint whose paywall teaser sells a "complete tax-incentive
            # program list". Column names differ from the dead query's
            # entirely; mapped to what the table actually holds.
            rows = _read(cur, """
                SELECT state_name, incentive_details, qualifying_investment,
                       qualifying_jobs, duration_years, max_benefit,
                       sales_tax_exempt, property_tax_abatement,
                       data_center_specific, source_url
                  FROM tax_incentives_neon
                 WHERE UPPER(state_abbr) = %s
                 LIMIT 10""", errors, "state_incentives", (state,))
            incentives = [
                {"state_name": r[0],
                 "details": (r[1] or '')[:400],
                 "qualifying_investment": r[2],
                 "qualifying_jobs": r[3],
                 "duration_years": r[4],
                 "max_benefit": r[5],
                 "sales_tax_exempt": r[6],
                 "property_tax_abatement": r[7],
                 "data_center_specific": r[8],
                 "source_url": r[9]}
                for r in rows]
            payload["state_incentives"] = (
                None if "state_incentives" in errors else incentives)
            if incentives:
                sources.append(src(f"{state} tax incentives",
                                   "tax_incentives_neon", now_iso()))

            # Economic snapshot — derived numbers (best-effort).
            # ★ A DERIVED NUMBER INHERITS ITS INPUTS' UNCERTAINTY. This block
            # used `(payload.get(...) or {}).get(..., 0)`, so a section that
            # FAILED silently became a 0 MW input and the jobs figure came out
            # as a confident number computed from data we never read. If an
            # input is unknown the estimate is null and says which input.
            ib = payload.get("installed_base")
            pp = payload.get("pipeline_pressure")
            missing = [k for k, v in (("installed_base", ib),
                                      ("pipeline_pressure", pp)) if not v]
            if missing:
                payload["impact_estimates"] = None
                payload["impact_estimates_note"] = (
                    "null because " + ", ".join(missing) + " could not be "
                    "read — see query_errors. A jobs estimate derived from an "
                    "unread input would be a fabricated number, not a zero.")
            else:
                mw_total = (ib.get("total_operational_mw") or 0)
                mw_pipeline = (pp.get("total_planned_mw") or 0)
                payload["impact_estimates"] = {
                    "operational_mw": mw_total,
                    "planned_mw": mw_pipeline,
                    "estimated_jobs_supported": int((mw_total + mw_pipeline * 0.3) * 1.5),
                    "note": ("Jobs estimate uses 1.5 FTEs per operational MW + 0.5x weight on "
                             "planned MW (industry-standard heuristic). Not authoritative."),
                }
    except Exception as e:
        payload["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    payload["drill_deeper"] = {
        "iso_snapshot":     f"/api/v1/iso/<iso>/snapshot",
        "facility_search":  f"/api/v1/facilities/search?state={state}",
        "tax_incentives":   f"/api/v1/tax-incentives?state={state}",
    }
    return _finish(payload, errors, sources)
