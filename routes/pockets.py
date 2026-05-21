"""Phase r27-r30 (2026-05-20) — Pockets of Power surface.
==========================================================================

Makes DCPI autonomous from the *user's* perspective: ranked
data-center-friendly markets ("pockets of power") delivered as a
public page, a tier-gated API, and personalized recommendations.

Builds on the existing `market_power_scores` table — same data that
already powers /digest, the developer brief, and the DCPI weekly
email — but exposes it as a first-class surface rather than a buried
asset.

Surfaces:
  GET  /api/v1/pockets/top            — top N pockets, tier-gated by count
  GET  /api/v1/pockets/for-me         — personalized: ?target_mw= &iso= &state= &within=
  GET  /api/v1/pockets/movers         — 7-day score deltas (top up + top down)
  GET  /pockets                       — public HTML page (Bloomberg-style)
  GET  /api/v1/pockets/health         — smoke endpoint

Tier gates (mirror /api/v1/map):
  anonymous → top 3
  identified → top 10
  developer  → top 50
  pro/founding/enterprise → unlimited

Cache: in-process 5min TTL so dashboard polling doesn't hammer Neon.
"""
import os
import time
import json
import logging
import datetime
from flask import Blueprint, jsonify, request, Response, render_template_string

logger = logging.getLogger(__name__)
pockets_bp = Blueprint("pockets", __name__)


def _get_db():
    """Reuse main.py's connection pool."""
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception as e:
        logger.warning(f"pockets: pg connection failed: {e}")
        return None


def _return_db(conn):
    try:
        from main import return_pg_connection
        return_pg_connection(conn)
    except Exception:
        try: conn.close()
        except Exception: pass


def _detect_tier():
    """Mirror map_tier_gating._detect_caller_tier. Returns one of:
    'anonymous' | 'identified' | 'developer' | 'pro' | 'founding' | 'enterprise'.
    Falls back to anonymous on any error — never raises out of a tier check."""
    try:
        from map_tier_gating import _detect_caller_tier
        # Reuse the same JWT decoder the energy endpoint uses
        def _decode(_t):
            try:
                import jwt as _jwt
                from main import JWT_SECRET
                return _jwt.decode(_t, JWT_SECRET, algorithms=['HS256'])
            except Exception:
                return None
        tier, _ = _detect_caller_tier(decode_jwt_func=_decode)
        return (tier or "anonymous").lower()
    except Exception as e:
        logger.warning(f"pockets: tier detect failed: {e}")
        return "anonymous"


_TIER_LIMITS = {
    "anonymous": 3,
    "free":      5,
    "identified": 10,
    "developer": 50,
    "pro":       9999,
    "founding":  9999,
    "enterprise": 9999,
    "internal":  9999,
}


def _limit_for_tier(tier: str) -> int:
    return _TIER_LIMITS.get(tier, 3)


# ── In-process cache ─────────────────────────────────────────────────
_CACHE: dict = {"data": None, "expires_at": 0.0}
_CACHE_TTL = 300  # 5 min


def _fetch_pockets(limit_hint: int = 100) -> list[dict]:
    """Pull latest market_power_scores, compute rank score, return
    sorted (descending). Limit_hint is a fetch ceiling — final slicing
    happens at the tier-gate layer."""
    now = time.time()
    if _CACHE["data"] is not None and _CACHE["expires_at"] > now:
        return _CACHE["data"]

    rows: list[dict] = []
    conn = _get_db()
    if conn is None:
        return rows
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, iso, state, verdict,
                   excess_power_score, constraint_score,
                   time_to_power_months, computed_at
              FROM market_power_scores
             ORDER BY market_slug, computed_at DESC
        """)
        raw = cur.fetchall()

        # 7d-ago lookup so each pocket can carry its delta
        delta_map: dict[str, float] = {}
        try:
            cur.execute("""
                SELECT DISTINCT ON (market_slug)
                       market_slug, excess_power_score
                  FROM market_power_scores
                 WHERE computed_at < NOW() - INTERVAL '7 days'
                 ORDER BY market_slug, computed_at DESC
            """)
            for r in cur.fetchall():
                delta_map[r[0]] = float(r[1] or 0)
        except Exception:
            try: conn.rollback()
            except Exception: pass

        for r in raw:
            slug, name, iso, state, verdict, excess, constraint, ttp, cat = r
            excess_v = float(excess or 0)
            constraint_v = float(constraint or 0)
            ttp_v = float(ttp or 36)
            # Composite rank: excess - constraint/2 - ttp_penalty + verdict bonus
            ttp_penalty = max(0, ttp_v - 24) * 2  # penalize beyond 24mo TTP
            score = excess_v - (constraint_v * 0.5) - ttp_penalty
            if verdict == "BUILD":
                score += 10
            elif verdict == "AVOID":
                score -= 20

            prev = delta_map.get(slug)
            delta_7d = None
            if prev is not None:
                delta_7d = round(excess_v - prev, 1)

            rows.append({
                "market_slug": slug,
                "market_name": name,
                "iso": iso,
                "state": state,
                "verdict": verdict,
                "excess_power_score": round(excess_v, 1),
                "constraint_score": round(constraint_v, 1),
                "time_to_power_months": round(ttp_v, 0),
                "rank_score": round(score, 1),
                "delta_7d": delta_7d,
                "computed_at": cat.isoformat() if cat else None,
            })
    except Exception as e:
        logger.warning(f"pockets: fetch failed: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        _return_db(conn)

    rows.sort(key=lambda x: -x["rank_score"])
    rows = rows[:limit_hint]

    _CACHE["data"] = rows
    _CACHE["expires_at"] = now + _CACHE_TTL
    return rows


def _rationale(p: dict) -> str:
    """Human-readable why-this-pocket sentence."""
    parts = []
    if p["verdict"] == "BUILD":
        parts.append("DCPI verdict: BUILD")
    elif p["verdict"] == "AVOID":
        parts.append("DCPI verdict: AVOID")
    if p["excess_power_score"] >= 75:
        parts.append(f"strong excess capacity ({p['excess_power_score']:.0f})")
    elif p["excess_power_score"] >= 55:
        parts.append(f"adequate excess capacity ({p['excess_power_score']:.0f})")
    if p["constraint_score"] >= 60:
        parts.append(f"high grid constraints ({p['constraint_score']:.0f})")
    if p["time_to_power_months"] and p["time_to_power_months"] <= 18:
        parts.append(f"fast TTP ({int(p['time_to_power_months'])}mo)")
    elif p["time_to_power_months"] and p["time_to_power_months"] > 36:
        parts.append(f"slow TTP ({int(p['time_to_power_months'])}mo)")
    if p["delta_7d"] is not None:
        if p["delta_7d"] >= 5:
            parts.append(f"trending up (+{p['delta_7d']:.1f} pts/7d)")
        elif p["delta_7d"] <= -5:
            parts.append(f"trending down ({p['delta_7d']:+.1f} pts/7d)")
    return "; ".join(parts) if parts else "neutral signal"


# ─── Endpoints ───────────────────────────────────────────────────────

@pockets_bp.route("/api/v1/pockets/top", methods=["GET"])
def pockets_top():
    """Top pockets. Tier-gated by count.
    Query: ?limit= (capped by tier), ?state= (filter)."""
    tier = _detect_tier()
    limit_arg = request.args.get("limit", type=int) or 50
    state_filter = (request.args.get("state") or "").strip().upper()

    cap = _limit_for_tier(tier)
    effective_limit = min(limit_arg, cap)

    rows = _fetch_pockets(limit_hint=500)
    if state_filter:
        rows = [r for r in rows if (r["state"] or "").upper() == state_filter]

    truncated = max(0, len(rows) - effective_limit)
    rows = rows[:effective_limit]

    for r in rows:
        r["why"] = _rationale(r)

    payload = {
        "ok": True,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "caller_tier": tier,
        "tier_cap": cap,
        "shown": len(rows),
        "truncated_by_tier": truncated,
        "filter": {"state": state_filter or None},
        "pockets": rows,
    }
    if truncated > 0 and tier in ("anonymous", "free", "identified"):
        payload["upgrade"] = {
            "label": "Upgrade to see all pockets",
            "url":   "/pricing?from=pockets",
            "tier_needed": "developer" if tier != "developer" else "pro",
        }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


@pockets_bp.route("/api/v1/pockets/for-me", methods=["GET"])
def pockets_for_me():
    """Personalized ranking. Query-param filters re-rank pockets by fit.

    Query:
      target_mw    int — workload size, used to weight excess_power higher
      iso          str — preferred ISO (PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE)
      state        str — preferred state (filter, not weight)
      within_ttp   int — max acceptable months-to-power (filter)
      workload     str — 'ai_training' | 'inference' | 'colo' (weights excess vs ttp)

    Tier-gated count limits apply just like /top.
    """
    tier = _detect_tier()
    cap = _limit_for_tier(tier)

    # r33 (2026-05-20): query params win, but fall back to the user's
    # saved profile preferences. Logged-in users get personalized
    # rankings without specifying anything; query params override
    # for one-off "what if" exploration.
    profile = _load_preferences(_auth_user_id()) or {}
    target_mw = request.args.get("target_mw", type=int) or int(profile.get("target_mw") or 0)
    iso_pref = (request.args.get("iso") or profile.get("iso") or "").strip().upper()
    state_pref = (request.args.get("state") or profile.get("state") or "").strip().upper()
    within_ttp = request.args.get("within_ttp", type=int) or int(profile.get("within_ttp") or 0)
    workload = (request.args.get("workload") or profile.get("workload") or "").strip().lower()
    source_of_prefs = "query+profile" if profile else "query"

    rows = _fetch_pockets(limit_hint=500)

    # Filter
    if state_pref:
        rows = [r for r in rows if (r["state"] or "").upper() == state_pref]
    if within_ttp:
        rows = [r for r in rows
                if not r["time_to_power_months"]
                or r["time_to_power_months"] <= within_ttp]

    # Re-rank with personalization weights. Start from the base rank_score
    # and add bonuses for fit.
    def _personal_score(r):
        s = r["rank_score"]
        # ISO match: +15 if the caller specified an ISO and this row matches
        if iso_pref and (r["iso"] or "").upper() == iso_pref:
            s += 15
        # Large-MW workload: weight excess_power harder
        if target_mw >= 100:
            s += (r["excess_power_score"] - 50) * 0.3  # amplify excess vs neutral
        # Workload preferences:
        if workload == "ai_training":
            # AI training tolerates higher latency; prioritize cheap+available power
            s += (r["excess_power_score"] - 60) * 0.2
        elif workload == "inference":
            # Latency-sensitive; favor mature ISOs (PJM, ERCOT, CAISO get a +5)
            if (r["iso"] or "").upper() in {"PJM", "ERCOT", "CAISO", "NYISO", "ISO-NE"}:
                s += 5
        elif workload == "colo":
            # Standard colo cares about both, slight ttp tiebreaker
            if r["time_to_power_months"] and r["time_to_power_months"] <= 24:
                s += 3
        # Movers bonus: trending-up pockets get a small nudge
        if r["delta_7d"] and r["delta_7d"] >= 5:
            s += 2
        return s

    for r in rows:
        r["personal_score"] = round(_personal_score(r), 1)
    rows.sort(key=lambda x: -x["personal_score"])

    truncated = max(0, len(rows) - cap)
    rows = rows[:cap]
    for r in rows:
        r["why"] = _rationale(r)

    payload = {
        "ok": True,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "caller_tier": tier,
        "tier_cap": cap,
        "shown": len(rows),
        "truncated_by_tier": truncated,
        "preference": {
            "target_mw": target_mw or None,
            "iso":       iso_pref or None,
            "state":     state_pref or None,
            "within_ttp": within_ttp or None,
            "workload":  workload or None,
            "source":    source_of_prefs,
        },
        "pockets": rows,
        "methodology": (
            "Base score = excess_power − (constraint × 0.5) − ttp_penalty "
            "(+10 BUILD bonus, −20 AVOID penalty). Personal score adds: "
            "+15 ISO match, +0.3 × (excess − 50) for ≥100MW workloads, "
            "workload-specific weights for ai_training/inference/colo, "
            "+2 for pockets trending up ≥5pts/7d."
        ),
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "private, max-age=120"
    return resp, 200


@pockets_bp.route("/api/v1/pockets/movers", methods=["GET"])
def pockets_movers():
    """7-day biggest movers. Public, tier-gated count."""
    tier = _detect_tier()
    cap = _limit_for_tier(tier)

    rows = _fetch_pockets(limit_hint=500)
    with_delta = [r for r in rows if r["delta_7d"] is not None]

    up_movers = sorted(with_delta, key=lambda r: -(r["delta_7d"] or 0))[:cap]
    down_movers = sorted(with_delta, key=lambda r: (r["delta_7d"] or 0))[:cap]

    payload = {
        "ok": True,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "caller_tier": tier,
        "shown_up": len(up_movers),
        "shown_down": len(down_movers),
        "rising": up_movers,
        "falling": down_movers,
    }
    return jsonify(payload), 200


@pockets_bp.route("/api/v1/pockets/health", methods=["GET", "HEAD"])
def pockets_health():
    rows = _fetch_pockets(limit_hint=10)
    return jsonify(
        ok=True,
        pockets_cached=len(_CACHE.get("data") or []),
        cache_expires_in=max(0, int(_CACHE["expires_at"] - time.time())),
        sample=[{"market": r["market_name"], "score": r["rank_score"]} for r in rows[:3]],
    ), 200


# ─── Public HTML page ────────────────────────────────────────────────

_POCKETS_PAGE_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Pockets of Power · DC Hub</title>
<meta name="description" content="Live ranking of data-center-friendly markets by excess power, grid constraint, and time-to-power. Updated daily.">
<meta property="og:title" content="Pockets of Power — DC Hub">
<meta property="og:description" content="The {{ shown }} best US/global markets for new data center capacity, ranked by live grid data.">
<meta property="og:url" content="https://dchub.cloud/pockets">
<link rel="canonical" href="https://dchub.cloud/pockets">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--green:#10b981;--orange:#f59e0b;--red:#ef4444;--acc:#6366f1;--violet:#8b5cf6}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem}
.kicker{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.5rem}
h1{font-size:2.8rem;margin:0 0 0.5rem;font-weight:800;letter-spacing:-0.02em;background:linear-gradient(90deg,#fff,#a78bfa);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--tx2);margin:0 0 2rem;font-size:1.05rem;max-width:720px}
.callout{background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(139,92,246,0.08));border:1px solid rgba(139,92,246,0.3);border-radius:12px;padding:1.25rem 1.5rem;margin:0 0 2rem;font-size:0.95rem;color:#ddd}
.callout b{color:#fff}
h2{font-size:0.82rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.12em;margin:2.5rem 0 1rem;font-weight:700}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
th{text-align:left;padding:0.85rem 1rem;background:#0f1019;color:var(--tx2);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.1em;font-weight:700;border-bottom:1px solid var(--bd)}
td{padding:0.85rem 1rem;border-bottom:1px solid var(--bd);font-size:0.95rem}
tr:last-child td{border-bottom:none}
tr:hover{background:rgba(99,102,241,0.04)}
.rank{font-family:'JetBrains Mono',monospace;color:var(--violet);font-weight:700;width:48px}
.market{font-weight:600}
.market a{color:var(--tx);text-decoration:none;border-bottom:1px dotted rgba(255,255,255,0.2)}
.market a:hover{color:var(--acc);border-bottom-color:var(--acc)}
.score{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.05rem}
.verdict{font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em;font-weight:700;padding:2px 8px;border-radius:4px;display:inline-block}
.verdict.BUILD{background:rgba(16,185,129,0.15);color:var(--green)}
.verdict.HOLD{background:rgba(245,158,11,0.15);color:var(--orange)}
.verdict.AVOID{background:rgba(239,68,68,0.15);color:var(--red)}
.delta{font-family:'JetBrains Mono',monospace;font-size:0.85rem;font-weight:600}
.delta.up{color:var(--green)}
.delta.down{color:var(--red)}
.delta.flat{color:var(--tx2)}
.iso{font-family:'JetBrains Mono',monospace;font-size:0.8rem;color:var(--tx2)}
.why{color:var(--tx2);font-size:0.85rem;max-width:340px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:1.5rem 0 2.5rem}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1rem 1.25rem}
.stat .n{font-family:'JetBrains Mono',monospace;font-size:1.7rem;font-weight:800}
.stat .l{color:var(--tx2);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.25rem}
.upgrade{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border-radius:12px;padding:1.5rem;margin:2.5rem 0;text-align:center}
.upgrade a{display:inline-block;margin-top:0.5rem;color:#fff;background:rgba(255,255,255,0.15);padding:0.6rem 1.2rem;border-radius:8px;text-decoration:none;font-weight:600;border:1px solid rgba(255,255,255,0.3)}
.upgrade a:hover{background:rgba(255,255,255,0.25)}
footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--bd);color:var(--tx2);font-size:0.85rem}
footer a{color:var(--acc)}
@media(max-width:740px){
  .why{display:none}
  h1{font-size:2rem}
  .wrap{padding:2rem 1rem}
}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB · LIVE MARKETS · {{ as_of[:10] }}</div>
<h1>Pockets of Power</h1>
<p class="sub">Where to put new data center capacity, ranked by excess power, grid constraint, and time-to-power. Updated daily from EIA + ISO + DCPI signals.</p>
{% if top_mover %}
<div class="callout"><b>{{ top_mover.market_name }}</b> ({{ top_mover.iso }}, {{ top_mover.state }}) is the biggest mover this week — <span class="delta {{ 'up' if top_mover.delta_7d > 0 else 'down' }}">{{ '+' if top_mover.delta_7d > 0 else '' }}{{ top_mover.delta_7d }}</span> on the excess-power index. {{ top_mover.why }}.</div>
{% endif %}
<div class="stats">
<div class="stat"><div class="n">{{ shown }}</div><div class="l">Pockets ranked</div></div>
<div class="stat"><div class="n green">{{ build_count }}</div><div class="l">BUILD verdict</div></div>
<div class="stat"><div class="n red">{{ avoid_count }}</div><div class="l">AVOID verdict</div></div>
<div class="stat"><div class="n">{{ caller_tier }}</div><div class="l">Your tier</div></div>
</div>
<h2>Top {{ shown }} Pockets</h2>
<table><thead><tr>
<th>#</th><th>Market</th><th>ISO</th><th>Score</th><th>Excess</th><th>Verdict</th><th>7d Δ</th><th>TTP</th><th class="why-h">Why</th>
</tr></thead><tbody>
{% for p in pockets %}
<tr>
<td class="rank">{{ loop.index }}</td>
<td class="market"><a href="/pockets/{{ p.market_slug }}">{{ p.market_name }}</a><br><span class="iso">{{ p.state or '—' }}</span></td>
<td class="iso">{{ p.iso or '—' }}</td>
<td class="score">{{ p.rank_score }}</td>
<td class="score">{{ p.excess_power_score }}</td>
<td><span class="verdict {{ p.verdict or 'HOLD' }}">{{ p.verdict or 'HOLD' }}</span></td>
<td class="delta {% if p.delta_7d and p.delta_7d > 0 %}up{% elif p.delta_7d and p.delta_7d < 0 %}down{% else %}flat{% endif %}">{% if p.delta_7d is none %}—{% else %}{{ '+' if p.delta_7d > 0 else '' }}{{ p.delta_7d }}{% endif %}</td>
<td class="iso">{{ p.time_to_power_months|int if p.time_to_power_months else '—' }}mo</td>
<td class="why">{{ p.why }}</td>
</tr>
{% endfor %}
</tbody></table>
{% if truncated_by_tier > 0 %}
<div class="upgrade">
<div><b>{{ truncated_by_tier }} more pockets</b> are ranked but hidden at your tier.</div>
<a href="{{ upgrade_url }}">Upgrade to see them →</a>
</div>
{% endif %}
<footer>
Methodology: composite of EIA retail rates, ISO grid headroom, DCPI verdict, time-to-power.
Data refreshed daily. <a href="/digest">Daily brief →</a> · <a href="/api/v1/pockets/top">JSON</a> · <a href="/pockets.rss">RSS</a> · <a href="/dcpi">DCPI index</a>
</footer>
</div></body></html>'''


@pockets_bp.route("/pockets", methods=["GET"])
def pockets_page():
    tier = _detect_tier()
    cap = _limit_for_tier(tier)
    state_filter = (request.args.get("state") or "").strip().upper()

    rows = _fetch_pockets(limit_hint=500)
    if state_filter:
        rows = [r for r in rows if (r["state"] or "").upper() == state_filter]
    total_known = len(rows)
    rows = rows[:cap]
    for r in rows:
        r["why"] = _rationale(r)

    # Top mover for the callout
    movers = [r for r in rows if r["delta_7d"] is not None]
    top_mover = None
    if movers:
        top_mover = max(movers, key=lambda r: abs(r["delta_7d"] or 0))

    build_count = sum(1 for r in rows if r["verdict"] == "BUILD")
    avoid_count = sum(1 for r in rows if r["verdict"] == "AVOID")

    upgrade_url = "/pricing?from=pockets"
    if tier == "anonymous":
        upgrade_url = "/signup?next=/pockets&utm_source=pockets"

    html = render_template_string(
        _POCKETS_PAGE_HTML,
        as_of=datetime.datetime.utcnow().isoformat() + "Z",
        pockets=rows,
        shown=len(rows),
        caller_tier=tier,
        truncated_by_tier=max(0, total_known - len(rows)),
        upgrade_url=upgrade_url,
        top_mover=top_mover,
        build_count=build_count,
        avoid_count=avoid_count,
    )
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return resp


# ─── Profile-driven preferences (r33) ─────────────────────────────────
#
# Persisted preferences live on users.pocket_preferences (JSONB on
# Postgres, TEXT on SQLite). Shape:
#   {
#     "target_mw":   int,
#     "iso":         "PJM"|"ERCOT"|...,
#     "state":       "VA"|"TX"|...,
#     "within_ttp":  int (months),
#     "workload":    "ai_training"|"inference"|"colo"
#   }
# /api/v1/pockets/for-me reads these when matching query params aren't
# provided, so logged-in users get personalized rankings without having
# to specify each time.

def _auth_user_id():
    """Resolve the authenticated user_id from the current request.
    Returns None for anonymous. Uses the canonical auth_context resolver
    where available; falls back to JWT cookie + X-API-Key."""
    try:
        from routes.auth_context import get_auth_context
        ctx = get_auth_context(request)
        if ctx and ctx.user_id:
            return ctx.user_id
    except Exception:
        pass
    # JWT cookie fallback
    try:
        import jwt as _jwt
        from main import JWT_SECRET
        token = (request.cookies.get('dchub_token') or
                 request.cookies.get('auth_token') or
                 request.cookies.get('token') or '')
        if token:
            payload = _jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            return payload.get('user_id') or payload.get('sub')
    except Exception:
        pass
    # X-API-Key fallback
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key and api_key.startswith('dchub_'):
        try:
            import hashlib
            from main import get_pg_connection, return_pg_connection
            c = get_pg_connection(retries=1)
            try:
                cur = c.cursor()
                kh = hashlib.sha256(api_key.encode()).hexdigest()
                cur.execute("SELECT user_id FROM api_keys "
                            "WHERE key_hash=%s AND is_active=1 LIMIT 1", (kh,))
                r = cur.fetchone()
                return r[0] if r else None
            finally:
                return_pg_connection(c)
        except Exception:
            pass
    return None


def _load_preferences(user_id) -> dict:
    """Read users.pocket_preferences for the given user. Returns {} on
    any failure or missing user. Tolerates both JSONB (returns dict)
    and TEXT (returns JSON string)."""
    if not user_id:
        return {}
    conn = _get_db()
    if conn is None:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT pocket_preferences FROM users WHERE id = %s",
                    (user_id,))
        row = cur.fetchone()
        if not row or row[0] is None:
            return {}
        v = row[0]
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v) if v else {}
            except Exception:
                return {}
        return {}
    except Exception as e:
        logger.warning(f"pockets: prefs load failed for {user_id}: {e}")
        return {}
    finally:
        _return_db(conn)


def _save_preferences(user_id, prefs: dict) -> bool:
    """Persist pocket_preferences for the user. Returns True on success."""
    if not user_id:
        return False
    conn = _get_db()
    if conn is None:
        return False
    try:
        cur = conn.cursor()
        # Postgres JSONB path first (production); fall back to TEXT.
        try:
            cur.execute(
                "UPDATE users SET pocket_preferences = %s::jsonb WHERE id = %s",
                (json.dumps(prefs), user_id),
            )
            conn.commit()
            return True
        except Exception:
            try: conn.rollback()
            except Exception: pass
            cur.execute(
                "UPDATE users SET pocket_preferences = %s WHERE id = %s",
                (json.dumps(prefs), user_id),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.warning(f"pockets: prefs save failed for {user_id}: {e}")
        return False
    finally:
        _return_db(conn)


@pockets_bp.route("/api/v1/pockets/preferences", methods=["GET", "POST"])
def pockets_preferences():
    """Read or write the authenticated user's pocket preferences.

    GET  → returns current prefs (or {} if not set)
    POST → body {target_mw, iso, state, within_ttp, workload} writes them
    """
    user_id = _auth_user_id()
    if not user_id:
        return jsonify(ok=False, error="auth_required",
                       message="Sign in to save pocket preferences"), 401

    if request.method == "GET":
        return jsonify(ok=True, user_id=user_id,
                       preferences=_load_preferences(user_id)), 200

    body = request.get_json(silent=True) or {}
    # Whitelist + light validation. Unknown fields are dropped silently.
    clean = {}
    if body.get("target_mw") is not None:
        try: clean["target_mw"] = max(0, int(body["target_mw"]))
        except (ValueError, TypeError): pass
    if body.get("iso"):
        clean["iso"] = str(body["iso"]).upper().strip()[:12]
    if body.get("state"):
        clean["state"] = str(body["state"]).upper().strip()[:3]
    if body.get("within_ttp") is not None:
        try: clean["within_ttp"] = max(0, int(body["within_ttp"]))
        except (ValueError, TypeError): pass
    if body.get("workload"):
        w = str(body["workload"]).lower().strip()
        if w in {"ai_training", "inference", "colo"}:
            clean["workload"] = w

    if not _save_preferences(user_id, clean):
        return jsonify(ok=False, error="save_failed"), 500
    return jsonify(ok=True, user_id=user_id, preferences=clean,
                   saved=True), 200


# ─── Pocket detail (per-market) endpoints ────────────────────────────

def _fetch_pocket_detail(slug: str) -> dict | None:
    """Pull full detail for one market: latest snapshot + 30d history
    + comparable markets. Used by both the JSON and HTML detail
    endpoints."""
    conn = _get_db()
    if conn is None or not slug:
        return None
    out: dict = {}
    try:
        cur = conn.cursor()
        # Latest snapshot
        cur.execute("""
            SELECT market_slug, market_name, iso, state, verdict,
                   excess_power_score, constraint_score,
                   time_to_power_months, computed_at
              FROM market_power_scores
             WHERE market_slug = %s
             ORDER BY computed_at DESC
             LIMIT 1
        """, (slug,))
        row = cur.fetchone()
        if not row:
            return None
        excess_v = float(row[5] or 0)
        constraint_v = float(row[6] or 0)
        ttp_v = float(row[7] or 36)
        ttp_penalty = max(0, ttp_v - 24) * 2
        rank_score = excess_v - (constraint_v * 0.5) - ttp_penalty
        if row[4] == "BUILD":
            rank_score += 10
        elif row[4] == "AVOID":
            rank_score -= 20
        out = {
            "market_slug": row[0],
            "market_name": row[1],
            "iso":         row[2],
            "state":       row[3],
            "verdict":     row[4],
            "excess_power_score":    round(excess_v, 1),
            "constraint_score":      round(constraint_v, 1),
            "time_to_power_months":  round(ttp_v, 0),
            "rank_score":            round(rank_score, 1),
            "computed_at": row[8].isoformat() if row[8] else None,
        }

        # 30-day score history (chart data)
        try:
            cur.execute("""
                SELECT computed_at, excess_power_score, constraint_score,
                       time_to_power_months, verdict
                  FROM market_power_scores
                 WHERE market_slug = %s
                   AND computed_at >= NOW() - INTERVAL '30 days'
                 ORDER BY computed_at ASC
                 LIMIT 60
            """, (slug,))
            history = []
            for r in cur.fetchall():
                history.append({
                    "at":       r[0].isoformat() if r[0] else None,
                    "excess":   round(float(r[1] or 0), 1),
                    "constraint": round(float(r[2] or 0), 1),
                    "ttp":      round(float(r[3] or 0), 0),
                    "verdict":  r[4],
                })
            out["history_30d"] = history
        except Exception:
            try: conn.rollback()
            except Exception: pass
            out["history_30d"] = []

        # 7d delta
        delta_7d = None
        try:
            cur.execute("""
                SELECT excess_power_score
                  FROM market_power_scores
                 WHERE market_slug = %s
                   AND computed_at < NOW() - INTERVAL '7 days'
                 ORDER BY computed_at DESC
                 LIMIT 1
            """, (slug,))
            r = cur.fetchone()
            if r and r[0] is not None:
                delta_7d = round(excess_v - float(r[0]), 1)
        except Exception:
            try: conn.rollback()
            except Exception: pass
        out["delta_7d"] = delta_7d

        # Comparable markets — same ISO if available, ranked by closest score
        try:
            iso = out["iso"] or ""
            cur.execute("""
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, iso, state, verdict,
                       excess_power_score
                  FROM market_power_scores
                 WHERE market_slug != %s
                   AND (%s = '' OR iso = %s)
                 ORDER BY market_slug, computed_at DESC
            """, (slug, iso, iso))
            candidates = []
            for r in cur.fetchall():
                cand_excess = float(r[5] or 0)
                candidates.append({
                    "market_slug": r[0],
                    "market_name": r[1],
                    "iso": r[2],
                    "state": r[3],
                    "verdict": r[4],
                    "excess_power_score": round(cand_excess, 1),
                    "_distance": abs(cand_excess - excess_v),
                })
            candidates.sort(key=lambda x: x["_distance"])
            for c in candidates:
                c.pop("_distance", None)
            out["comparables"] = candidates[:5]
        except Exception:
            try: conn.rollback()
            except Exception: pass
            out["comparables"] = []
    except Exception as e:
        logger.warning(f"pockets: detail fetch failed for {slug}: {e}")
    finally:
        _return_db(conn)

    out["why"] = _rationale({
        **out,
        "delta_7d": out.get("delta_7d"),
    })
    return out


@pockets_bp.route("/api/v1/pockets/<slug>", methods=["GET"])
def pocket_detail_json(slug):
    """JSON detail for one market. Tier-gated: anon gets a SEO-friendly
    truncated payload (name + verdict + score + why), identified+ gets
    full breakdown + history + comparables."""
    tier = _detect_tier()
    detail = _fetch_pocket_detail(slug.strip())
    if not detail:
        return jsonify(ok=False, error="not_found", market_slug=slug), 404

    # Tier-gate: anon sees the headline, full data for identified+
    if tier in ("anonymous", "free"):
        return jsonify(
            ok=True,
            tier=tier,
            gated=True,
            market_slug=detail["market_slug"],
            market_name=detail["market_name"],
            iso=detail["iso"],
            state=detail["state"],
            verdict=detail["verdict"],
            rank_score=detail["rank_score"],
            delta_7d=detail["delta_7d"],
            why=detail["why"],
            upgrade={
                "label": "Sign up free for full breakdown + 30d history",
                "url":   f"/signup?next=/pockets/{slug}&utm_source=pocket_detail",
            },
        ), 200

    return jsonify(ok=True, tier=tier, **detail), 200


_POCKET_DETAIL_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>{{ d.market_name }} · Pocket of Power · DC Hub</title>
<meta name="description" content="{{ d.market_name }} ({{ d.iso or 'no ISO' }}, {{ d.state or 'no state' }}) — DCPI composite score {{ d.rank_score }}, verdict {{ d.verdict or 'HOLD' }}. {{ d.why }}.">
<meta property="og:title" content="{{ d.market_name }} — Pocket of Power Score {{ d.rank_score }}">
<meta property="og:description" content="DCPI verdict: {{ d.verdict or 'HOLD' }} · Composite score {{ d.rank_score }} · {{ d.why }}.">
<meta property="og:url" content="https://dchub.cloud/pockets/{{ d.market_slug }}">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/pockets/{{ d.market_slug }}">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "{{ d.market_name }} — Pocket of Power Ranking",
  "description": "DCPI composite score {{ d.rank_score }}, verdict {{ d.verdict or 'HOLD' }}. {{ d.why }}.",
  "datePublished": "{{ d.computed_at or '' }}",
  "dateModified": "{{ d.computed_at or '' }}",
  "author": {"@type": "Organization", "name": "DC Hub"},
  "publisher": {
    "@type": "Organization",
    "name": "DC Hub",
    "url": "https://dchub.cloud"
  },
  "url": "https://dchub.cloud/pockets/{{ d.market_slug }}",
  "about": {
    "@type": "Place",
    "name": "{{ d.market_name }}",
    "containedInPlace": {"@type": "State", "name": "{{ d.state or '' }}"}
  }
}
</script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--green:#10b981;--orange:#f59e0b;--red:#ef4444;--acc:#6366f1;--violet:#8b5cf6}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.6}
.wrap{max-width:980px;margin:0 auto;padding:2.5rem 1.5rem}
.crumb{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.08em;margin-bottom:.75rem}
.crumb a{color:var(--acc);text-decoration:none}.crumb a:hover{color:#fff}
h1{font-size:2.6rem;margin:0 0 0.5rem;font-weight:800;letter-spacing:-0.02em;display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}
.verdict{font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;font-weight:700;padding:4px 12px;border-radius:6px}
.verdict.BUILD{background:rgba(16,185,129,0.18);color:var(--green);border:1px solid rgba(16,185,129,.4)}
.verdict.HOLD{background:rgba(245,158,11,0.18);color:var(--orange);border:1px solid rgba(245,158,11,.4)}
.verdict.AVOID{background:rgba(239,68,68,0.18);color:var(--red);border:1px solid rgba(239,68,68,.4)}
.sub{color:var(--tx2);font-size:1rem;margin:0 0 2rem}
.hero-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin:1.5rem 0 2.5rem}
.hero-stat{background:linear-gradient(135deg,rgba(99,102,241,.1),rgba(168,85,247,.05));border:1px solid rgba(168,85,247,.3);border-radius:12px;padding:1.25rem 1.5rem}
.hero-stat .n{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:800;line-height:1}
.hero-stat .n.green{color:var(--green)}.hero-stat .n.red{color:var(--red)}.hero-stat .n.amber{color:var(--orange)}
.hero-stat .l{color:var(--tx2);font-size:0.7rem;text-transform:uppercase;letter-spacing:.08em;margin-top:0.5rem}
.hero-stat .x{color:var(--tx2);font-size:0.8rem;margin-top:0.25rem;font-family:'JetBrains Mono',monospace}
h2{font-size:0.82rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.12em;margin:2rem 0 0.8rem;font-weight:700}
.lede{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.5rem;margin:0 0 2rem;font-size:1.05rem;color:#ddd}
.chart{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.5rem;margin:0 0 2rem;height:240px;position:relative;overflow:hidden}
.bar{position:absolute;bottom:0;background:linear-gradient(180deg,var(--violet),var(--acc));border-radius:2px 2px 0 0;transition:opacity .15s}
.bar:hover{opacity:0.7}
.chart-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--tx2);font-size:0.9rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
th{text-align:left;padding:0.75rem 1rem;background:#0f1019;color:var(--tx2);font-size:0.72rem;text-transform:uppercase;letter-spacing:.1em;font-weight:700;border-bottom:1px solid var(--bd)}
td{padding:0.75rem 1rem;border-bottom:1px solid var(--bd);font-size:0.92rem}
tr:last-child td{border-bottom:none}
tr:hover{background:rgba(99,102,241,.04)}
.market a{color:var(--tx);text-decoration:none;font-weight:600;border-bottom:1px dotted rgba(255,255,255,.2)}
.market a:hover{color:var(--acc);border-bottom-color:var(--acc)}
footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--bd);color:var(--tx2);font-size:0.85rem}
footer a{color:var(--acc)}
.share{display:flex;gap:0.5rem;margin-top:1.5rem;flex-wrap:wrap}
.share a{padding:0.5rem 1rem;background:var(--card);border:1px solid var(--bd);border-radius:8px;color:var(--tx2);text-decoration:none;font-size:0.85rem;font-weight:600}
.share a:hover{border-color:var(--violet);color:#fff}
</style></head><body><div class="wrap">
<div class="crumb"><a href="/pockets">← All pockets</a> · <a href="/dcpi">DCPI index</a></div>
<h1>{{ d.market_name }} <span class="verdict {{ d.verdict or 'HOLD' }}">{{ d.verdict or 'HOLD' }}</span></h1>
<p class="sub">{{ d.iso or 'No ISO' }} · {{ d.state or 'No state' }} · last computed {{ d.computed_at[:10] if d.computed_at else 'never' }}</p>

<div class="hero-grid">
  <div class="hero-stat"><div class="n">{{ d.rank_score }}</div><div class="l">Composite score</div><div class="x">excess − constraint/2 − ttp penalty</div></div>
  <div class="hero-stat"><div class="n {% if d.excess_power_score >= 70 %}green{% elif d.excess_power_score >= 40 %}amber{% else %}red{% endif %}">{{ d.excess_power_score }}</div><div class="l">Excess power</div><div class="x">0=tight · 100=ample</div></div>
  <div class="hero-stat"><div class="n {% if d.constraint_score <= 30 %}green{% elif d.constraint_score <= 60 %}amber{% else %}red{% endif %}">{{ d.constraint_score }}</div><div class="l">Grid constraint</div><div class="x">0=clear · 100=blocked</div></div>
  <div class="hero-stat"><div class="n">{{ d.time_to_power_months|int }}<span style="font-size:.55em">mo</span></div><div class="l">Time to power</div><div class="x">interconnect → MW</div></div>
  {% if d.delta_7d is not none %}
  <div class="hero-stat"><div class="n {% if d.delta_7d > 0 %}green{% elif d.delta_7d < 0 %}red{% else %}amber{% endif %}">{{ '+' if d.delta_7d > 0 else '' }}{{ d.delta_7d }}</div><div class="l">7-day Δ excess</div><div class="x">vs same time last week</div></div>
  {% endif %}
</div>

<div class="lede"><b>Why this verdict:</b> {{ d.why }}.</div>

<h2>30-day excess-power trend</h2>
<div class="chart">
{% if d.history_30d %}
  {% set vals = d.history_30d|map(attribute='excess')|list %}
  {% set vmax = vals|max if vals else 100 %}
  {% set vmin = vals|min if vals else 0 %}
  {% set span = (vmax - vmin) if vmax != vmin else 1 %}
  {% set w = 100.0 / (d.history_30d|length) %}
  {% for h in d.history_30d %}
    {% set ht = ((h.excess - vmin) / span * 95) + 5 %}
    <div class="bar" style="left:{{ loop.index0 * w }}%;width:{{ w * 0.7 }}%;height:{{ ht }}%" title="{{ h.at[:10] }}: {{ h.excess }}"></div>
  {% endfor %}
{% else %}
  <div class="chart-empty">No 30-day history yet — this market joined the index recently.</div>
{% endif %}
</div>

<h2>Comparable markets {% if d.iso %}in {{ d.iso }}{% endif %}</h2>
{% if d.comparables %}
<table><thead><tr><th>Market</th><th>ISO</th><th>State</th><th>Verdict</th><th>Excess</th></tr></thead><tbody>
{% for c in d.comparables %}
<tr>
<td class="market"><a href="/pockets/{{ c.market_slug }}">{{ c.market_name }}</a></td>
<td>{{ c.iso or '—' }}</td>
<td>{{ c.state or '—' }}</td>
<td><span class="verdict {{ c.verdict or 'HOLD' }}">{{ c.verdict or 'HOLD' }}</span></td>
<td style="font-family:'JetBrains Mono',monospace">{{ c.excess_power_score }}</td>
</tr>
{% endfor %}
</tbody></table>
{% else %}
<div class="lede">No comparable markets indexed yet.</div>
{% endif %}

<div class="share">
<a href="https://twitter.com/intent/tweet?text={{ d.market_name }} — DCPI verdict {{ d.verdict or 'HOLD' }}, score {{ d.rank_score }}&url=https://dchub.cloud/pockets/{{ d.market_slug }}" target="_blank" rel="noopener">Share on X</a>
<a href="https://www.linkedin.com/sharing/share-offsite/?url=https://dchub.cloud/pockets/{{ d.market_slug }}" target="_blank" rel="noopener">Share on LinkedIn</a>
<a href="/api/v1/pockets/{{ d.market_slug }}">JSON</a>
</div>

<footer>
Methodology: composite of EIA retail rates, ISO grid headroom, DCPI verdict, time-to-power. Updated daily.
<a href="/pockets">All pockets</a> · <a href="/dcpi/{{ d.market_slug }}">Market deep-dive</a> · <a href="/digest">Daily brief</a>
</footer>
</div></body></html>'''


@pockets_bp.route("/pockets/<slug>", methods=["GET"])
def pocket_detail_page(slug):
    """Per-market HTML detail page — SEO-friendly, schema.org Article
    markup, share buttons, embedded 30d sparkline."""
    detail = _fetch_pocket_detail(slug.strip())
    if not detail:
        # Soft 404 — render a back-to-/pockets prompt rather than the
        # ugly Flask default. 404 status code preserved.
        html = f"""<!DOCTYPE html><html><head><title>Pocket not found · DC Hub</title>
        <style>body{{font-family:system-ui;background:#0a0a12;color:#fff;padding:3rem;text-align:center}}
        a{{color:#8b5cf6}}</style></head><body>
        <h1>Pocket "{slug}" not in the index</h1>
        <p>It may have been renamed or dropped from coverage.</p>
        <p><a href="/pockets">← See all ranked pockets</a></p></body></html>"""
        return Response(html, mimetype="text/html"), 404

    html = render_template_string(_POCKET_DETAIL_HTML, d=detail)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=600, must-revalidate"
    return resp


# ─── RSS feed for syndication ────────────────────────────────────────

_POCKETS_RSS = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>DC Hub · Pockets of Power</title>
  <link>https://dchub.cloud/pockets</link>
  <atom:link href="https://dchub.cloud/pockets.rss" rel="self" type="application/rss+xml"/>
  <description>Live ranking of data-center-friendly markets by excess power, grid constraint, and time-to-power. Updated daily.</description>
  <language>en-us</language>
  <lastBuildDate>{{ build_date }}</lastBuildDate>
  <ttl>360</ttl>
{% for p in pockets %}
  <item>
    <title>{{ p.market_name }} — {{ p.verdict or 'HOLD' }} — Score {{ p.rank_score }}{% if p.delta_7d and p.delta_7d > 0 %} (▲ +{{ p.delta_7d }}){% elif p.delta_7d and p.delta_7d < 0 %} (▼ {{ p.delta_7d }}){% endif %}</title>
    <link>https://dchub.cloud/pockets/{{ p.market_slug }}</link>
    <guid isPermaLink="true">https://dchub.cloud/pockets/{{ p.market_slug }}</guid>
    <pubDate>{{ p.computed_at_rfc }}</pubDate>
    <description><![CDATA[{{ p.market_name }} ({{ p.iso or 'no ISO' }}, {{ p.state or 'no state' }}) — DCPI verdict {{ p.verdict or 'HOLD' }}. Composite score {{ p.rank_score }}, excess power {{ p.excess_power_score }}, constraint {{ p.constraint_score }}, TTP {{ p.time_to_power_months|int }}mo. {{ p.why }}.]]></description>
    <category>{{ p.verdict or 'HOLD' }}</category>
    {% if p.iso %}<category>{{ p.iso }}</category>{% endif %}
    {% if p.state %}<category>{{ p.state }}</category>{% endif %}
  </item>
{% endfor %}
</channel>
</rss>'''


def _rfc822(iso_str: str) -> str:
    """ISO 8601 → RFC 822 for RSS pubDate."""
    if not iso_str:
        return datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    except Exception:
        return datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")


@pockets_bp.route("/pockets.rss", methods=["GET"])
@pockets_bp.route("/api/v1/pockets/rss", methods=["GET"])
def pockets_rss():
    """RSS 2.0 feed for the top 30 ranked pockets. No auth — designed
    to be picked up by Feedly, Inoreader, NetNewsWire, etc., and by
    LLM agents (Perplexity, Claude) for indexing."""
    rows = _fetch_pockets(limit_hint=30)
    for r in rows:
        r["why"] = _rationale(r)
        r["computed_at_rfc"] = _rfc822(r.get("computed_at") or "")
    xml = render_template_string(
        _POCKETS_RSS,
        pockets=rows,
        build_date=_rfc822(datetime.datetime.utcnow().isoformat() + "Z"),
    )
    resp = Response(xml, mimetype="application/rss+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "public, max-age=900"
    return resp


# ─── Pocket-alert queue endpoint (autopilot-callable) ─────────────────
@pockets_bp.route("/api/v1/marketing/queue-pocket-alert", methods=["POST"])
def queue_pocket_alert():
    """Phase r28 (2026-05-20) — autopilot endpoint. The brain's
    _action_pocket_alert_announce calls this when a market shifts
    ≥15pts in 7 days. We persist into social_post_queue (if it exists)
    + log so the existing LinkedIn/X auto-publish cron picks it up.

    Admin-gated (autopilot includes the admin key automatically). Idempotent
    per market_slug — a same-slug post in the last 24h is rejected so the
    queue doesn't fill up if the detector fires twice."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(ok=False, error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    slug = (body.get("market_slug") or "").strip()
    text = (body.get("body") or "").strip()
    if not slug or not text:
        return jsonify(ok=False, error="market_slug + body required"), 400

    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503

    queued = False
    skipped = False
    try:
        cur = conn.cursor()
        # Idempotency: refuse if we already queued an alert for this slug
        # in the last 24 hours. Tries multiple known queue tables; if
        # none exist, treat as "log only" (still returns ok).
        for tbl in ("social_post_queue", "marketing_queue", "social_posts"):
            try:
                cur.execute(
                    f"""SELECT 1 FROM {tbl}
                         WHERE meta::text LIKE %s
                           AND created_at > NOW() - INTERVAL '24 hours'
                         LIMIT 1""",
                    (f'%{slug}%',),
                )
                if cur.fetchone():
                    skipped = True
                    break
            except Exception:
                try: conn.rollback()
                except Exception: pass
                continue

        if not skipped:
            # Try to insert into social_post_queue (the canonical table).
            for tbl, sql in (
                ("social_post_queue",
                 """INSERT INTO social_post_queue (platform, body, meta, status, created_at)
                    VALUES ('linkedin', %s, %s, 'queued', NOW())"""),
                ("marketing_queue",
                 """INSERT INTO marketing_queue (channel, body, meta, status, created_at)
                    VALUES ('social', %s, %s, 'queued', NOW())"""),
            ):
                try:
                    meta_json = json.dumps({
                        "kind":         "pocket_alert",
                        "market_slug":  slug,
                        "market_name":  body.get("market_name"),
                        "iso":          body.get("iso"),
                        "state":        body.get("state"),
                        "delta_7d":     body.get("delta_7d"),
                        "verdict":      body.get("verdict"),
                    })
                    cur.execute(sql, (text, meta_json))
                    conn.commit()
                    queued = True
                    break
                except Exception:
                    try: conn.rollback()
                    except Exception: pass

        # Always log to brain_findings for observability so even if no
        # queue table exists we have a paper trail.
        try:
            cur.execute(
                """INSERT INTO brain_findings (issue, url, count, detail, detector, created_at)
                   VALUES ('pocket_alert_queued', %s, 1, %s, 'autopilot', NOW())""",
                (f"/pockets?focus={slug}", text[:300]),
            )
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
    finally:
        _return_db(conn)

    return jsonify(
        ok=True,
        queued=queued,
        skipped_idempotent=skipped,
        market_slug=slug,
        body_preview=text[:120],
    ), 200


# ─── Pocket-mover detector helper (for brain) ─────────────────────────
def detect_high_movers(threshold: float = 15.0) -> list[dict]:
    """Returns list of pockets that moved more than `threshold` points
    on the excess-power index in the last 7 days. Used by the brain
    consistency radar to surface significant shifts."""
    rows = _fetch_pockets(limit_hint=500)
    out = []
    for r in rows:
        if r["delta_7d"] is not None and abs(r["delta_7d"]) >= threshold:
            out.append({
                "market_slug": r["market_slug"],
                "market_name": r["market_name"],
                "iso": r["iso"],
                "state": r["state"],
                "delta_7d": r["delta_7d"],
                "current_score": r["excess_power_score"],
                "verdict": r["verdict"],
            })
    out.sort(key=lambda r: -abs(r["delta_7d"]))
    return out


# ─── r34: Map overlay support ────────────────────────────────────────
#
# /map already exists. r34 adds a "show pockets" overlay layer. The
# /api/v1/pockets/geojson endpoint emits a GeoJSON FeatureCollection
# with one Feature per market (Point geometry). Each feature carries
# the verdict + score so the frontend can color-code (BUILD=green,
# HOLD=amber, AVOID=red) and link the marker popup to /pockets/<slug>.
#
# Lat/lng comes from a curated MARKET_COORDS dict — market_power_scores
# doesn't carry coordinates, and looking up via facilities aggregation
# would be slow + noisy. Curated covers ~80 known markets.
MARKET_COORDS = {
    # PJM
    "northern-virginia": (38.9047, -77.4356),  # Ashburn
    "ashburn":           (39.0438, -77.4874),
    "chicago":           (41.8781, -87.6298),
    "columbus":          (39.9612, -82.9988),
    "atlanta":           (33.7490, -84.3880),
    "richmond":          (37.5407, -77.4360),
    "pittsburgh":        (40.4406, -79.9959),
    # ERCOT
    "dallas":            (32.7767, -96.7970),
    "fort-worth":        (32.7555, -97.3308),
    "houston":           (29.7604, -95.3698),
    "austin":            (30.2672, -97.7431),
    "san-antonio":       (29.4241, -98.4936),
    # CAISO / WECC
    "silicon-valley":    (37.3382, -121.8863),
    "san-francisco":     (37.7749, -122.4194),
    "los-angeles":       (34.0522, -118.2437),
    "san-jose":          (37.3382, -121.8863),
    "phoenix":           (33.4484, -112.0740),
    "tucson":            (32.2226, -110.9747),
    "las-vegas":         (36.1699, -115.1398),
    "reno":              (39.5296, -119.8138),
    "salt-lake-city":    (40.7608, -111.8910),
    "portland":          (45.5152, -122.6784),
    "seattle":           (47.6062, -122.3321),
    "spokane":           (47.6588, -117.4260),
    "denver":            (39.7392, -104.9903),
    "boise":             (43.6150, -116.2023),
    "san-diego":         (32.7157, -117.1611),
    # MISO
    "minneapolis":       (44.9778, -93.2650),
    "des-moines":        (41.5868, -93.6250),
    "milwaukee":          (43.0389, -87.9065),
    "st-louis":          (38.6270, -90.1994),
    "kansas-city":       (39.0997, -94.5786),
    "indianapolis":      (39.7684, -86.1581),
    "detroit":           (42.3314, -83.0458),
    # NYISO + ISO-NE
    "new-york":          (40.7128, -74.0060),
    "albany":            (42.6526, -73.7562),
    "boston":            (42.3601, -71.0589),
    "providence":        (41.8240, -71.4128),
    # SPP / SERC
    "oklahoma-city":     (35.4676, -97.5164),
    "omaha":             (41.2565, -95.9345),
    "memphis":           (35.1495, -90.0490),
    "nashville":         (36.1627, -86.7816),
    "charlotte":         (35.2271, -80.8431),
    "raleigh":           (35.7796, -78.6382),
    "miami":             (25.7617, -80.1918),
    "tampa":             (27.9506, -82.4572),
    "orlando":           (28.5384, -81.3789),
    "jacksonville":      (30.3322, -81.6557),
    # Canada
    "toronto":           (43.6532, -79.3832),
    "montreal":          (45.5017, -73.5673),
    "vancouver":         (49.2827, -123.1207),
    "calgary":           (51.0447, -114.0719),
    "ottawa":            (45.4215, -75.6972),
    "quebec-city":       (46.8139, -71.2080),
    # Europe (top markets)
    "london":            (51.5074, -0.1278),
    "frankfurt":         (50.1109, 8.6821),
    "amsterdam":         (52.3676, 4.9041),
    "paris":             (48.8566, 2.3522),
    "dublin":            (53.3498, -6.2603),
    "stockholm":         (59.3293, 18.0686),
    "madrid":            (40.4168, -3.7038),
    "milan":             (45.4642, 9.1900),
    "zurich":            (47.3769, 8.5417),
    "warsaw":            (52.2297, 21.0122),
    # APAC
    "singapore":         (1.3521, 103.8198),
    "tokyo":             (35.6762, 139.6503),
    "osaka":             (34.6937, 135.5023),
    "sydney":            (-33.8688, 151.2093),
    "melbourne":         (-37.8136, 144.9631),
    "hong-kong":         (22.3193, 114.1694),
    "mumbai":            (19.0760, 72.8777),
    "delhi":             (28.7041, 77.1025),
    "bangalore":         (12.9716, 77.5946),
    "seoul":             (37.5665, 126.9780),
    "jakarta":           (-6.2088, 106.8456),
    "kuala-lumpur":      (3.1390, 101.6869),
    "bangkok":           (13.7563, 100.5018),
    "manila":            (14.5995, 120.9842),
    # LATAM
    "sao-paulo":         (-23.5505, -46.6333),
    "rio-de-janeiro":    (-22.9068, -43.1729),
    "mexico-city":       (19.4326, -99.1332),
    "santiago":          (-33.4489, -70.6693),
    "bogota":            (4.7110, -74.0721),
    "buenos-aires":      (-34.6037, -58.3816),
}


@pockets_bp.route("/api/v1/pockets/geojson", methods=["GET"])
def pockets_geojson():
    """GeoJSON FeatureCollection for the map overlay. One Point per
    market that has known coordinates. Each Feature carries verdict,
    score, delta_7d, and a link URL so the frontend can color + popup.

    Public — no tier gate. The map exposing where the BUILD markets
    are is marketing surface, not gated data.
    """
    rows = _fetch_pockets(limit_hint=200)
    features = []
    skipped = 0
    for r in rows:
        slug = (r.get("market_slug") or "").lower()
        coords = MARKET_COORDS.get(slug)
        if not coords:
            skipped += 1
            continue
        lat, lng = coords
        verdict = (r.get("verdict") or "HOLD").upper()
        color = {
            "BUILD": "#10b981",  # green
            "HOLD":  "#f59e0b",  # amber
            "AVOID": "#ef4444",  # red
        }.get(verdict, "#9ca3af")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "market_slug": slug,
                "market_name": r.get("market_name"),
                "iso":         r.get("iso"),
                "state":       r.get("state"),
                "verdict":     verdict,
                "rank_score":  r.get("rank_score"),
                "excess":      r.get("excess_power_score"),
                "constraint":  r.get("constraint_score"),
                "delta_7d":    r.get("delta_7d"),
                "color":       color,
                "url":         f"/pockets/{slug}",
                "why":         _rationale(r),
            },
        })
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "shown": len(features),
        "skipped_no_coords": skipped,
        "legend": {
            "BUILD": "#10b981",
            "HOLD":  "#f59e0b",
            "AVOID": "#ef4444",
        },
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp


# ─── r35: Weekly pockets digest ──────────────────────────────────────
#
# Mon-morning focused email of the week's top BUILD pockets + biggest
# movers. Separate from /digest (which is daily, broader) — this one
# is narrow: pockets only, weekly cadence, sent Mondays so it lands at
# the top of the inbox.

def _build_weekly_digest():
    """Compose the weekly digest payload: top 5 BUILD + top 5 movers."""
    rows = _fetch_pockets(limit_hint=200)
    top_build = [r for r in rows if r.get("verdict") == "BUILD"][:5]
    movers = [r for r in rows if r.get("delta_7d") is not None]
    top_movers = sorted(movers, key=lambda r: -abs(r.get("delta_7d") or 0))[:5]
    for r in (top_build + top_movers):
        r["why"] = _rationale(r)
    return {
        "as_of":      datetime.datetime.utcnow().isoformat() + "Z",
        "week_of":    datetime.date.today().isoformat(),
        "top_build":  top_build,
        "top_movers": top_movers,
        "title":      f"Pockets of Power · Week of {datetime.date.today().strftime('%b %d, %Y')}",
    }


def _render_weekly_html(d: dict) -> str:
    """Render the weekly digest as HTML for both the preview endpoint
    and the Resend email body. Brand-matched dark theme."""
    rows_build = "".join(
        f'<tr><td><a href="https://dchub.cloud/pockets/{r["market_slug"]}" '
        f'style="color:#fff;font-weight:600;text-decoration:none">'
        f'{r["market_name"]}</a><br><span style="color:#9ca3af;font-size:12px">'
        f'{r.get("iso") or "—"} · {r.get("state") or "—"}</span></td>'
        f'<td style="font-family:monospace;font-weight:700;color:#10b981">'
        f'{r["rank_score"]}</td>'
        f'<td style="color:#cbd5e1;font-size:13px">{r["why"]}</td></tr>'
        for r in d["top_build"]
    ) or '<tr><td colspan="3" style="color:#6b7280;text-align:center;padding:24px">No BUILD verdict markets this week.</td></tr>'

    rows_movers = "".join(
        (lambda r, sign, color: (
            f'<tr><td><a href="https://dchub.cloud/pockets/{r["market_slug"]}" '
            f'style="color:#fff;font-weight:600;text-decoration:none">'
            f'{r["market_name"]}</a><br><span style="color:#9ca3af;font-size:12px">'
            f'{r.get("iso") or "—"} · {r.get("state") or "—"}</span></td>'
            f'<td style="font-family:monospace;font-weight:700;color:{color}">'
            f'{sign}{r["delta_7d"]}</td>'
            f'<td style="color:#cbd5e1;font-size:13px">{r["why"]}</td></tr>'
        ))(r,
            "+" if (r.get("delta_7d") or 0) > 0 else "",
            "#10b981" if (r.get("delta_7d") or 0) > 0 else "#ef4444")
        for r in d["top_movers"]
    ) or '<tr><td colspan="3" style="color:#6b7280;text-align:center;padding:24px">No significant movers this week.</td></tr>'

    return f'''<div style="font-family:'Instrument Sans',-apple-system,sans-serif;background:#0a0a12;color:#fff;padding:32px;max-width:680px;margin:0 auto;line-height:1.55">
<div style="font-family:monospace;font-size:11px;color:#c4b5fd;text-transform:uppercase;letter-spacing:.14em;margin-bottom:8px">DC HUB · WEEKLY POCKETS · {d["week_of"]}</div>
<h1 style="font-size:24px;margin:0 0 8px;font-weight:800;color:#fff">{d["title"]}</h1>
<p style="color:#9ca3af;margin:0 0 24px;font-size:14px">Top BUILD-verdict markets and biggest 7-day movers on the excess-power index. Powered by DC Hub's live grid + DCPI signals.</p>

<h2 style="font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.12em;margin:24px 0 12px">Top BUILD verdicts</h2>
<table style="width:100%;border-collapse:collapse;background:#11121a;border:1px solid #1f2030;border-radius:10px;overflow:hidden;font-size:14px">
<thead><tr><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">Market</th><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">Score</th><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">Why</th></tr></thead>
<tbody>{rows_build}</tbody></table>

<h2 style="font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.12em;margin:24px 0 12px">Biggest 7-day movers</h2>
<table style="width:100%;border-collapse:collapse;background:#11121a;border:1px solid #1f2030;border-radius:10px;overflow:hidden;font-size:14px">
<thead><tr><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">Market</th><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">7d Δ</th><th style="text-align:left;padding:10px 14px;color:#9ca3af;background:#0f1019;font-size:11px;text-transform:uppercase;letter-spacing:.1em">Why</th></tr></thead>
<tbody>{rows_movers}</tbody></table>

<p style="margin:24px 0 0;color:#6b7280;font-size:13px;text-align:center">
<a href="https://dchub.cloud/pockets" style="color:#6366f1;text-decoration:none">See the full ranking →</a> ·
<a href="https://dchub.cloud/pockets.rss" style="color:#6b7280">RSS</a> ·
<a href="https://dchub.cloud/digest" style="color:#6b7280">Daily brief</a>
</p>
<p style="margin:16px 0 0;color:#6b7280;font-size:11px;text-align:center">DC Hub · neutral data layer for data center infrastructure</p>
</div>'''


@pockets_bp.route("/api/v1/pockets/digest/preview", methods=["GET"])
def pockets_digest_preview():
    """Preview the weekly digest HTML in a browser. Public — same
    content the email would carry, useful for QA + the user to share
    as a link instead of an email blast."""
    d = _build_weekly_digest()
    html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{d["title"]}</title>'
        f'<style>body{{background:#0a0a12;margin:0;padding:24px 0}}</style>'
        f'</head><body>{_render_weekly_html(d)}</body></html>'
    )
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=900"
    return resp


@pockets_bp.route("/api/v1/pockets/digest/send", methods=["POST"])
def pockets_digest_send():
    """Send the weekly pockets digest to the UNION'd recipient list
    (digest_subscribers + mcp_dev_keys). Admin-gated. Idempotent per
    week — refuses if a digest was already sent in the last 6 days.
    Supports ?dry=1 to inspect recipients without sending."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(ok=False, error="unauthorized"), 401

    dry = request.args.get("dry", "0") == "1"

    # Recipient list — same UNION as /api/v1/digest/send.
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    emails: list = []
    try:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT DISTINCT lower(email)
                  FROM (
                    SELECT email FROM mcp_dev_keys
                     WHERE email IS NOT NULL AND email != ''
                    UNION
                    SELECT email FROM digest_subscribers
                     WHERE email IS NOT NULL AND email != ''
                       AND unsubscribed_at IS NULL
                  ) u
            """)
            emails = [r[0] for r in cur.fetchall() if r[0]]
        except Exception as e:
            return jsonify(ok=False, error="recipient_query_failed",
                           detail=str(e)[:200]), 500

        # Idempotency — refuse to send twice within 6 days. We log
        # sends to brain_findings so this is queryable.
        if not dry:
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM brain_findings
                     WHERE issue = 'pockets_weekly_digest_sent'
                       AND created_at > NOW() - INTERVAL '6 days'
                """)
                if int((cur.fetchone() or [0])[0] or 0) > 0:
                    return jsonify(
                        ok=False, error="already_sent_this_week",
                        message=("A pockets-weekly digest was already sent "
                                 "in the last 6 days. Use ?dry=1 to inspect "
                                 "without sending."),
                    ), 409
            except Exception:
                try: conn.rollback()
                except Exception: pass
    finally:
        _return_db(conn)

    d = _build_weekly_digest()
    html_body = _render_weekly_html(d)

    if dry:
        return jsonify(
            ok=True, dry_run=True,
            recipient_count=len(emails),
            sample_recipients=emails[:3],
            digest=d,
        ), 200

    # Fire the emails via Resend (same client digest.py uses).
    sent, failed = 0, 0
    resend_key = os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY", "")
    if not resend_key:
        return jsonify(ok=False, error="resend_not_configured"), 500
    try:
        import requests as _rq
        from_email = os.environ.get("DCHUB_FROM_EMAIL",
                                     "DC Hub <jonathan@dchub.cloud>")
        for em in emails:
            try:
                r = _rq.post(
                    "https://api.resend.com/emails",
                    json={
                        "from":    from_email,
                        "to":      [em],
                        "subject": d["title"],
                        "html":    html_body,
                    },
                    headers={
                        "Authorization": f"Bearer {resend_key.strip()}",
                        "Content-Type":  "application/json",
                    },
                    timeout=15,
                )
                if 200 <= r.status_code < 300:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error="resend_exception",
                       detail=str(e)[:200], sent=sent, failed=failed), 500

    # Log the send for idempotency + observability.
    conn = _get_db()
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO brain_findings (issue, url, count, detail, detector, created_at)
                   VALUES ('pockets_weekly_digest_sent', %s, %s, %s, 'pockets_digest', NOW())""",
                (f"/pockets?week={d['week_of']}",
                 sent, f"sent={sent} failed={failed} title={d['title']}"),
            )
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
        finally:
            _return_db(conn)

    return jsonify(ok=True, sent=sent, failed=failed,
                   recipient_count=len(emails), week_of=d["week_of"]), 200
