"""
Phase r42 (2026-05-25) — Auto-narrative for monthly + quarterly reports.

Closes the depth gap vs CBRE/JLL: their reports earn license fees because
of senior-analyst prose, not raw data. We have the raw data; we add a
~250-word analyst-style narrative_summary on each report, generated from
the structured data by claude-haiku-4-5. Cost ~$0.001/report; cached
in-memory for 1 hour so the next 1000 readers all hit cache.

Public API:
    attach_narrative(report_dict, kind="monthly") -> dict (in-place)

Adds a top-level `narrative_summary` field shaped like:
    {
        "text": "<250-word analyst prose>",
        "model": "claude-haiku-4-5-20251001",
        "generated_at": "2026-05-25T18:42:00Z",
        "cache_age_seconds": 0,
    }

If ANTHROPIC_API_KEY is not set OR the LLM call fails, the field is
omitted entirely (no half-broken state).
"""

import os
import time
import logging
import datetime as _dt
import json

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_MODEL = "claude-haiku-4-5-20251001"
_CACHE_TTL = 3600  # 1 hour
_CACHE: dict[str, dict] = {}  # key -> {"text", "computed_at"}


def _cache_key(kind: str, d: dict, audience: str = "default") -> str:
    """Stable key per (kind, period, audience). Monthly uses year+month
    if present; quarterly + comprehensive monthly fall back to generated_at
    date so we don't share cache across days. Audience suffix lets us
    cache per-tone variants (journalist/pe/agent) independently."""
    suffix = f":{audience}" if audience and audience != "default" else ""
    if kind == "monthly" and d.get("year") and d.get("month"):
        return f"monthly:{d.get('year')}:{d.get('month')}{suffix}"
    gen = (d.get("generated_at") or d.get("as_of_date") or "unknown")[:10]
    return f"{kind}:{gen}{suffix}"


# r42e (2026-05-25): audience-specific tone overrides. The structured-
# data is identical; only the prompt voice changes. Each audience gets
# its own cache slot so we don't have to invalidate when toggling.
_AUDIENCE_HEADERS = {
    "journalist": (
        "Voice: like a senior reporter at the Wall Street Journal or "
        "Bloomberg writing the lede paragraph of a sector piece. Lead "
        "with the most quotable sentence — one a reporter could lift "
        "into their own copy with attribution. Concrete names, "
        "concrete numbers, no jargon. Avoid hedging."
    ),
    "pe": (
        "Voice: like a senior private-equity analyst writing a deal-"
        "committee memo. Lead with the capital-flow signal. Frame "
        "everything in terms of returns, risk-adjusted basis, multiples, "
        "and exit windows. Name acquirer types (sovereign, mega-cap, "
        "growth-PE, REIT) and capital-stack implications. Take a "
        "directional position on where the basis is going."
    ),
    "agent": (
        "Voice: like a research analyst writing a structured digest for "
        "an LLM reader. Use one or two short sentences per claim. Surface "
        "named entities (markets, companies, ISOs, MW figures) explicitly. "
        "Prefer parseable phrasing. Still 2 paragraphs, still prose — "
        "but optimized so an AI agent can extract facts cleanly."
    ),
}


def _audience_block(audience: str) -> str:
    """Return the audience-tone block to inject into the base prompt."""
    return _AUDIENCE_HEADERS.get(audience, "")


def _build_monthly_prompt(d: dict, audience: str = "default") -> str:
    """Strip the report dict to the analyst-relevant signals and ask
    Claude to write 250 words in CBRE/JLL house style — but with the
    explicit positioning that we are *complementary*, not competing."""
    h = d.get("headline") or {}
    df = d.get("deal_flow") or {}
    curr = df.get("current") or {}
    prior = df.get("prior") or {}
    movers = d.get("dcpi_movers") or []
    top_mkts = (d.get("top_markets") or [])[:5]
    top_deals = (d.get("top_deals") or [])[:3]
    ai = d.get("ai_traffic") or {}
    vsp = d.get("vs_proprietary_research") or {}
    label = d.get("month_label") or f"{d.get('year')}-{d.get('month')}"

    # Compress to a tight JSON block — model reasons better on dense data
    facts = {
        "month": label,
        "facilities_total": h.get("facilities_total"),
        "total_mw_global": h.get("total_mw"),
        "facilities_added_month": h.get("facilities_added_month"),
        "deals_this_month": curr.get("count"),
        "deal_value_b_this_month": curr.get("value"),
        "deal_mw_this_month": curr.get("mw"),
        "deals_prior_month": prior.get("count"),
        "deals_mom_pct": df.get("deals_mom_pct"),
        "value_mom_pct": df.get("value_mom_pct"),
        "deals_yoy_pct": df.get("deals_yoy_pct"),
        "top_markets_by_mw": [{"market": m.get("market"), "mw": m.get("total_mw"),
                               "facilities": m.get("facilities")} for m in top_mkts],
        "top_deals": [{"buyer": t.get("buyer"), "seller": t.get("seller"),
                       "value_m": t.get("value"), "date": t.get("date")}
                      for t in top_deals],
        "dcpi_movers": [{"market": m.get("market"), "delta": m.get("delta")}
                        for m in movers if not m.get("sentinel")],
        "ai_tool_calls_month": ai.get("tool_calls_month"),
        "ai_mom_pct": ai.get("mom_pct"),
    }
    audience_block = _audience_block(audience)
    return f"""You are a senior research analyst at DC Hub, a data center
intelligence platform. You are drafting the executive summary for the
{label} monthly trend report. Your reader is a hyperscaler exec, a
real-estate analyst, or a journalist covering the data-center beat.

Write a 250-word, 2-paragraph executive summary in the voice of a CBRE
or JLL H2 outlook — confident, specific, sober, no hype. Lead with the
single most important signal from the data below. Name specific markets
and specific dollar/MW figures. Avoid generic phrases like "robust" or
"strong activity" — say *what* and *how much*.

{audience_block}

Paragraph 1 — THE MONTH: the headline number and the story behind it.
What did capital do? Where did it land? Which markets stood out?
Paragraph 2 — THE SIGNAL: what does this month mean for capacity,
power, or M&A in the next 2 quarters? Be willing to take a position.

DO NOT:
- Repeat the totals in both paragraphs
- Use bullets or headers
- Mention "DC Hub" or our platform in the prose (the report is ours)
- Hallucinate any number not in the facts block

Facts (all live as of {d.get('as_of_date')}):
{json.dumps(facts, indent=2, default=str)}

Positioning context (do not quote, but inform your tone):
{json.dumps({"we_cover": vsp.get("we_cover", []),
              "we_dont": vsp.get("they_cover_we_dont_yet", []),
              "edge": vsp.get("edge_vs_them", {})}, indent=2)}

Write only the 2 paragraphs. No preamble, no sign-off.
"""


def _build_quarterly_prompt(d: dict, audience: str = "default") -> str:
    """Quarterly deep-dive narrative — built on the comprehensive_report
    data shape (window, top_build_markets, hyperscaler_deals, etc.)."""
    window = d.get("window", "quarter")
    window_days = d.get("window_days", 90)
    label = f"Q{(_dt.date.today().month - 1)//3 + 1} {_dt.date.today().year}"

    build_top = (d.get("top_build_markets") or [])[:5]
    avoid_top = (d.get("top_avoid_markets") or [])[:5]
    hyperscaler = (d.get("hyperscaler_deals") or [])[:5]
    ma_deals = (d.get("ma_top_deals") or [])[:5]
    pipeline = d.get("pipeline_by_status") or []
    operators = (d.get("top_operators") or [])[:5]

    facts = {
        "label": label,
        "window_days": window_days,
        "facilities_tracked": d.get("total_facilities"),
        "facilities_added_this_window": d.get("facilities_added"),
        "markets_scored_dcpi": d.get("markets_scored"),
        "verdict_distribution": d.get("verdicts"),
        "top_build_markets": [{"market": m.get("market"), "iso": m.get("iso"),
                               "score": m.get("score")} for m in build_top],
        "top_avoid_markets": [{"market": m.get("market"),
                               "reason": m.get("reason") or m.get("verdict")}
                              for m in avoid_top],
        "ma_count": d.get("ma_count"),
        "ma_total_value_m": d.get("ma_total_value_m"),
        "ma_window_used": d.get("ma_window_used"),
        "biggest_ma_deals": [{"buyer": x.get("buyer"), "target": x.get("target"),
                              "value_m": x.get("value_m")} for x in ma_deals],
        "hyperscaler_1b_plus_deals": [{"buyer": h.get("buyer"),
                                       "target": h.get("target"),
                                       "value_b": h.get("value_b"),
                                       "mw": h.get("mw")}
                                      for h in hyperscaler],
        "pipeline_by_status": pipeline,
        "top_operators": [{"operator": o.get("operator"),
                           "facilities": o.get("facilities"),
                           "mw": o.get("mw")} for o in operators],
        "press_releases_this_window": d.get("press_count"),
    }
    audience_block = _audience_block(audience)
    return f"""You are a senior research analyst at DC Hub drafting the
executive summary for the {label} {window} deep-dive (a {window_days}-day
window). Reader: a hyperscaler CFO, a data-center private-equity partner,
or a sector journalist. Voice: CBRE/JLL H2 outlook — confident, specific,
willing to take a position, no hype.

{audience_block}

Write 350 words across 3 paragraphs:

Paragraph 1 — THE WINDOW: what was the single biggest shift in this
{window_days}-day window? Pick from {{capacity, capital, market verdicts}}.
Name specific MW, dollars, companies, markets.
Paragraph 2 — THE STRUCTURAL SHIFT: what does this window reveal about
the next 2-4 quarters? Pick one theme (power gap, hyperscaler vs PE
capital, BUILD/AVOID verdict shift, M&A consolidation, etc.) and develop
it. Use the verdict_distribution + DCPI movements as evidence.
Paragraph 3 — THE WATCH LIST: which 2-3 markets or signals should the
reader monitor next, and why? Pull from top_build_markets,
top_avoid_markets, or hyperscaler_1b_plus_deals.

DO NOT:
- Use bullets or section headers
- Reference DC Hub by name in the prose
- Hallucinate any number not in the facts block
- Hedge — take a position

Facts (live as of {d.get('generated_at', '')[:10]}):
{json.dumps(facts, indent=2, default=str)}

Write only the 3 paragraphs. No preamble, no sign-off.
"""


def _call_claude(prompt: str) -> str | None:
    """Single Claude call. Returns narrative text or None on failure.
    Uses haiku — cheap, fast, plenty for analyst prose."""
    if not _ANTHROPIC_KEY:
        return None
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        if r.status_code != 200:
            logger.warning(f"narrative API {r.status_code}: {r.text[:200]}")
            return None
        j = r.json() or {}
        blocks = j.get("content") or []
        text_parts = [b.get("text") for b in blocks if b.get("type") == "text"]
        return ("".join(text_parts)).strip() or None
    except Exception as e:
        logger.warning(f"narrative call failed: {e}")
        return None


def attach_narrative(d: dict, kind: str = "monthly",
                       audience: str = "default") -> dict:
    """Add `narrative_summary` to the report dict, in-place.

    audience: 'default' | 'journalist' | 'pe' | 'agent'
      - default:    CBRE/JLL house style (sober analyst voice)
      - journalist: WSJ/Bloomberg lede style (quotable, concrete)
      - pe:         Deal-committee memo (capital flow + basis + exit windows)
      - agent:      LLM-reader friendly (named entities surfaced, parseable)

    - Cache hit (within TTL): adds the field instantly with cache_age_seconds
    - Cache miss + key present: synchronous LLM call (~3-5s on haiku), then cache
    - Cache miss + no key: omits the field entirely (no half-broken state)
    - LLM failure: omits the field
    """
    if not isinstance(d, dict):
        return d
    if not _ANTHROPIC_KEY:
        return d  # silent — no field added

    key = _cache_key(kind, d, audience)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and (now - cached["computed_at"]) < _CACHE_TTL:
        d["narrative_summary"] = {
            "text": cached["text"],
            "model": _MODEL,
            "audience": audience,
            "generated_at": cached["generated_at"],
            "cache_age_seconds": int(now - cached["computed_at"]),
        }
        return d

    # Cache miss — build prompt and call
    try:
        if kind == "quarterly":
            prompt = _build_quarterly_prompt(d, audience=audience)
        else:
            prompt = _build_monthly_prompt(d, audience=audience)
    except Exception as e:
        logger.warning(f"narrative prompt build failed for {kind}/{audience}: {e}")
        return d

    text = _call_claude(prompt)
    if not text:
        return d

    generated_at = _dt.datetime.utcnow().isoformat() + "Z"
    _CACHE[key] = {
        "text": text,
        "computed_at": now,
        "generated_at": generated_at,
    }
    d["narrative_summary"] = {
        "text": text,
        "model": _MODEL,
        "audience": audience,
        "generated_at": generated_at,
        "cache_age_seconds": 0,
    }
    return d


def cache_stats() -> dict:
    """For brain-radar visibility into the narrative cache."""
    now = time.monotonic()
    return {
        "anthropic_key_set": bool(_ANTHROPIC_KEY),
        "entries": len(_CACHE),
        "ttl_seconds": _CACHE_TTL,
        "model": _MODEL,
        "entries_detail": [
            {"key": k,
             "age_seconds": int(now - v["computed_at"]),
             "text_chars": len(v.get("text", "")),
             "generated_at": v.get("generated_at")}
            for k, v in _CACHE.items()
        ],
    }
