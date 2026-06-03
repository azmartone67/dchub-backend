"""
Site Selection Canvas (2026-06-03) — DC Hub's flagship end-to-end siting product.

Fuses the existing DCPI market scores into a guided
    find -> rank -> shortlist -> VERDICT
workflow. Input: capacity target + geography + deadline. Output:

  • a ranked SHORTLIST of markets (FREE — the hook that wins agent citations), and
  • a SYNTHESIS decision layer (PAID — the #1 pick, the *why*, a build sequence,
    and risk flags) gated behind Developer/Pro/Enterprise.

This is the "decision layer is the product" thesis made concrete: raw facts are
free, the judgment is paid.

Built entirely on existing, verified data:
  • market_power_scores (published = true)  — the DCPI table
  • derive_composite_score()                — routes.dcpi (canonical ranking)
  • _resolve_caller_tier()                  — routes.tier_gate (the paywall)
The synthesis narrative is DETERMINISTIC (string-built from the scores), so the
endpoint is fast and has zero dependency on the narrative/LLM path that can time
out. No new tables, no external calls.

Endpoints:
  GET|POST /api/v1/site-selection/canvas   the engine (JSON)
  GET      /site-selection                 branded HTML page
"""

import logging
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
site_selection_canvas_bp = Blueprint("site_selection_canvas", __name__)

# Tiers that unlock the synthesis (decision) layer. FREE/IDENTIFIED get the
# shortlist + a teaser only.
_PAID = {"DEVELOPER", "PRO", "ENTERPRISE", "FOUNDING", "ADMIN"}

_UPGRADE_URL = "https://dchub.cloud/upgrade?tool=site_selection_canvas"


def _db():
    """Read connection — prefer the read replica path, fall back to primary."""
    from main import get_read_db
    try:
        return get_read_db()
    except Exception:
        from main import get_db
        return get_db()


def _load_markets():
    """Every published DCPI market score + the canonical composite. List[dict]."""
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
                    d.get("excess_power_score"),
                    d.get("constraint_score"),
                    d.get("time_to_power_months"),
                    d.get("verdict"),
                )
            except Exception:
                d["composite_score"] = d.get("excess_power_score") or 0
            rows.append(d)
    except Exception as e:
        logger.warning(f"site-selection: market load failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def _rank(markets, region, max_months, verdicts):
    """Filter by region (state OR iso) + deadline + verdict, then rank by composite."""
    out = []
    region = (region or "").strip().upper()
    for m in markets:
        v = (m.get("verdict") or "").upper()
        if verdicts and v not in verdicts:
            continue
        if region and region not in ((m.get("state") or "").upper(), (m.get("iso") or "").upper()):
            continue
        ttp = m.get("time_to_power_months")
        if max_months and ttp and ttp > max_months:
            continue
        out.append(m)
    out.sort(key=lambda r: -(r.get("composite_score") or 0))
    return out


def _row_public(m):
    """The FREE shortlist row — raw facts, no synthesis."""
    return {
        "market": m.get("market_name") or m.get("market_slug"),
        "slug": m.get("market_slug"),
        "state": m.get("state"),
        "iso": m.get("iso"),
        "verdict": m.get("verdict"),
        "excess_power_score": m.get("excess_power_score"),
        "constraint_score": m.get("constraint_score"),
        "time_to_power_months": m.get("time_to_power_months"),
        "composite_score": (round(m["composite_score"], 1)
                            if isinstance(m.get("composite_score"), (int, float)) else None),
        "dcpi_url": f"https://dchub.cloud/dcpi/{m.get('market_slug')}",
    }


def _months(m):
    t = m.get("time_to_power_months")
    return f"~{int(t)}mo" if isinstance(t, (int, float)) and t else "unscored"


def _synthesis(shortlist, capacity_mw, deadline):
    """DETERMINISTIC decision-layer narrative over the real ranked data.

    This is the PAID layer: the pick, the reasoning, the sequence, the risks.
    No LLM — every sentence is derived from the actual scores so it can't
    hallucinate or time out.
    """
    if not shortlist:
        return {
            "verdict": "NO_MATCH",
            "headline": "No market matches your filters.",
            "recommendation": "Widen the geography or extend the deadline — every "
                              "candidate was filtered out by your region/verdict/time-to-power limits.",
            "build_sequence": [],
            "risk_flags": ["Filters too tight — no scored market qualified."],
            "alternatives": [],
        }

    top = shortlist[0]
    nm = top.get("market_name") or top.get("market_slug")
    iso = top.get("iso") or "its ISO"
    st = top.get("state") or ""
    ttp = top.get("time_to_power_months")
    excess = top.get("excess_power_score")
    constraint = top.get("constraint_score")
    verdict = (top.get("verdict") or "").upper()

    cap_txt = f"{int(capacity_mw)}MW" if capacity_mw else "your load"

    headline = f"{nm} ({iso}{(' · ' + st) if st else ''}) is the strongest match for {cap_txt}."

    why = []
    if isinstance(excess, (int, float)):
        why.append(f"excess-power score {round(excess,1)} (higher = more stranded/available headroom)")
    if isinstance(constraint, (int, float)):
        why.append(f"constraint score {round(constraint,1)} (lower = less queue/reserve pressure)")
    if isinstance(ttp, (int, float)) and ttp:
        why.append(f"~{int(ttp)} months to power")
    why_txt = "It leads on " + ", ".join(why) + "." if why else "It leads the composite ranking."

    # Build sequence — grounded in the verdict + time-to-power.
    seq = []
    if verdict == "BUILD":
        seq.append(f"Move now: {nm} carries a BUILD verdict. Secure an interconnection "
                   f"position in {iso} early — the queue is the binding constraint.")
    elif verdict == "CAUTION":
        seq.append(f"Proceed with diligence: {nm} is CAUTION, not BUILD. Validate the "
                   f"specific substation/feeder headroom before committing capital.")
    else:
        seq.append(f"{nm} ranks highest of your filtered set but the verdict is {verdict or 'unscored'} "
                   f"— treat it as a watch, not a commit.")
    if isinstance(ttp, (int, float)) and ttp:
        if deadline and ttp > deadline:
            seq.append(f"⚠ Time-to-power (~{int(ttp)}mo) exceeds your {int(deadline)}-month deadline — "
                       f"behind-the-meter generation (see DCGI gas siting) may be the only path that hits it.")
        else:
            seq.append(f"~{int(ttp)} months to power fits your timeline; lock the queue slot to hold it.")
    seq.append("Cross-check gas-to-power optionality for this state on the DC Hub Gas Index (DCGI) — "
               "behind-the-meter gas is how capacity gets energized when the grid queue runs long.")

    # Risk flags from the data.
    risks = []
    if isinstance(constraint, (int, float)) and constraint >= 50:
        risks.append(f"Elevated constraint score ({round(constraint,1)}) — interconnection/reserve pressure is real here.")
    if isinstance(ttp, (int, float)) and ttp and ttp >= 36:
        risks.append(f"Long time-to-power (~{int(ttp)}mo) — grid energization is the schedule risk.")
    if verdict == "AVOID":
        risks.append("Top candidate is AVOID-rated — the whole filtered set is constrained; widen your search.")
    avoid_nearby = [m for m in shortlist[:8] if (m.get("verdict") or "").upper() == "AVOID"]
    if avoid_nearby and verdict != "AVOID":
        risks.append(f"{len(avoid_nearby)} of your top candidates are AVOID-rated — the region is tightening.")
    if not risks:
        risks.append("No structural red flags in the top candidate's scores — diligence on the specific parcel/feeder still required.")

    alts = []
    for m in shortlist[1:4]:
        alts.append({
            "market": m.get("market_name") or m.get("market_slug"),
            "iso": m.get("iso"), "verdict": m.get("verdict"),
            "time_to_power": _months(m),
            "why": f"composite {round(m['composite_score'],1)}" if isinstance(m.get("composite_score"), (int, float)) else "next best",
        })

    return {
        "verdict": verdict or "RANKED",
        "headline": headline,
        "recommendation": why_txt,
        "build_sequence": seq,
        "risk_flags": risks,
        "alternatives": alts,
    }


def _teaser(shortlist):
    n = len(shortlist)
    top_name = (shortlist[0].get("market_name") or shortlist[0].get("market_slug")) if shortlist else "your top market"
    return {
        "locked": True,
        "message": (f"🎯 The decision layer is locked. You can see the ranked shortlist above "
                    f"({n} markets) for free — but the *answer* (which one to pick, the why, the "
                    f"build sequence, and the risk flags for {top_name}) is the paid layer."),
        "unlock": {
            "url": _UPGRADE_URL,
            "developer_usd_month": 49,
            "pro_usd_month": 199,
            "pitch": "Developer ($49/mo) unlocks the full Site Selection Canvas synthesis + the MCP decision tools.",
        },
    }


@site_selection_canvas_bp.route("/api/v1/site-selection/canvas", methods=["GET", "POST", "OPTIONS"])
def canvas():
    if request.method == "OPTIONS":
        return ("", 204)

    body = request.get_json(silent=True) if request.method == "POST" else None
    body = body or {}

    def _arg(name, default=None):
        v = request.args.get(name)
        if v is None and isinstance(body, dict):
            v = body.get(name)
        return v if v not in (None, "") else default

    try:
        capacity_mw = float(_arg("capacity_mw") or 0) or None
    except (TypeError, ValueError):
        capacity_mw = None
    region = _arg("region") or _arg("state") or _arg("iso")
    try:
        max_months = int(_arg("max_months") or _arg("deadline_months") or 0) or None
    except (TypeError, ValueError):
        max_months = None
    verdict_param = (_arg("verdict") or "BUILD,CAUTION").upper()
    verdicts = {v.strip() for v in verdict_param.split(",") if v.strip()} if verdict_param != "ALL" else None
    try:
        limit = max(1, min(int(_arg("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10

    markets = _load_markets()
    if not markets:
        return jsonify(ok=False, error="DCPI scores temporarily unavailable"), 503

    ranked = _rank(markets, region, max_months, verdicts)
    shortlist_full = ranked[:limit]
    shortlist = [_row_public(m) for m in shortlist_full]

    # Resolve tier for the paywall (best-effort; defaults to FREE).
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
    except Exception:
        tier = "FREE"
    is_paid = (tier or "FREE").upper() in _PAID

    out = {
        "ok": True,
        "product": "Site Selection Canvas",
        "inputs": {
            "capacity_mw": capacity_mw,
            "region": region,
            "max_months": max_months,
            "verdicts": sorted(verdicts) if verdicts else "ALL",
            "limit": limit,
        },
        "universe": len(markets),
        "matched": len(ranked),
        "shortlist": shortlist,
        "tier": tier,
        "citation": "DC Hub Site Selection Canvas — dchub.cloud/site-selection (CC BY 4.0)",
    }
    if is_paid:
        out["synthesis"] = _synthesis(shortlist_full, capacity_mw, max_months)
    else:
        out["synthesis"] = _teaser(shortlist_full)

    return jsonify(out), 200


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site Selection Canvas · DC Hub</title>
<meta name="description" content="Guided data-center site selection: input capacity, geography, and deadline — get a ranked shortlist of US markets with BUILD/CAUTION/AVOID verdicts and time-to-power, powered by the DC Hub Power Index.">
<link rel="canonical" href="https://dchub.cloud/site-selection">
<style>
 :root{--bg:#0a0b12;--card:#12141d;--line:rgba(255,255,255,.08);--ink:#e2e8f0;--mut:#94a3b8;--ind:#6366f1;--grn:#34d399;--amb:#fbbf24;--red:#f87171}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
 h1{font-size:1.9rem;margin:.2em 0}.sub{color:var(--mut);max-width:60ch}
 .badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--ind);border:1px solid var(--line);border-radius:20px;padding:3px 12px;margin-bottom:14px}
 form{display:flex;flex-wrap:wrap;gap:10px;margin:22px 0;align-items:end}
 label{display:block;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
 input,select{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:9px 11px;font-size:14px}
 button{background:var(--ind);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-weight:700;cursor:pointer}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13.5px}
 th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .v{font-weight:700;font-size:11px;padding:2px 8px;border-radius:6px}
 .BUILD{background:rgba(52,211,153,.16);color:var(--grn)}.CAUTION{background:rgba(251,191,36,.16);color:var(--amb)}.AVOID{background:rgba(248,113,113,.16);color:var(--red)}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-top:22px}
 .lock{border-color:rgba(99,102,241,.4)}
 .cta{display:inline-block;margin-top:10px;background:var(--ind);color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:700}
 li{margin:.3em 0}.flag{color:var(--amb)}
 a{color:#a5b4fc}
</style></head><body><div class="wrap">
<span class="badge">DC Hub · Site Selection Canvas</span>
<h1>Where should this data center go?</h1>
<p class="sub">Set your capacity, geography, and deadline. The Canvas ranks US markets by the DC Hub Power Index (excess-power headroom, grid constraint, and time-to-power) and returns a shortlist — free. The verdict and build plan are the paid decision layer.</p>
<form id="f" onsubmit="run(event)">
 <div><label>Capacity (MW)</label><input id="cap" type="number" placeholder="100" style="width:110px"></div>
 <div><label>Region (state or ISO)</label><input id="reg" placeholder="e.g. TX, ERCOT, VA" style="width:160px"></div>
 <div><label>Deadline (months)</label><input id="dl" type="number" placeholder="24" style="width:120px"></div>
 <div><label>Verdicts</label><select id="vd"><option value="BUILD,CAUTION">Build + Caution</option><option value="BUILD">Build only</option><option value="ALL">All</option></select></div>
 <button>Run Canvas →</button>
</form>
<div id="out"><p class="sub">Run the Canvas to see your shortlist.</p></div>
<script>
async function run(e){if(e)e.preventDefault();
 var out=document.getElementById('out');out.innerHTML='<p class="sub">Scoring markets…</p>';
 var qs=new URLSearchParams();
 var cap=document.getElementById('cap').value;if(cap)qs.set('capacity_mw',cap);
 var reg=document.getElementById('reg').value;if(reg)qs.set('region',reg);
 var dl=document.getElementById('dl').value;if(dl)qs.set('max_months',dl);
 qs.set('verdict',document.getElementById('vd').value);qs.set('limit','12');
 try{
  var r=await fetch('/api/v1/site-selection/canvas?'+qs.toString());var d=await r.json();
  if(!d.ok){out.innerHTML='<p class="sub">'+(d.error||'No data')+'</p>';return;}
  var h='<p class="sub">'+d.matched+' of '+d.universe+' scored markets match.</p>';
  h+='<table><thead><tr><th>#</th><th>Market</th><th>ISO</th><th>Verdict</th><th>Excess</th><th>Constraint</th><th>Time-to-power</th></tr></thead><tbody>';
  d.shortlist.forEach(function(m,i){h+='<tr><td>'+(i+1)+'</td><td><a href="'+m.dcpi_url+'">'+(m.market||'')+'</a> <span style="color:#64748b">'+(m.state||'')+'</span></td><td>'+(m.iso||'')+'</td><td><span class="v '+(m.verdict||'')+'">'+(m.verdict||'')+'</span></td><td>'+(m.excess_power_score!=null?m.excess_power_score:'—')+'</td><td>'+(m.constraint_score!=null?m.constraint_score:'—')+'</td><td>'+(m.time_to_power_months!=null?'~'+m.time_to_power_months+'mo':'—')+'</td></tr>';});
  h+='</tbody></table>';
  var s=d.synthesis||{};
  if(s.locked){h+='<div class="card lock"><strong>🎯 Decision layer locked</strong><p class="sub">'+s.message+'</p><a class="cta" href="'+(s.unlock&&s.unlock.url||'/pricing')+'">Unlock the verdict — Developer $49/mo →</a></div>';}
  else if(s.headline){h+='<div class="card"><strong>'+s.headline+'</strong><p>'+(s.recommendation||'')+'</p>';
   if(s.build_sequence&&s.build_sequence.length){h+='<p class="sub">Build sequence:</p><ol>';s.build_sequence.forEach(function(x){h+='<li>'+x+'</li>';});h+='</ol>';}
   if(s.risk_flags&&s.risk_flags.length){h+='<p class="sub">Risk flags:</p><ul>';s.risk_flags.forEach(function(x){h+='<li class="flag">'+x+'</li>';});h+='</ul>';}
   h+='</div>';}
  out.innerHTML=h;
 }catch(err){out.innerHTML='<p class="sub">Error: '+err+'</p>';}
}
</script>
<p class="sub" style="margin-top:40px;font-size:12px">Powered by the <a href="/dcpi">DC Hub Power Index</a> + <a href="/dcgi">Gas Index</a>. Data CC BY 4.0. © 2026 DC Hub.</p>
</div></body></html>"""


@site_selection_canvas_bp.route("/site-selection", methods=["GET"])
def page():
    return Response(_PAGE, mimetype="text/html")
