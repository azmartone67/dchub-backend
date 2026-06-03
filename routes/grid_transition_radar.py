"""
Grid + Gas Transition Sentinel (2026-06-03)
===========================================
The predictive companion to DCPI's full 233-market dump: instead of listing
every market, the Sentinel surfaces ONLY the markets and ISOs with the strongest
"emergence" signal — where hyperscale-friendly grid capacity is opening up RIGHT
NOW (BUILD verdict + high excess-power headroom + short time-to-power).

Positioning vs incumbents: CBRE/JLL/DataCenterHawk publish *retrospective* "where
capacity landed". The Sentinel is *forward*: "where it's landing next", grounded
in grid physics (the DCPI excess-power + constraint + queue inputs), not hype.

  • Emerging Markets Radar  — top markets by composite among BUILD-verdict, sub-24mo
  • Grid Headroom Leaderboard — top markets by raw excess-power score
  • ISO rollup              — BUILD-rate + avg headroom + avg time-to-power per ISO
  • Transition thesis (PAID) — the "which ISO is about to open up, and why" call

Built on existing verified data (market_power_scores + DCPI derive_composite_score).
Raw radar/leaderboard = FREE (the hook). The forward thesis = PAID decision layer
(Developer/Pro/Enterprise) — the enterprise-alert use case for infra funds.

Endpoints:
  GET /api/v1/grid-transition/radar   JSON
  GET /grid-transition                branded HTML page
"""

import logging
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
grid_transition_radar_bp = Blueprint("grid_transition_radar", __name__)

_PAID = {"DEVELOPER", "PRO", "ENTERPRISE", "FOUNDING", "ADMIN"}
_UPGRADE_URL = "https://dchub.cloud/upgrade?tool=grid_transition_sentinel"


def _db():
    from main import get_read_db
    try:
        return get_read_db()
    except Exception:
        from main import get_db
        return get_db()


def _load_markets():
    from routes.dcpi import derive_composite_score
    conn = None
    rows = []
    try:
        conn = _db()
        c = conn.cursor()
        c.execute(
            """
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, state, iso,
                   constraint_score, excess_power_score,
                   time_to_power_months, verdict
            FROM market_power_scores
            WHERE published = true
            ORDER BY market_slug, computed_at DESC
            """
        )
        cols = [d[0] for d in c.description]
        for r in c.fetchall():
            d = dict(zip(cols, r))
            try:
                d["composite_score"] = derive_composite_score(
                    d.get("excess_power_score"), d.get("constraint_score"),
                    d.get("time_to_power_months"), d.get("verdict"),
                )
            except Exception:
                d["composite_score"] = d.get("excess_power_score") or 0
            rows.append(d)
    except Exception as e:
        logger.warning(f"transition-sentinel: market load failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _iso_rollup(markets):
    """Per-ISO emergence: BUILD rate, avg excess headroom, avg time-to-power."""
    by = {}
    for m in markets:
        iso = (m.get("iso") or "?").upper()
        b = by.setdefault(iso, {"iso": iso, "markets": 0, "build": 0,
                                "excess_sum": 0.0, "excess_n": 0,
                                "ttp_sum": 0.0, "ttp_n": 0})
        b["markets"] += 1
        if (m.get("verdict") or "").upper() == "BUILD":
            b["build"] += 1
        e = _num(m.get("excess_power_score"))
        if e is not None:
            b["excess_sum"] += e
            b["excess_n"] += 1
        t = _num(m.get("time_to_power_months"))
        if t is not None:
            b["ttp_sum"] += t
            b["ttp_n"] += 1
    out = []
    for b in by.values():
        out.append({
            "iso": b["iso"],
            "markets": b["markets"],
            "build_verdicts": b["build"],
            "build_rate_pct": round(b["build"] / b["markets"] * 100, 1) if b["markets"] else 0,
            "avg_excess_power": round(b["excess_sum"] / b["excess_n"], 1) if b["excess_n"] else None,
            "avg_time_to_power_months": round(b["ttp_sum"] / b["ttp_n"]) if b["ttp_n"] else None,
        })
    # Emergence rank: BUILD-rate first, then avg headroom, then faster time-to-power.
    out.sort(key=lambda x: (-(x["build_rate_pct"] or 0),
                            -(x["avg_excess_power"] or 0),
                            (x["avg_time_to_power_months"] or 1e9)))
    return out


def _emerging(markets, max_months=24):
    """Markets where capacity is opening up now: BUILD + sub-deadline, by composite."""
    e = [m for m in markets
         if (m.get("verdict") or "").upper() == "BUILD"
         and (_num(m.get("time_to_power_months")) or 1e9) <= max_months]
    e.sort(key=lambda r: -(r.get("composite_score") or 0))
    return e


def _row(m):
    return {
        "market": m.get("market_name") or m.get("market_slug"),
        "slug": m.get("market_slug"),
        "state": m.get("state"),
        "iso": m.get("iso"),
        "verdict": m.get("verdict"),
        "excess_power_score": m.get("excess_power_score"),
        "time_to_power_months": m.get("time_to_power_months"),
        "composite_score": (round(m["composite_score"], 1)
                            if isinstance(m.get("composite_score"), (int, float)) else None),
        "dcpi_url": f"https://dchub.cloud/dcpi/{m.get('market_slug')}",
    }


def _thesis(iso_rollup, emerging):
    """PAID forward thesis: which ISO is opening up, and the why."""
    if not iso_rollup:
        return {"locked": False, "headline": "No ISO signal available."}
    # Best emerging ISO with >=2 markets so it's not a single-market fluke.
    cand = [x for x in iso_rollup if x["markets"] >= 2 and (x["build_rate_pct"] or 0) > 0]
    lead = cand[0] if cand else iso_rollup[0]
    top_mkts = [m for m in emerging if (m.get("iso") or "").upper() == lead["iso"]][:3]
    names = ", ".join((m.get("market_name") or m.get("market_slug")) for m in top_mkts) or "its markets"
    return {
        "locked": False,
        "headline": f"{lead['iso']} is the strongest emerging grid right now "
                    f"({lead['build_verdicts']}/{lead['markets']} markets BUILD-rated, "
                    f"{lead['build_rate_pct']}%).",
        "why": (f"{lead['iso']} pairs a {lead['build_rate_pct']}% BUILD rate with avg excess-power "
                f"headroom of {lead['avg_excess_power']} and avg time-to-power of "
                f"~{lead['avg_time_to_power_months']}mo — the combination that signals real, "
                f"near-term, sitable capacity rather than queue backlog."),
        "where_to_look": names,
        "next_step": ("Validate substation/feeder-level headroom in these markets and secure an "
                      "interconnection position before the queue tightens; cross-check behind-the-meter "
                      "gas optionality on the DC Hub Gas Index (DCGI) for the same states."),
    }


def _thesis_teaser(iso_rollup):
    n = len(iso_rollup)
    return {
        "locked": True,
        "message": (f"🎯 The forward thesis is the paid layer. The radar + ISO leaderboard above "
                    f"(all {n} ISOs) are free — but the call on *which* grid is about to open up, "
                    f"why, and what to do about it is Developer/Pro."),
        "unlock": {"url": _UPGRADE_URL, "developer_usd_month": 49, "pro_usd_month": 199},
    }


@grid_transition_radar_bp.route("/api/v1/grid-transition/radar", methods=["GET", "OPTIONS"])
def radar():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        max_months = int(request.args.get("max_months") or 24)
    except (TypeError, ValueError):
        max_months = 24
    try:
        limit = max(1, min(int(request.args.get("limit") or 15), 50))
    except (TypeError, ValueError):
        limit = 15

    markets = _load_markets()
    if not markets:
        return jsonify(ok=False, error="DCPI scores temporarily unavailable"), 503

    iso_rollup = _iso_rollup(markets)
    emerging = _emerging(markets, max_months)
    headroom = sorted(markets, key=lambda r: -(_num(r.get("excess_power_score")) or 0))[:limit]

    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
    except Exception:
        tier = "FREE"
    is_paid = (tier or "FREE").upper() in _PAID

    out = {
        "ok": True,
        "product": "Grid + Gas Transition Sentinel",
        "as_of_universe": len(markets),
        "emerging_markets": [_row(m) for m in emerging[:limit]],
        "grid_headroom_leaderboard": [_row(m) for m in headroom],
        "iso_rollup": iso_rollup,
        "tier": tier,
        "citation": "DC Hub Grid + Gas Transition Sentinel — dchub.cloud/grid-transition (CC BY 4.0)",
    }
    out["transition_thesis"] = _thesis(iso_rollup, emerging) if is_paid else _thesis_teaser(iso_rollup)
    return jsonify(out), 200


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grid + Gas Transition Sentinel · DC Hub</title>
<meta name="description" content="Where is the next hyperscale-friendly grid emerging? A forward-looking radar of US markets and ISOs opening up for data-center power now — BUILD verdicts, excess-power headroom, and time-to-power from the DC Hub Power Index.">
<link rel="canonical" href="https://dchub.cloud/grid-transition">
<style>
 :root{--bg:#0a0b12;--card:#12141d;--line:rgba(255,255,255,.08);--ink:#e2e8f0;--mut:#94a3b8;--ind:#6366f1;--grn:#34d399;--amb:#fbbf24}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
 h1{font-size:1.9rem;margin:.2em 0}h2{font-size:1.15rem;margin:1.6em 0 .4em}.sub{color:var(--mut);max-width:64ch}
 .badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--ind);border:1px solid var(--line);border-radius:20px;padding:3px 12px;margin-bottom:14px}
 table{width:100%;border-collapse:collapse;margin-top:6px;font-size:13.5px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .v{font-weight:700;font-size:11px;padding:2px 8px;border-radius:6px}
 .BUILD{background:rgba(52,211,153,.16);color:var(--grn)}.CAUTION{background:rgba(251,191,36,.16);color:var(--amb)}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-top:18px}
 .lock{border-color:rgba(99,102,241,.4)}.lock-badge{display:inline-block;background:var(--ind);color:#fff;font-size:10px;font-weight:700;padding:2px 9px;border-radius:6px;letter-spacing:.08em;margin-bottom:8px}.peek{position:relative;margin:12px 0;border-radius:8px;overflow:hidden;border:1px solid var(--line)}.peek ul{filter:blur(3.5px);margin:0;padding:14px 14px 14px 30px;color:#cbd5e1;font-size:13px;user-select:none;background:rgba(99,102,241,.05)}.peek::after{content:'🔒 Pro';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;color:#c4b5fd;background:rgba(10,11,18,.22)}.cta-big{display:inline-block;margin-top:12px;background:var(--ind);color:#fff;text-decoration:none;padding:12px 22px;border-radius:9px;font-weight:800;font-size:15px;box-shadow:0 4px 14px rgba(99,102,241,.35)}.cta-sub{margin-top:7px;font-size:11px;color:#64748b}.cta{display:inline-block;margin-top:10px;background:var(--ind);color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:700}
 a{color:#a5b4fc}
</style></head><body><div class="wrap">
<span class="badge">DC Hub · Transition Sentinel</span>
<h1>Where is the next grid opening up?</h1>
<p class="sub">Incumbents tell you where capacity <em>landed</em>. The Sentinel tells you where it's <em>landing next</em> — the US markets and ISOs with real near-term hyperscale headroom right now, grounded in the DC Hub Power Index (excess-power, constraint, and time-to-power), not hype.</p>
<div id="out"><p class="sub">Loading the radar…</p></div>
<script>
async function load(){
 var out=document.getElementById('out');
 try{
  var r=await fetch('/api/v1/grid-transition/radar?limit=12');var d=await r.json();
  if(!d.ok){out.innerHTML='<p class="sub">'+(d.error||'No data')+'</p>';return;}
  function tbl(rows,cols){var h='<table><thead><tr>'+cols.map(function(c){return '<th>'+c[0]+'</th>';}).join('')+'</tr></thead><tbody>';
   rows.forEach(function(m){h+='<tr>'+cols.map(function(c){var val=c[1](m);return '<td>'+val+'</td>';}).join('')+'</tr>';});return h+'</tbody></table>';}
  var mcol=[['Market',function(m){return '<a href="'+m.dcpi_url+'">'+(m.market||'')+'</a> <span style="color:#64748b">'+(m.state||'')+'</span>';}],
            ['ISO',function(m){return m.iso||'';}],
            ['Verdict',function(m){return '<span class="v '+(m.verdict||'')+'">'+(m.verdict||'')+'</span>';}],
            ['Excess',function(m){return m.excess_power_score!=null?m.excess_power_score:'—';}],
            ['Time-to-power',function(m){return m.time_to_power_months!=null?'~'+m.time_to_power_months+'mo':'—';}]];
  var h='<h2>⚡ Emerging Markets Radar <span class="sub">(BUILD + sub-24mo, ranked)</span></h2>'+tbl(d.emerging_markets,mcol);
  h+='<h2>🏆 Grid Headroom Leaderboard</h2>'+tbl(d.grid_headroom_leaderboard,mcol);
  var icol=[['ISO',function(x){return x.iso;}],['Markets',function(x){return x.markets;}],
            ['BUILD rate',function(x){return x.build_rate_pct+'%';}],
            ['Avg excess',function(x){return x.avg_excess_power!=null?x.avg_excess_power:'—';}],
            ['Avg time-to-power',function(x){return x.avg_time_to_power_months!=null?'~'+x.avg_time_to_power_months+'mo':'—';}]];
  h+='<h2>🌐 ISO Emergence Rollup</h2>'+tbl(d.iso_rollup,icol);
  var t=d.transition_thesis||{};
  if(t.locked){h+='<div class="card lock"><div class="lock-badge">🔒 PRO · FORWARD THESIS</div><strong>Which grid is about to open up next?</strong><p class="sub">The radar + leaderboard above are free — the forward call is Pro:</p><div class="peek"><ul><li>The #1 emerging ISO — and why now</li><li>The markets to move on first</li><li>The interconnection play before the queue tightens</li></ul></div><a class="cta-big" href="'+(t.unlock&&t.unlock.url||'/pricing')+'">🔓 Unlock the thesis — Developer $49/mo →</a><div class="cta-sub">Cancel anytime · also unlocks the MCP decision tools + raw data</div></div>';}
  else if(t.headline){h+='<div class="card"><strong>'+t.headline+'</strong><p>'+(t.why||'')+'</p><p class="sub">Where to look: '+(t.where_to_look||'')+'</p><p class="sub">Next step: '+(t.next_step||'')+'</p></div>';}
  out.innerHTML=h;
 }catch(e){out.innerHTML='<p class="sub">Error: '+e+'</p>';}
}
load();
</script>
<p class="sub" style="margin-top:40px;font-size:12px">Powered by the <a href="/dcpi">DC Hub Power Index</a> + <a href="/dcgi">Gas Index</a>. Data CC BY 4.0. © 2026 DC Hub.</p>
</div></body></html>"""


@grid_transition_radar_bp.route("/grid-transition", methods=["GET"])
def page():
    return Response(_PAGE, mimetype="text/html")
