"""
Deal Autopsy (2026-06-03)
=========================
Reactive companion to the Canvas + Sentinel: takes DC Hub's tracked M&A / capex
deal flow and overlays the DCPI grid-reality verdict on each deal's market —
answering "what's the REAL play?" for every announced deal.

The insight no incumbent publishes: a $Xbn acquisition in a market DCPI rates
AVOID with ~48mo to power is a long-dated land/power *option*, not a near-term
build. A deal in a BUILD market with sub-24mo time-to-power is smart money.
Deal flow + grid physics, fused.

Free: the deal + its market's DCPI facts (the hook). Paid: the per-deal
"autopsy read" — the real play, the catch, the smart-money interpretation
(Developer/Pro/Enterprise decision layer).

Built on existing verified data: the `deals` table (real tracked M&A) +
market_power_scores (DCPI) + derive_composite_score. Deterministic — no LLM,
no timeout risk. Best-effort deal->market match; unmatched deals show without
an overlay rather than guessing.

Endpoints:
  GET /api/v1/deal-autopsy   JSON
  GET /deal-autopsy          branded HTML page
"""

import re
import logging
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
deal_autopsy_bp = Blueprint("deal_autopsy", __name__)

_PAID = {"DEVELOPER", "PRO", "ENTERPRISE", "FOUNDING", "ADMIN"}
_UPGRADE_URL = "https://dchub.cloud/upgrade?tool=deal_autopsy"


def _db():
    from main import get_read_db
    try:
        return get_read_db()
    except Exception:
        from main import get_db
        return get_db()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Deal geography is often a STATE (or state name), not a metro — map it so we can
# fall back to that state's representative DCPI market for the grid overlay.
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "newhampshire": "NH", "newjersey": "NJ", "newmexico": "NM", "newyork": "NY",
    "northcarolina": "NC", "northdakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhodeisland": "RI", "southcarolina": "SC",
    "southdakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "westvirginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "districtofcolumbia": "DC",
}


def _market_index():
    """Lookup of normalized market_name / slug / state -> DCPI row."""
    from routes.dcpi import derive_composite_score
    idx = {}
    by_state = {}
    conn = None
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
                    d.get("time_to_power_months"), d.get("verdict"))
            except Exception:
                d["composite_score"] = d.get("excess_power_score") or 0
            for key in (_norm(d.get("market_slug")), _norm(d.get("market_name"))):
                if key and key not in idx:
                    idx[key] = d
            stc = (d.get("state") or "").upper().strip()
            if stc:
                cur = by_state.get(stc)
                if cur is None or (d.get("composite_score") or 0) > (cur.get("composite_score") or 0):
                    by_state[stc] = d
    except Exception as e:
        logger.warning(f"deal-autopsy: market index failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return idx, by_state


def _recent_deals(limit):
    """Recent tracked deals (confirmed columns only). List[dict]."""
    conn = None
    rows = []
    try:
        conn = _db()
        c = conn.cursor()
        c.execute(
            """
            SELECT id, date, buyer, seller, value, type, region, market, source_url
            FROM deals
            WHERE COALESCE(status,'') NOT IN ('rejected','duplicate')
            ORDER BY date DESC NULLS LAST, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in c.description]
        for r in c.fetchall():
            rows.append(dict(zip(cols, r)))
    except Exception as e:
        logger.warning(f"deal-autopsy: deals load failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def _match_market(deal, idx, by_state):
    # 1. Direct metro slug/name match.
    for field in ("market", "region"):
        key = _norm(deal.get(field))
        if key and key in idx:
            return idx[key]
    # 2. State-level fallback: deal geography is often a state (code or name).
    #    Use that state's highest-composite DCPI market as a representative proxy.
    for field in ("market", "region", "state"):
        raw = (deal.get(field) or "").strip()
        if not raw:
            continue
        code = raw.upper() if (len(raw) == 2 and raw.upper() in by_state) else _STATE_NAMES.get(_norm(raw))
        if code and code in by_state:
            m = dict(by_state[code])
            m["_state_proxy"] = code
            return m
    return None


def _autopsy_read(deal, mk):
    """Deterministic 'what's the real play' read. PAID layer."""
    if not mk:
        return ("Location not in DCPI's 233 scored markets — can't grid-check this one. "
                "Treat the headline MW/$ with caution; the binding question is interconnection, "
                "and we have no power verdict here.")
    v = (mk.get("verdict") or "").upper()
    ttp = mk.get("time_to_power_months")
    nm = mk.get("market_name") or mk.get("market_slug")
    ttp_txt = f"~{int(ttp)}mo to power" if isinstance(ttp, (int, float)) and ttp else "unscored time-to-power"
    if v == "AVOID":
        return (f"The real play: this is a long-dated land/power OPTION, not a near-term build. "
                f"DCPI rates {nm} AVOID ({ttp_txt}) — the grid queue is the wall. Smart money here "
                f"is banking the site + chasing behind-the-meter gas (see DCGI), not racing to energize.")
    if v == "CAUTION":
        return (f"The real play: a diligence bet. {nm} is CAUTION ({ttp_txt}) — headroom exists but "
                f"it's constrained. The deal works IF the buyer has line-of-sight on a specific "
                f"substation/feeder; otherwise it's a queue gamble.")
    if v == "BUILD":
        return (f"The real play: this is genuine near-term capacity. {nm} is BUILD-rated ({ttp_txt}) — "
                f"one of the few markets where the grid can actually say yes soon. This is smart money "
                f"moving before the queue tightens.")
    return (f"{nm} is scored {v or 'LOW_SIGNAL'} ({ttp_txt}). Read the deal as exploratory until the "
            f"power verdict firms up.")


def _deal_public(deal, mk):
    val = deal.get("value")
    _d = deal.get("date")
    return {
        # deals.date may be a date/datetime OR already a string — handle both.
        "date": (_d.isoformat() if hasattr(_d, "isoformat") else (str(_d) if _d else None)),
        "buyer": deal.get("buyer"),
        "seller": deal.get("seller"),
        "type": deal.get("type"),
        "value_usd": float(val) if isinstance(val, (int, float)) else None,
        "market_raw": deal.get("market") or deal.get("region"),
        "source_url": deal.get("source_url"),
        "dcpi": ({
            "market": mk.get("market_name"),
            "iso": mk.get("iso"),
            "verdict": mk.get("verdict"),
            "excess_power_score": mk.get("excess_power_score"),
            "time_to_power_months": mk.get("time_to_power_months"),
            "dcpi_url": f"https://dchub.cloud/dcpi/{mk.get('market_slug')}",
            # Honest about basis: a metro-direct match vs the state's representative market.
            "basis": (f"{mk.get('_state_proxy')} state proxy ({mk.get('market_name')})"
                      if mk.get("_state_proxy") else "metro match"),
        } if mk else None),
    }


@deal_autopsy_bp.route("/api/v1/deal-autopsy", methods=["GET", "OPTIONS"])
def autopsy():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        limit = max(1, min(int(request.args.get("limit") or 15), 50))
    except (TypeError, ValueError):
        limit = 15

    deals = _recent_deals(limit)
    if not deals:
        return jsonify(ok=False, error="Deal flow temporarily unavailable"), 503
    idx, by_state = _market_index()

    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
    except Exception:
        tier = "FREE"
    is_paid = (tier or "FREE").upper() in _PAID

    items = []
    matched = 0
    for d in deals:
        mk = _match_market(d, idx, by_state)
        if mk:
            matched += 1
        row = _deal_public(d, mk)
        if is_paid:
            read = _autopsy_read(d, mk)
            if mk and mk.get("_state_proxy"):
                read = (f"[Grid-checked via {mk.get('_state_proxy')}'s representative market, "
                        f"{mk.get('market_name')}] ") + read
            row["autopsy_read"] = read
        items.append(row)

    out = {
        "ok": True,
        "product": "Deal Autopsy",
        "deals": items,
        "count": len(items),
        "grid_checked": matched,
        "tier": tier,
        "citation": "DC Hub Deal Autopsy — dchub.cloud/deal-autopsy (CC BY 4.0)",
    }
    if not is_paid:
        out["autopsy"] = {
            "locked": True,
            "message": ("🎯 The autopsy read is the paid layer. The deal flow + each market's DCPI "
                        "verdict above are free — but the 'what's the REAL play' call (long-dated "
                        "option vs near-term build vs queue gamble) on each deal is Developer/Pro."),
            "unlock": {"url": _UPGRADE_URL, "developer_usd_month": 49, "pro_usd_month": 199},
        }
    return jsonify(out), 200


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deal Autopsy · DC Hub</title>
<meta name="description" content="Every tracked data-center M&A and capex deal, with the DC Hub Power Index grid-reality verdict overlaid: is this a near-term build, a long-dated power option, or a queue gamble?">
<link rel="canonical" href="https://dchub.cloud/deal-autopsy">
<style>
 :root{--bg:#0a0b12;--card:#12141d;--line:rgba(255,255,255,.08);--ink:#e2e8f0;--mut:#94a3b8;--ind:#6366f1;--grn:#34d399;--amb:#fbbf24;--red:#f87171}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:980px;margin:0 auto;padding:32px 20px 80px}
 h1{font-size:1.9rem;margin:.2em 0}.sub{color:var(--mut);max-width:64ch}
 .badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--ind);border:1px solid var(--line);border-radius:20px;padding:3px 12px;margin-bottom:14px}
 .deal{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:12px}
 .deal h3{margin:0 0 4px;font-size:15px}.meta{color:var(--mut);font-size:12.5px}
 .v{font-weight:700;font-size:11px;padding:2px 8px;border-radius:6px;margin-left:6px}
 .BUILD{background:rgba(52,211,153,.16);color:var(--grn)}.CAUTION{background:rgba(251,191,36,.16);color:var(--amb)}.AVOID{background:rgba(248,113,113,.16);color:var(--red)}
 .read{margin-top:8px;font-size:13.5px;color:#cbd5e1;border-left:2px solid var(--ind);padding-left:10px}
 .lock{border-color:rgba(99,102,241,.4)}.cta{display:inline-block;margin-top:8px;background:var(--ind);color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:700}
 a{color:#a5b4fc}
</style></head><body><div class="wrap">
<span class="badge">DC Hub · Deal Autopsy</span>
<h1>What's the real play behind each deal?</h1>
<p class="sub">Every tracked data-center M&A / capex deal, with the DC Hub Power Index grid-reality verdict overlaid. A $bn deal in an AVOID market with a dead queue is a land-banking option, not a build. We tell you which is which.</p>
<div id="out"><p class="sub">Loading deal flow…</p></div>
<script>
async function load(){
 var out=document.getElementById('out');
 try{
  var r=await fetch('/api/v1/deal-autopsy?limit=15');var d=await r.json();
  if(!d.ok){out.innerHTML='<p class="sub">'+(d.error||'No data')+'</p>';return;}
  var h='<p class="sub">'+d.count+' recent deals · '+d.grid_checked+' grid-checked against DCPI.</p>';
  d.deals.forEach(function(x){
   var title=(x.buyer||'?')+(x.seller?(' → '+x.seller):'')+(x.type?(' · '+x.type):'');
   var dcpi=x.dcpi;
   h+='<div class="deal"><h3>'+title+(dcpi&&dcpi.verdict?('<span class="v '+dcpi.verdict+'">'+dcpi.verdict+'</span>'):'')+'</h3>';
   h+='<div class="meta">'+(x.date||'')+(x.value_usd?(' · $'+(x.value_usd/1e9).toFixed(2)+'B'):'')+(x.market_raw?(' · '+x.market_raw):'')+(dcpi?(' · '+(dcpi.iso||'')+' · '+(dcpi.time_to_power_months!=null?'~'+dcpi.time_to_power_months+'mo to power':'')):' · not in DCPI')+'</div>';
   if(x.autopsy_read){h+='<div class="read">'+x.autopsy_read+'</div>';}
   h+='</div>';
  });
  var a=d.autopsy||{};
  if(a.locked){h+='<div class="deal lock"><strong>🎯 Autopsy read locked</strong><p class="sub">'+a.message+'</p><a class="cta" href="'+(a.unlock&&a.unlock.url||'/pricing')+'">Unlock the read — Developer $49/mo →</a></div>';}
  out.innerHTML=h;
 }catch(e){out.innerHTML='<p class="sub">Error: '+e+'</p>';}
}
load();
</script>
<p class="sub" style="margin-top:40px;font-size:12px">Deal flow + <a href="/dcpi">DC Hub Power Index</a>. Data CC BY 4.0. © 2026 DC Hub.</p>
</div></body></html>"""


@deal_autopsy_bp.route("/deal-autopsy", methods=["GET"])
def page():
    return Response(_PAGE, mimetype="text/html")
