"""
context_packs.py — token-budgeted market context packs (RAG v1, 2026-07-03).

The agent-shaped read of the Market Brief: instead of the 9-section JSON
blob, GET /api/v1/context/market/<slug>?max_tokens=4000&format=json|md
re-serializes the market_brief._build_brief() fan-out into prose blocks an
LLM can drop straight into context — each with an honest token estimate,
its own as_of timestamp, and a citable URL. Sections are filled greedily in
value-priority order until the budget runs out:

  hero/DCPI verdict → grid/power → outlook (deep-dive paragraphs) → deals →
  pipeline → operators → comps → risk → news top-3

Gate: DEVELOPER+ tier (or the MCP server's X-Internal-Key / admin key /
loopback) gets the full budgeted pack; everyone else gets the free pack —
hero + verdict + 1 news headline (~800 tokens max) + an _upgrade block that
names the locked sections. The MCP get_market_context tool proxies here with
the internal key and applies its own free-tier tease at the MCP layer, so
the tier logic never forks.

Backing data is the same _BRIEF_CACHE-cached fan-out the /brief page uses —
this module adds only a cheap news query (600s cached) and serialization.
"""

from __future__ import annotations

import datetime
import os
import re

from flask import Blueprint, Response, jsonify, request

from routes.url_registry import build_public_url
from utils.cache import BoundedCache

context_packs_bp = Blueprint("context_packs", __name__)

_NEWS_CACHE = BoundedCache(max_size=256, ttl=600)

_DEFAULT_TOKENS = 4000
_MAX_TOKENS = 8000
_MIN_TOKENS = 200
_FREE_TOKENS = 800

_CITE_FOOTER = 'Data: DC Hub (dchub.cloud) — cite as "DC Hub, dchub.cloud"'


def _tok(s: str) -> int:
    """Honest-enough token estimate (chars/4 — the standard approximation)."""
    return max(1, (len(s or "") + 3) // 4)


# ─────────────────────────────────────────────────────────────────────
# Gate — full pack for DEVELOPER+ / internal / admin / loopback.
# Deliberately NOT tier_gate.caller_is_privileged(): that helper also trusts
# the r43-G browser session cookie, which would hand the PRO-gated brief
# sections to any logged-in free web user. Tier rank comes from the same
# _resolve_caller_tier the brief itself uses.
# ─────────────────────────────────────────────────────────────────────

def _full_access() -> bool:
    try:
        from routes.tier_gate import _resolve_caller_tier, _TIER_RANK
        tier, _ = _resolve_caller_tier()
        if _TIER_RANK.get((tier or "").upper(), 0) >= _TIER_RANK.get("DEVELOPER", 2):
            return True
    except Exception:
        pass
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
            return True
    except Exception:
        pass
    try:
        import hmac as _hmac
        got = (request.headers.get("X-Admin-Key") or "").strip()
        exp = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        if got and exp and _hmac.compare_digest(got, exp):
            return True
    except Exception:
        pass
    try:
        # '::ffff:127.0.0.1' is the IPv4-mapped form a dual-stack ([::])
        # listener reports for an IPv4 loopback connect (#2018/#2041).
        if request.remote_addr in ("127.0.0.1", "::1", "localhost",
                                   "::ffff:127.0.0.1"):
            return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────────────────
# Section serializers — each returns (blocks, as_of, cite) where blocks is a
# list of independent text units (paragraphs / lines) the budgeter can cut at.
# ─────────────────────────────────────────────────────────────────────

def _fmt_num(v, suffix=""):
    if v is None:
        return None
    try:
        f = float(v)
        s = f"{f:,.0f}" if abs(f) >= 100 or f == int(f) else f"{f:,.1f}"
        return s + suffix
    except Exception:
        return str(v)


def _market_url(slug: str) -> str:
    return build_public_url("markets", slug)


def _ser_hero(brief: dict) -> tuple[list, str, str]:
    hero = brief.get("hero") or {}
    kpis = brief.get("kpis") or {}
    name = hero.get("name") or brief.get("slug")
    loc = ", ".join([x for x in (hero.get("state"), hero.get("iso")) if x])
    bits = [f"{name}" + (f" ({loc})" if loc else "")
            + f" — DCPI verdict: {hero.get('verdict') or 'N/A'}."]
    scores = []
    if hero.get("excess_power") is not None:
        scores.append(f"excess-power {_fmt_num(hero['excess_power'])}/100")
    if hero.get("constraint_score") is not None:
        scores.append(f"constraint {_fmt_num(hero['constraint_score'])}/100")
    if scores:
        bits.append("DCPI scores: " + ", ".join(scores) + ".")
    kb = []
    if kpis.get("facility_count") is not None:
        kb.append(f"{kpis['facility_count']} tracked facilities")
    if kpis.get("operational_mw"):
        kb.append(f"{_fmt_num(kpis['operational_mw'], ' MW')} operational")
    if kpis.get("pipeline_mw"):
        kb.append(f"{_fmt_num(kpis['pipeline_mw'], ' MW')} in construction/planned")
    top = kpis.get("top_operator") or {}
    if top.get("name"):
        kb.append(f"top operator {top['name']} ({top.get('facility_count')} facilities)")
    if kb:
        bits.append("At a glance: " + "; ".join(kb) + ".")
    as_of = hero.get("computed_at") or (brief.get("live_as_of") or {}).get("iso")
    return [" ".join(bits)], as_of, _market_url(brief.get("slug"))


def _ser_grid(brief: dict) -> tuple[list, str, str]:
    pg = brief.get("power_grid") or {}
    hero = brief.get("hero") or {}
    lines = []
    if pg.get("iso"):
        lines.append(f"Grid: {pg['iso']}.")
    facts = []
    if pg.get("queue_capacity_mw"):
        facts.append(f"interconnection queue capacity {_fmt_num(pg['queue_capacity_mw'], ' MW')}")
    if pg.get("interconnection_pending_mw"):
        facts.append(f"pending/active/study queue {_fmt_num(pg['interconnection_pending_mw'], ' MW')}")
    if pg.get("reserve_margin_pct") is not None:
        facts.append(f"reserve margin {_fmt_num(pg['reserve_margin_pct'], '%')}")
    if pg.get("gen_additions_mw"):
        facts.append(f"generation additions (12mo) {_fmt_num(pg['gen_additions_mw'], ' MW')}")
    if pg.get("queue_wait_months"):
        facts.append(f"queue wait ~{_fmt_num(pg['queue_wait_months'])} months")
    if facts:
        lines.append("Power & grid: " + "; ".join(facts) + ".")
    as_of = hero.get("computed_at") or (brief.get("live_as_of") or {}).get("iso")
    return lines, as_of, _market_url(brief.get("slug"))


def _ser_outlook(brief: dict) -> tuple[list, str, str]:
    ol = brief.get("outlook") or {}
    md = ol.get("narrative_md") or ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", md) if p.strip()]
    return paras, ol.get("generated_at"), _market_url(brief.get("slug")) + "/deep-dive"


def _ser_deals(brief: dict) -> tuple[list, str, str]:
    rows = brief.get("ma") or []
    lines = []
    for d in rows[:10]:
        seg = [x for x in (d.get("buyer"), "→", d.get("seller")) if x]
        line = " ".join(seg) if len(seg) > 1 else (d.get("buyer") or d.get("seller") or "deal")
        extras = []
        if d.get("value"):
            extras.append(f"value {_fmt_num(d['value'])}")
        if d.get("mw"):
            extras.append(f"{_fmt_num(d['mw'])} MW")
        if d.get("type"):
            extras.append(str(d["type"]))
        if d.get("date"):
            extras.append(str(d["date"]))
        lines.append("- " + line + (" (" + ", ".join(extras) + ")" if extras else ""))
    if lines:
        lines.insert(0, "M&A activity (last ~2 years):")
    return lines, None, "https://dchub.cloud/database"


def _ser_pipeline(brief: dict) -> tuple[list, str, str]:
    rows = brief.get("pipeline") or []
    lines = []
    for p in rows[:10]:
        extras = [x for x in (
            _fmt_num(p.get("power_mw"), " MW"), p.get("status"),
            (f"ETA {p.get('eta')}" if p.get("eta") else None)) if x]
        nm = " — ".join([x for x in (p.get("operator"), p.get("facility")) if x]) or "project"
        lines.append("- " + nm + (" (" + ", ".join(extras) + ")" if extras else ""))
    if lines:
        lines.insert(0, "Construction pipeline:")
    return lines, None, _market_url(brief.get("slug"))


def _ser_operators(brief: dict) -> tuple[list, str, str]:
    rows = brief.get("operators") or []
    lines = []
    for o in rows[:5]:
        lines.append(f"- {o.get('operator')}: {o.get('facility_count')} facilities, "
                     f"{_fmt_num(o.get('total_mw'), ' MW')}")
    if lines:
        lines.insert(0, "Operator footprint (top by MW):")
    return lines, None, _market_url(brief.get("slug"))


def _ser_comps(brief: dict) -> tuple[list, str, str]:
    comps = brief.get("comps") or {}
    lines = []
    for label, key in (("Powered shell", "powered_shell"), ("Land", "land")):
        for d in (comps.get(key) or [])[:4]:
            extras = [x for x in (
                (f"${_fmt_num(d.get('value'))}M" if d.get("value") else None),
                _fmt_num(d.get("mw"), " MW"), d.get("date")) if x]
            nm = " → ".join([x for x in (d.get("buyer"), d.get("seller")) if x]) or (d.get("asset") or "comp")
            lines.append(f"- [{label}] {nm}" + (" (" + ", ".join(str(x) for x in extras) + ")" if extras else ""))
    if lines:
        lines.insert(0, "Transaction comps:")
    return lines, None, "https://dchub.cloud/database"


def _ser_risk(brief: dict) -> tuple[list, str, str]:
    r = brief.get("risk") or {}
    facts = []
    if r.get("water_stress") is not None:
        facts.append(f"water-stress score {_fmt_num(r['water_stress'])}")
    if r.get("drought_months_d2_plus") is not None:
        facts.append(f"{r['drought_months_d2_plus']} months at drought D2+")
    if r.get("wildfire_seismic_note"):
        facts.append(r["wildfire_seismic_note"])
    lines = ["Risk factors: " + "; ".join(facts) + "."] if facts else []
    return lines, None, _market_url(brief.get("slug"))


def _market_news(slug: str, name: str) -> list:
    """Top-3 recent news mentioning the market (word-boundary on the city
    part of the name). Cached 600s; best-effort []."""
    city = (name or "").split(",")[0].strip()
    if len(city) < 4:
        return []
    return _news_matching(slug, city)


def _news_matching(cache_key: str, token: str) -> list:
    """Top-3 recent news whose title/summary word-boundary-match token
    (a city or an ISO code — min 3 chars, so PJM/SPP work). Cached 600s."""
    hit = _NEWS_CACHE.get(cache_key)
    if hit is not None:
        return hit
    token = (token or "").strip()
    if len(token) < 3:
        _NEWS_CACHE.set(cache_key, [])
        return []
    out = []
    try:
        import psycopg2
        db = os.environ.get("DATABASE_URL")
        if db:
            with psycopg2.connect(db, sslmode="require", connect_timeout=5) as c:
                with c.cursor() as cur:
                    cur.execute("SET statement_timeout = '5000ms'")
                    pat = r"\y" + re.escape(token) + r"\y"
                    cur.execute("""
                        SELECT title, url, source, published_at, left(coalesce(summary,''), 300)
                          FROM news_articles
                         WHERE title ~* %s OR summary ~* %s
                         ORDER BY published_at DESC NULLS LAST
                         LIMIT 3
                    """, (pat, pat))
                    for t, u, s, p, sm in cur.fetchall():
                        out.append({"title": t, "url": u, "source": s,
                                    "published_at": (p.isoformat() if hasattr(p, "isoformat") else str(p)),
                                    "summary": sm})
    except Exception:
        out = []
    _NEWS_CACHE.set(cache_key, out)
    return out


def _ser_news(news: list) -> tuple[list, str, list]:
    lines = []
    for n in news:
        lines.append(f"- {n['title']} ({n.get('source')}, {str(n.get('published_at'))[:10]}): "
                     f"{n.get('summary') or ''}".rstrip())
    if lines:
        lines.insert(0, "Recent news (top 3):")
    as_of = news[0]["published_at"] if news else None
    cites = [n.get("url") for n in news if n.get("url")]
    return lines, as_of, cites


# ─────────────────────────────────────────────────────────────────────
# Pack assembly — greedy fill in priority order under the token budget.
# ─────────────────────────────────────────────────────────────────────

_SECTION_TITLES = {
    "hero": "Market & DCPI verdict", "grid": "Power & grid", "outlook": "12-month outlook",
    "deals": "M&A activity", "pipeline": "Construction pipeline", "operators": "Operator footprint",
    "comps": "Transaction comps", "risk": "Risk factors", "news": "Recent news",
}
_PACK_ORDER = ("hero", "grid", "outlook", "deals", "pipeline", "operators", "comps", "risk", "news")
# blocks are LINES here (join "\n"); everything else is paragraphs ("\n\n")
_LIST_SECTIONS = {"deals", "pipeline", "operators", "comps", "news"}


def _candidates(brief: dict, news: list) -> list:
    return [
        ("hero", *_ser_hero(brief)),
        ("grid", *_ser_grid(brief)),
        ("outlook", *_ser_outlook(brief)),
        ("deals", *_ser_deals(brief)),
        ("pipeline", *_ser_pipeline(brief)),
        ("operators", *_ser_operators(brief)),
        ("comps", *_ser_comps(brief)),
        ("risk", *_ser_risk(brief)),
        ("news", *_ser_news(news)),
    ]


def _assemble(cands: list, max_tokens: int,
              titles: dict | None = None,
              list_sections: set | None = None) -> tuple[list, list, int]:
    """Greedy fill: whole section if it fits; otherwise cut at block
    boundaries (min 2 blocks for list sections so a lone header never ships);
    otherwise omit. Returns (sections, omitted_ids, used_tokens).
    titles/list_sections default to the market-pack tables; the ISO pack
    passes its own (kept as params — module tables must stay untouched,
    requests run concurrently)."""
    titles = _SECTION_TITLES if titles is None else titles
    list_sections = _LIST_SECTIONS if list_sections is None else list_sections
    sections, omitted, used = [], [], 0
    for sid, blocks, as_of, cite in cands:
        if not blocks:
            continue
        budget = max_tokens - used
        if budget <= 20:
            omitted.append(sid)
            continue
        kept, t = [], 0
        for b in blocks:
            bt = _tok(b)
            if t + bt > budget:
                break
            kept.append(b)
            t += bt
        # a list section whose header fit but no rows did is noise — drop it
        if not kept or (len(blocks) > 1 and len(kept) == 1 and blocks[0].endswith(":")):
            omitted.append(sid)
            continue
        text = ("\n" if sid in list_sections else "\n\n").join(kept)
        sec = {"id": sid, "title": titles.get(sid, sid), "text": text,
               "tokens": _tok(text), "as_of": as_of, "cite": cite}
        if len(kept) < len(blocks):
            sec["truncated"] = f"{len(kept)}/{len(blocks)} blocks within budget"
        sections.append(sec)
        used += sec["tokens"]
    return sections, omitted, used


def _to_markdown(name: str, slug: str, sections: list, used: int, max_tokens: int,
                 kind: str = "market") -> str:
    out = [f"# {name} — DC Hub {kind} context",
           f"_~{used} tokens of a {max_tokens}-token budget · {_CITE_FOOTER}_", ""]
    for s in sections:
        meta = []
        if s.get("as_of"):
            meta.append(f"as of {str(s['as_of'])[:10]}")
        cite = s.get("cite")
        cite_s = ", ".join(cite) if isinstance(cite, list) else cite
        if cite_s:
            meta.append(f"source: {cite_s}")
        out.append(f"## {s['title']}" + (f"  ({'; '.join(meta)})" if meta else ""))
        out.append(s["text"])
        out.append("")
    return "\n".join(out)


@context_packs_bp.route("/api/v1/context/market/<slug>", methods=["GET"])
def market_context_pack(slug):
    try:
        from routes.market_brief import _build_brief, _canonical
    except Exception as e:
        return jsonify(ok=False, error=f"brief_unavailable: {str(e)[:120]}"), 200
    try:
        max_tokens = max(_MIN_TOKENS, min(_MAX_TOKENS, int(request.args.get("max_tokens", _DEFAULT_TOKENS))))
    except Exception:
        max_tokens = _DEFAULT_TOKENS
    fmt = (request.args.get("format") or "json").strip().lower()
    # force_free=1: a privileged caller (the MCP resources surface) asks for the
    # FREE-shaped pack explicitly — dchub://markets/{slug} is a free GEO surface,
    # so it must never ship the full paid pack even though the fetch is internal.
    full = _full_access() and request.args.get("force_free") != "1"
    if not full:
        max_tokens = min(max_tokens, _FREE_TOKENS)

    canonical = _canonical(slug)
    # ADMIN tier → all 9 sections from the (cached) fan-out; presentation
    # gating happens below, server-side, so nothing leaks in transit.
    brief = _build_brief(canonical, "ADMIN")
    if not brief.get("ok"):
        return jsonify(ok=False, slug=canonical, error=brief.get("error") or "unavailable",
                       sample_markets=brief.get("sample_markets")), 404

    name = (brief.get("hero") or {}).get("name") or canonical
    news = _market_news(canonical, name)

    # Free pack: hero + VERDICT + 1 news headline — numeric DCPI scores are Pro
    # site-wide (r-gate-everywhere / L12: the prose embeds the score), so the
    # free hero is serialized from a score-masked copy. Locked sections are
    # NAMED (not silently absent) so an agent knows what an upgrade buys.
    def _free_candidates():
        masked = {**brief, "hero": {**(brief.get("hero") or {}),
                                    "excess_power": None, "constraint_score": None}}
        return [c for c in _candidates(masked, news[:1]) if c[0] in ("hero", "news")]

    _locked = [s for s in _PACK_ORDER if s not in ("hero", "news")]
    _upgrade = {
        "locked_sections": _locked,
        "message": ("Free pack = market verdict + 1 headline (~800 tokens). The full "
                    f"{_DEFAULT_TOKENS}-token pack adds {', '.join(_SECTION_TITLES[s] for s in _locked)} "
                    "— Developer ($49/mo) or the $10/1,000-call pack unlocks it."),
        "upgrade_url": "https://dchub.cloud/pricing?utm_source=context_pack&utm_medium=" + canonical,
    }

    cands = _candidates(brief, news) if full else _free_candidates()
    sections, omitted, used = _assemble(cands, max_tokens)
    out = {
        "ok": True,
        "market": canonical,
        "name": name,
        "tier": "full" if full else "free",
        "max_tokens": max_tokens,
        "used_tokens": used,
        "sections": sections,
        "omitted": omitted or None,
        "as_of": (brief.get("live_as_of") or {}).get("iso"),
        "_cite": _CITE_FOOTER,
    }
    if not full:
        out["_upgrade"] = _upgrade
    else:
        # For the MCP layer: the exact free-tier shape, pre-built server-side,
        # so buildDepthTease (server.mjs) can tease sub-Developer callers
        # without re-deriving the masking rules in JS.
        _fsec, _, _fused = _assemble(_free_candidates(), _FREE_TOKENS)
        out["_free_preview"] = {"sections": _fsec, "used_tokens": _fused,
                                "locked_sections": _locked}
    if fmt == "md":
        return Response(_to_markdown(name, canonical, sections, used, max_tokens),
                        content_type="text/markdown; charset=utf-8")
    return jsonify(out), 200


# ─────────────────────────────────────────────────────────────────────
# ISO context packs (RAG v1 part 2, 2026-07-03) — the grid-side twin of the
# market pack. Same assembler/budget/gate machinery; data comes from the
# existing internal endpoints (loopback self-fetch, the grid-intel headroom
# precedent) fanned out in parallel + two direct SQL reads. Cached 300s.
# ─────────────────────────────────────────────────────────────────────

_ISO_PACK_CACHE = BoundedCache(max_size=32, ttl=300)
_SUPPORTED_ISOS = ("PJM", "ERCOT", "CAISO", "MISO", "SPP", "NYISO", "ISO-NE")
# word-boundary news token per ISO (ISO-NE articles say "New England")
_ISO_NEWS_TOKEN = {"ISO-NE": "New England"}


def _norm_iso(s: str) -> str | None:
    u = re.sub(r"[^A-Z]", "", (s or "").upper())
    u = {"ISONE": "ISO-NE", "NEISO": "ISO-NE"}.get(u, u)
    return u if u in _SUPPORTED_ISOS else None


def _self_get(path: str, timeout: int = 6):
    """Loopback fetch of one of our own endpoints — trusted (remote_addr
    loopback) so gated internals return full data. Best-effort None."""
    import urllib.request
    import json as _j
    base = f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    try:
        req = urllib.request.Request(base + path,
                                     headers={"User-Agent": "dchub-context-pack/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _j.loads(r.read())
    except Exception:
        return None


def _iso_gather(iso: str) -> dict:
    """Parallel fan-out: grid intelligence + DCPI iso-comparison + queue + LMP
    (loopback), top DCPI markets (SQL), narratives (RAG), news (SQL)."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            "gi":  ex.submit(_self_get, f"/api/v1/grid/intelligence/{iso.lower()}"),
            "cmp": ex.submit(_self_get, "/api/v1/dcpi/iso-comparison"),
            "q":   ex.submit(_self_get, f"/api/v1/interconnection-queue/by-iso?iso={iso}"),
            "lmp": ex.submit(_self_get, f"/api/v1/lmp/prices?iso={iso}"),
        }
        for k, f in futs.items():
            try:
                out[k] = f.result(timeout=8)
            except Exception:
                out[k] = None
    # top DCPI markets in this ISO — latest row per slug, BUILD first
    out["markets"] = []
    try:
        import psycopg2
        db = os.environ.get("DATABASE_URL")
        if db:
            with psycopg2.connect(db, sslmode="require", connect_timeout=5) as c:
                with c.cursor() as cur:
                    cur.execute("SET statement_timeout = '5000ms'")
                    cur.execute("""
                        SELECT market_slug, market_name, verdict FROM (
                            SELECT DISTINCT ON (market_slug)
                                   market_slug, market_name, verdict
                              FROM market_power_scores
                             WHERE REPLACE(UPPER(COALESCE(iso,'')), '_', '-') IN (%s, REPLACE(%s, '-', ''))
                             ORDER BY market_slug, computed_at DESC
                        ) t
                        ORDER BY CASE COALESCE(verdict,'')
                                 WHEN 'BUILD' THEN 0 WHEN 'CAUTION' THEN 1
                                 WHEN 'AVOID' THEN 2 ELSE 3 END, market_slug
                        LIMIT 8
                    """, (iso, iso))
                    out["markets"] = [{"slug": r[0], "name": r[1], "verdict": r[2]}
                                      for r in cur.fetchall()]
    except Exception:
        pass
    # deep-dive narrative chunks about this ISO's markets — RAG-retrieved
    out["narratives"] = []
    try:
        from routes.brain_rag import retrieve_context
        out["narratives"] = retrieve_context(
            f"data center market outlook {iso} grid power constraint",
            k=2, corpus="market_narratives")
    except Exception:
        pass
    token = _ISO_NEWS_TOKEN.get(iso, iso)
    out["news"] = _news_matching(f"iso:{iso}", token)  # 3-char tokens OK (PJM/SPP)
    return out


def _iso_candidates(iso: str, g: dict) -> list:
    gi, cmp_all, q, lmp = g.get("gi") or {}, g.get("cmp") or {}, g.get("q") or {}, g.get("lmp") or {}
    # headline — live demand + fuel mix shares (public EIA facts)
    lines = []
    as_of_head = gi.get("demand_period")
    if gi.get("demand_mw"):
        bits = [f"{iso} live demand {_fmt_num(gi['demand_mw'], ' MW')}"]
        if gi.get("peak_mw"):
            bits.append(f"24h peak {_fmt_num(gi['peak_mw'], ' MW')}")
        if gi.get("load_factor") is not None:
            bits.append(f"load factor {gi['load_factor']}")
        lines.append(", ".join(bits) + ".")
    mix = gi.get("generation_mix") or {}
    tot = sum((v or {}).get("mw", 0) or 0 for v in mix.values())
    if tot > 0:
        shares = sorted(((k, (v or {}).get("mw", 0) or 0) for k, v in mix.items()),
                        key=lambda x: -x[1])[:5]
        fuel = {"NG": "gas", "NUC": "nuclear", "COL": "coal", "WND": "wind",
                "SUN": "solar", "WAT": "hydro", "OIL": "oil", "OTH": "other", "BAT": "battery"}
        lines.append("Fuel mix: " + ", ".join(
            f"{fuel.get(k, k.lower())} {round(100.0 * v / tot)}%" for k, v in shares) + ".")
    headline = lines

    # dcpi — this ISO's row from the comparison
    dcpi = []
    row = next((r for r in (cmp_all.get("isos") or [])
                if _norm_iso(str(r.get("iso"))) == iso), None)
    if row:
        facts = [f"{row.get('market_count')} tracked DCPI markets "
                 f"({row.get('build_count')} BUILD / {row.get('caution_count')} CAUTION / "
                 f"{row.get('avoid_count')} AVOID)"]
        if row.get("avg_queue_wait_months"):
            facts.append(f"avg queue wait ~{_fmt_num(row['avg_queue_wait_months'])} months")
        if row.get("avg_kwh_cents"):
            facts.append(f"avg power cost {_fmt_num(row['avg_kwh_cents'])}¢/kWh")
        if row.get("avg_reserve_margin_pct"):
            facts.append(f"reserve margin {_fmt_num(row['avg_reserve_margin_pct'], '%')}")
        if row.get("total_stranded_capacity_mw"):
            facts.append(f"stranded capacity {_fmt_num(row['total_stranded_capacity_mw'], ' MW')}")
        if row.get("total_gen_additions_12mo_mw"):
            facts.append(f"12mo generation additions {_fmt_num(row['total_gen_additions_12mo_mw'], ' MW')}")
        dcpi = ["DCPI: " + "; ".join(str(f) for f in facts) + "."]

    # queue — total GW + largest projects
    queue = []
    if q.get("queued_load_total_gw"):
        queue.append(f"Interconnection queue: {_fmt_num(q['queued_load_total_gw'])} GW queued "
                     f"across {q.get('project_count')} active projects.")
    if q.get("queued_load_data_center_gw"):
        queue.append(f"Large-load (data-center) queue: {_fmt_num(q['queued_load_data_center_gw'])} GW.")
    for p in (q.get("projects") or [])[:3]:
        queue.append(f"- {p.get('project_name')} ({_fmt_num(p.get('capacity_mw'), ' MW')}, "
                     f"{p.get('fuel_type')}, {p.get('state')}, {p.get('queue_status')})")

    # prices — real-time benchmark LMP
    prices = []
    lrow = ((lmp.get("prices") or {}).get(iso)) or {}
    if lrow.get("price") is not None:
        prices = [f"Real-time benchmark LMP: ${lrow['price']}/MWh "
                  f"({lrow.get('location_type', 'hub')}, 5-min interval)."]

    # markets — verdicts are free-class; numeric scores never appear here
    markets = []
    for m in g.get("markets") or []:
        markets.append(f"- {m['name']} — {m.get('verdict') or 'N/A'} "
                       f"({_market_url(m['slug'])})")
    if markets:
        markets.insert(0, f"Tracked DCPI markets on {iso}:")

    # narratives — RAG-retrieved deep-dive prose about this grid's markets
    narr, narr_cites = [], []
    for n in g.get("narratives") or []:
        narr.append((n.get("text") or "").strip())
        slug_part = str(n.get("source_id") or "").split("#")[0]
        if slug_part:
            narr_cites.append(build_public_url("markets", slug_part, subpath="deep-dive"))

    news_blocks, news_as_of, news_cites = _ser_news(g.get("news") or [])

    return [
        ("headline", headline, as_of_head, "https://dchub.cloud/live"),
        ("dcpi", dcpi, cmp_all.get("as_of"), "https://dchub.cloud/dcpi/iso-comparison"),
        ("queue", queue, q.get("as_of"), q.get("source_url") or "https://dchub.cloud/land-power-map"),
        ("prices", prices, lrow.get("interval_ending_utc"), "https://dchub.cloud/live"),
        ("markets", markets, None, "https://dchub.cloud/markets"),
        ("narratives", narr, None, narr_cites),
        ("news", news_blocks, news_as_of, news_cites),
    ]


_ISO_SECTION_TITLES = {
    "headline": "Live grid snapshot", "dcpi": "DCPI verdicts & grid economics",
    "queue": "Interconnection queue", "prices": "Real-time prices",
    "markets": "Tracked markets", "narratives": "Market deep-dive excerpts",
    "news": "Recent news",
}
_ISO_PACK_ORDER = ("headline", "dcpi", "queue", "prices", "markets", "narratives", "news")
_ISO_LIST_SECTIONS = {"queue", "markets", "news"}


@context_packs_bp.route("/api/v1/context/iso/<iso_raw>", methods=["GET"])
def iso_context_pack(iso_raw):
    iso = _norm_iso(iso_raw)
    if not iso:
        return jsonify(ok=False, error="unsupported iso",
                       supported=list(_SUPPORTED_ISOS)), 400
    try:
        max_tokens = max(_MIN_TOKENS, min(_MAX_TOKENS, int(request.args.get("max_tokens", _DEFAULT_TOKENS))))
    except Exception:
        max_tokens = _DEFAULT_TOKENS
    fmt = (request.args.get("format") or "json").strip().lower()
    full = _full_access() and request.args.get("force_free") != "1"
    if not full:
        max_tokens = min(max_tokens, _FREE_TOKENS)

    g = _ISO_PACK_CACHE.get(iso)
    if g is None:
        g = _iso_gather(iso)
        _ISO_PACK_CACHE.set(iso, g)
    cands = _iso_candidates(iso, g)

    def _assemble_iso(cs, budget):
        return _assemble(cs, budget, _ISO_SECTION_TITLES, _ISO_LIST_SECTIONS)

    free_ids = ("headline", "news")
    _locked = [s for s in _ISO_PACK_ORDER if s not in free_ids]
    if full:
        sections, omitted, used = _assemble_iso(cands, max_tokens)
    else:
        # free pack: live public grid facts + 1 headline (parity with the
        # free get_grid_data class); DCPI/queue/prices/narratives are the paid
        # synthesis line.
        free_cands = []
        for c in cands:
            if c[0] == "headline":
                free_cands.append(c)
            elif c[0] == "news":
                free_cands.append((c[0], c[1][:2], c[2], c[3]))  # header + 1 item
        sections, omitted, used = _assemble_iso(free_cands, max_tokens)

    out = {
        "ok": True,
        "iso": iso,
        "tier": "full" if full else "free",
        "max_tokens": max_tokens,
        "used_tokens": used,
        "sections": sections,
        "omitted": omitted or None,
        "_cite": _CITE_FOOTER,
    }
    if not full:
        out["_upgrade"] = {
            "locked_sections": _locked,
            "message": ("Free pack = live grid snapshot + 1 headline. The full "
                        f"{_DEFAULT_TOKENS}-token pack adds "
                        f"{', '.join(_ISO_SECTION_TITLES[s] for s in _locked)} "
                        "— Developer ($49/mo) or the $10/1,000-call pack unlocks it."),
            "upgrade_url": "https://dchub.cloud/pricing?utm_source=context_pack&utm_medium=iso-" + iso.lower(),
        }
    else:
        free_cands = [c if c[0] == "headline" else (c[0], c[1][:2], c[2], c[3])
                      for c in cands if c[0] in free_ids]
        _fsec, _, _fused = _assemble_iso(free_cands, _FREE_TOKENS)
        out["_free_preview"] = {"sections": _fsec, "used_tokens": _fused,
                                "locked_sections": _locked}
    if fmt == "md":
        return Response(_to_markdown(iso, iso.lower(), sections, used, max_tokens, kind="grid"),
                        content_type="text/markdown; charset=utf-8")
    return jsonify(out), 200


# ── light market list — backs the dchub://markets/{slug} resource listing ──
_MARKET_LIST_CACHE = BoundedCache(max_size=2, ttl=600)


@context_packs_bp.route("/api/v1/context/markets", methods=["GET"])
def context_markets_list():
    hit = _MARKET_LIST_CACHE.get("all")
    if hit is None:
        hit = []
        try:
            import psycopg2
            db = os.environ.get("DATABASE_URL")
            if db:
                with psycopg2.connect(db, sslmode="require", connect_timeout=5) as c:
                    with c.cursor() as cur:
                        cur.execute("SET statement_timeout = '5000ms'")
                        cur.execute("""
                            SELECT market_slug, market_name, verdict FROM (
                                SELECT DISTINCT ON (market_slug)
                                       market_slug, market_name, verdict
                                  FROM market_power_scores
                                 ORDER BY market_slug, computed_at DESC
                            ) t ORDER BY market_slug
                        """)
                        hit = [{"slug": r[0], "name": r[1], "verdict": r[2]}
                               for r in cur.fetchall()]
        except Exception:
            hit = []
        _MARKET_LIST_CACHE.set("all", hit)
    return jsonify(ok=True, count=len(hit), markets=hit,
                   isos=list(_SUPPORTED_ISOS), _cite=_CITE_FOOTER), 200
