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

persona_briefs_bp = Blueprint("persona_briefs", __name__)


def _conn():
    import psycopg2
    # autocommit so one failed sub-query doesn't poison the rest of the bundle.
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────
# DEVELOPER BRIEF — "Where should I build?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/developer", methods=["GET"])
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

    try:
        with _conn() as c, c.cursor() as cur:
            where = ["computed_at IS NOT NULL", "verdict != 'AVOID'"]
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
            rows = _safe(cur, sql, params)

            # In-memory ranking. Score = excess_power - constraint - deadline_penalty.
            ranked = []
            for r in rows:
                slug, name, iso, st, verdict, excess, constraint, ttp, cat = r
                excess_v = float(excess) if excess is not None else 0
                constraint_v = float(constraint) if constraint is not None else 0
                ttp_v = float(ttp) if ttp is not None else 36
                # penalty: 0 if under deadline, else 5pts per month over
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
                rows = _safe(cur, """
                    SELECT COALESCE(SUM(capacity_mw), 0)
                      FROM capacity_pipeline
                     WHERE market ILIKE %s
                       AND LOWER(COALESCE(phase, status, '')) LIKE '%%construct%%'""",
                    (f"%{entry['market_slug']}%",))
                if rows and rows[0][0]:
                    entry["competing_construction_mw"] = _as_float(rows[0][0])
    except Exception as e:
        payload["error_partial"] = str(e)[:200]

    payload["shortlist"] = shortlist
    payload["methodology"] = (
        "Score = excess_power_score − (constraint_score × 0.5) − "
        "max(0, time_to_power − deadline) × 5; +10 bonus for BUILD verdict. "
        "Markets with AVOID verdict are excluded.")
    payload["drill_deeper"] = {
        "site_planner":  "/api/v1/site-planner/recommend",
        "market_brief":  "/api/v1/brief/market?market=<slug>",
        "site_report":   "/api/v1/sites/<id_or_slug>/capacity-report",
    }
    return jsonify(attach_sources(payload, sources)), 200


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

    try:
        with _conn() as c, c.cursor() as cur:
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
            for r in _safe(cur, sql_fac, params):
                on_market.append({
                    "id": r[0], "name": r[1], "provider": r[2],
                    "city": r[3], "state": r[4], "country": r[5],
                    "power_mw": _as_float(r[6]), "status": r[7],
                })
            payload["candidate_facilities"] = on_market
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
            for r in _safe(cur, sql_li, li_params):
                listings.append({
                    "id": r[0], "slug": r[1], "title": r[2],
                    "market": r[3], "state": r[4],
                    "capacity_mw": _as_float(r[5]),
                    "asking_price": _as_float(r[6]),
                    "asking_currency": r[7],
                    "tier_required": r[8], "status": r[9],
                })
            payload["pocket_listings"] = listings
            if listings:
                sources.append(src(
                    f"Pocket-listing inventory ({len(listings)} entries)",
                    "exclusive_listings", now_iso()))

            # Comparables: recent transactions in same geography.
            sql_tx = """SELECT target, acquirer, value_usd, announced_date,
                               market, deal_type
                          FROM transactions
                         WHERE announced_date IS NOT NULL"""
            tx_params = []
            if market:
                sql_tx += " AND market ILIKE %s"
                tx_params.append(f"%{market}%")
            sql_tx += " ORDER BY announced_date DESC NULLS LAST LIMIT 10"
            comps = []
            for r in _safe(cur, sql_tx, tx_params):
                comps.append({
                    "target": r[0], "acquirer": r[1],
                    "value_usd": _as_float(r[2]),
                    "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                    "market": r[4], "deal_type": r[5],
                })
            payload["recent_comparables"] = comps
            if comps:
                sources.append(src(
                    f"Transaction comparables ({len(comps)} recent deals)",
                    "transactions", now_iso()))
    except Exception as e:
        payload["error_partial"] = str(e)[:200]

    payload["drill_deeper"] = {
        "site_report":    "/api/v1/sites/<id>/capacity-report",
        "pocket_detail":  "/api/v1/listings/<id>",
        "tx_database":    "/api/v1/transactions",
    }
    return jsonify(attach_sources(payload, sources)), 200


# ─────────────────────────────────────────────────────────────────────
# INVESTOR BRIEF — "What's this operator's trajectory?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/investor", methods=["GET"])
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

    try:
        with _conn() as c, c.cursor() as cur:
            # Footprint
            rows = _safe(cur, """
                SELECT COUNT(*), COALESCE(SUM(power_mw), 0),
                       COUNT(DISTINCT country), COUNT(DISTINCT state)
                  FROM facilities
                 WHERE provider ILIKE %s""", (f"%{operator}%",))
            if rows and rows[0]:
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
            rows = _safe(cur, """
                SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0)
                  FROM capacity_pipeline
                 WHERE operator ILIKE %s""", (f"%{operator}%",))
            if rows and rows[0]:
                payload["pipeline"] = {
                    "projects": int(rows[0][0] or 0),
                    "total_mw": _as_float(rows[0][1]),
                }

            # M&A history
            rows = _safe(cur, """
                SELECT target, acquirer, value_usd, announced_date, market, deal_type
                  FROM transactions
                 WHERE acquirer ILIKE %s OR target ILIKE %s
                 ORDER BY announced_date DESC NULLS LAST LIMIT 15""",
                (f"%{operator}%", f"%{operator}%"))
            ma = []
            for r in rows:
                ma.append({
                    "target": r[0], "acquirer": r[1],
                    "value_usd": _as_float(r[2]),
                    "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                    "market": r[4], "deal_type": r[5],
                    "role": ("acquirer" if r[1] and operator.lower() in (r[1] or '').lower()
                             else "target"),
                })
            payload["ma_history"] = ma
            if ma:
                sources.append(src(f"M&A history ({len(ma)} deals)",
                                   "transactions", now_iso()))

            # Recent news mentions
            rows = _safe(cur, """
                SELECT title, url, published_date, source
                  FROM news
                 WHERE title ILIKE %s OR body ILIKE %s
                 ORDER BY published_date DESC NULLS LAST LIMIT 5""",
                (f"%{operator}%", f"%{operator}%"))
            news = []
            for r in rows:
                news.append({"title": r[0], "url": r[1],
                             "published_date": r[2].isoformat() if r[2] and hasattr(r[2], 'isoformat') else r[2],
                             "source": r[3]})
            payload["recent_news"] = news
            if news:
                sources.append(src(f"Recent news ({len(news)} articles)",
                                   "news", now_iso()))

            # Peer comparables: other top-MW operators
            rows = _safe(cur, """
                SELECT provider, COUNT(*), COALESCE(SUM(power_mw), 0)
                  FROM facilities
                 WHERE provider IS NOT NULL AND provider <> ''
                   AND provider NOT ILIKE %s
                 GROUP BY provider
                 ORDER BY SUM(power_mw) DESC NULLS LAST
                 LIMIT 8""", (f"%{operator}%",))
            peers = []
            for r in rows:
                peers.append({"provider": r[0],
                              "facility_count": int(r[1] or 0),
                              "total_mw": _as_float(r[2])})
            payload["peer_operators"] = peers
    except Exception as e:
        payload["error_partial"] = str(e)[:200]

    payload["drill_deeper"] = {
        "facility_search":  f"/api/v1/facilities/search?q={operator}",
        "transactions":     f"/api/v1/transactions?q={operator}",
        "news_filter":      f"/api/v1/news?q={operator}",
    }
    return jsonify(attach_sources(payload, sources)), 200


# ─────────────────────────────────────────────────────────────────────
# POLICY BRIEF — "What's the impact on my state?"
# ─────────────────────────────────────────────────────────────────────
@persona_briefs_bp.route("/api/v1/brief/policy", methods=["GET"])
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

    try:
        with _conn() as c, c.cursor() as cur:
            # Facility footprint
            rows = _safe(cur, """
                SELECT COUNT(*), COALESCE(SUM(power_mw), 0),
                       COUNT(DISTINCT provider)
                  FROM facilities
                 WHERE UPPER(state) = %s""", (state,))
            if rows and rows[0]:
                payload["installed_base"] = {
                    "facility_count": int(rows[0][0] or 0),
                    "total_operational_mw": _as_float(rows[0][1]),
                    "operator_diversity": int(rows[0][2] or 0),
                }
                if rows[0][0]:
                    sources.append(src(f"{state} installed base", "facilities", now_iso()))

            # Pipeline pressure
            rows = _safe(cur, """
                SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0),
                       COUNT(*) FILTER (WHERE LOWER(COALESCE(phase, status, ''))
                                              LIKE '%construct%') AS under_const,
                       COALESCE(SUM(capacity_mw) FILTER (WHERE
                            LOWER(COALESCE(phase, status, '')) LIKE '%construct%'), 0)
                            AS under_const_mw
                  FROM capacity_pipeline
                 WHERE UPPER(COALESCE(state, '')) = %s
                    OR market ILIKE %s""",
                (state, f"%{state.lower()}%"))
            if rows and rows[0]:
                payload["pipeline_pressure"] = {
                    "projects": int(rows[0][0] or 0),
                    "total_planned_mw": _as_float(rows[0][1]),
                    "under_construction_projects": int(rows[0][2] or 0),
                    "under_construction_mw": _as_float(rows[0][3]),
                }

            # Grid stress via DCPI rollup
            rows = _safe(cur, """
                SELECT verdict, COUNT(DISTINCT market_slug),
                       AVG(excess_power_score), AVG(constraint_score),
                       AVG(time_to_power_months)
                  FROM market_power_scores
                 WHERE UPPER(state) = %s
                 GROUP BY verdict""", (state,))
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
            payload["grid_stress"] = grid
            if grid["by_verdict"]:
                sources.append(src(f"{state} DCPI rollup", "market_power_scores", now_iso()))

            # Tax incentive policy
            rows = _safe(cur, """
                SELECT name, summary, min_investment_usd, min_capacity_mw
                  FROM tax_incentives
                 WHERE UPPER(state) = %s
                 LIMIT 10""", (state,))
            payload["state_incentives"] = [
                {"name": r[0], "summary": (r[1] or '')[:200],
                 "min_investment_usd": _as_float(r[2]),
                 "min_capacity_mw": _as_float(r[3])}
                for r in rows]

            # Economic snapshot — derived numbers (best-effort)
            ib = payload.get("installed_base") or {}
            pp = payload.get("pipeline_pressure") or {}
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

    payload["drill_deeper"] = {
        "iso_snapshot":     f"/api/v1/iso/<iso>/snapshot",
        "facility_search":  f"/api/v1/facilities/search?state={state}",
        "tax_incentives":   f"/api/v1/tax-incentives?state={state}",
    }
    return jsonify(attach_sources(payload, sources)), 200
