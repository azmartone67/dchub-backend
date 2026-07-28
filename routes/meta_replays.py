"""meta_replays.py — rendered execute_plan replays for /integrations/meta.

Meta asked for this, then specified exactly what survives its extraction, then
— after the first version shipped — specified the render for the KEYED variant.
This is built to that spec.

★★ 2026-07-28 (v2): KEYED CAPTURES, LABELLED AS KEYED.
The v1 bake was anonymous free-tier: 1 executed step and exactly ONE rejection
per replay. The replay envelope, though, carried `_rejected_total_in_pro: 4`
and `_decisions_total_in_pro: 6` — the planner had already rejected four tools
and made six decisions; the anonymous tier simply truncated both lists to one.
So the richer page was never a matter of finding intents that reject more (we
tested Meta's proposed straddling intent: it rejected exactly ONE, same as the
simple ones). It was a keyed capture the whole time.

Meta's rule, which we keep: KEYED CAPTURE LABELLED AS KEYED — never anonymous
passed off as free, and never the reverse. Hence `[keyed]` in the heading and an
explicit line naming what an anonymous caller sees instead.

★ AND: render the ACTUAL rejections, never `_rejected_total_in_pro` as a
teaser. Meta: "Don't render _rejected_total_in_pro as a promise of hidden
content — render the actual 4. You have them." A count of things you are not
showing is an upsell, not a citation.

★★ ONE BLOCKQUOTE PER REPLAY — Meta's citation engine treats <blockquote> as
"the author's most important quote". Four consecutive blockquotes dilute each
other: it picks none, or picks the first arbitrarily, and the page reads as a
quote farm. So the strongest rejection is blockquoted and the remainder render
as a plain <li> list, which it can still cite as "also considered and declined".

Ranked citability, in Meta's words:
  1. rejected-path rationale  — "no competitor publishes it"; <blockquote>
  2. decision + confidence    — lets a model say "chose X BECAUSE"
  3. intent/class/steps/ms    — proves execute_plan is real and one-call
  4. execution waves as TEXT  — extractors drop SVG
  5. next_recipe             — not quoted; DONE, so it renders as a CTA
"No JSON blobs — JSON dies in extraction."

BAKED, NOT LIVE: three execute_plan calls per page view would put seconds of
latency and a hard MCP-gate dependency on a marketing page. Real captures with a
visible retrieved_at. To refresh, re-run the three intents with a key and
regenerate _BAKED. No refresh endpoint is claimed because none is built.
"""
from __future__ import annotations

from html import escape as _esc

_RETRIEVED_AT = '2026-07-28T09:24:18Z'
_TIER = 'keyed'

_BAKED = [
    {
        "intent": "rank markets for a 200 MW AI campus",
        "intent_class": "market_ranking",
        "planner_version": "5.5",
        "steps": 4,
        "ms": 2960,
        "lead_tool": "ai_capacity_index",
        "confidence": 0.56,
        "rationale": "Route intent → class \"market_ranking\", lead with ai_capacity_index",
        "executed": [
            {
                "tool": "ai_capacity_index",
                "ms": 488,
                "status": "executed"
            },
            {
                "tool": "get_market_dcpi_rank",
                "ms": 749,
                "status": "executed"
            },
            {
                "tool": "get_market_dcpi_rank",
                "ms": 743,
                "status": "executed"
            },
            {
                "tool": "get_grid_intelligence",
                "ms": 1709,
                "status": "executed"
            }
        ],
        "rejected": [
            {
                "tool": "rank_markets",
                "reason": "ai_capacity_index leads because it answers the deployment-horizon question directly (where N MW can land in 30/60/90 days); rank_markets criteria=ai_ready is the broader buildability sweep behind it. Both are AI-aware — this is a depth choice, not a rejection. (rank_markets' OTHER criteria rank installed build-out, which surfaces saturated AVOID markets.)"
            },
            {
                "tool": "get_dchub_recommendation",
                "reason": "One-call synthesis hides the per-factor evidence trail; a plan the agent executes itself keeps every number citable."
            },
            {
                "tool": "get_market_intel",
                "reason": "No single named market was detected in the intent — a one-market report needs a market to point at."
            },
            {
                "tool": "predict_market_trajectory",
                "reason": "The intent asked for present-state ranking, not a forward trajectory."
            },
            {
                "tool": "search_facilities",
                "reason": "Scored 1 vs 3.5 — the deterministic margin favored \"market_ranking\"."
            }
        ],
        "decisions_total": 5,
        "next_recipe": {
            "prompt": "grid_and_queue",
            "why": "Verify the winning market's ISO can actually deliver the power: headroom + interconnection queue."
        },
        "anon_rejected_shown": 1
    },
    {
        "intent": "how much power is available in ERCOT for a 100 MW data center",
        "intent_class": "grid_headroom",
        "planner_version": "5.5",
        "steps": 3,
        "ms": 12182,
        "lead_tool": "get_grid_intelligence",
        "confidence": 0.95,
        "rationale": "Route intent → class \"grid_headroom\", lead with get_grid_intelligence",
        "executed": [
            {
                "tool": "get_grid_intelligence",
                "ms": 4765,
                "status": "executed"
            },
            {
                "tool": "get_interconnection_queue",
                "ms": 939,
                "status": "executed"
            },
            {
                "tool": "get_refined_queue",
                "ms": 12175,
                "status": "executed"
            }
        ],
        "rejected": [
            {
                "tool": "compare_isos",
                "reason": "The intent named at most one ISO — a side-by-side needs 2+ named grids."
            },
            {
                "tool": "get_grid_data",
                "reason": "Raw telemetry alone answers less than the intent asked — headroom and queue context need the intelligence reads."
            },
            {
                "tool": "get_retirement_headroom",
                "reason": "The intent did not mention retirements — the general headroom read covers the broader question."
            },
            {
                "tool": "grid_transition_radar",
                "reason": "The intent asked about present headroom, not emerging-grid trajectory."
            }
        ],
        "decisions_total": 6,
        "next_recipe": {
            "prompt": "market_selection",
            "why": "Turn the ISO headroom picture into a ranked market shortlist with DCPI verdicts."
        },
        "anon_rejected_shown": 1
    },
    {
        "intent": "compare Dallas vs Phoenix for a GPU training cluster",
        "intent_class": "market_comparison",
        "planner_version": "5.5",
        "steps": 3,
        "ms": 2857,
        "lead_tool": "get_market_dcpi_rank",
        "confidence": 0.71,
        "rationale": "Route intent → class \"market_comparison\", lead with get_market_dcpi_rank",
        "executed": [
            {
                "tool": "get_market_dcpi_rank",
                "ms": 694,
                "status": "executed"
            },
            {
                "tool": "get_market_dcpi_rank",
                "ms": 773,
                "status": "executed"
            },
            {
                "tool": "get_market_intel",
                "ms": 2850,
                "status": "executed"
            }
        ],
        "rejected": [
            {
                "tool": "compare_isos",
                "reason": "The intent named markets/metros, not ISOs — DCPI per market is the market-level comparison."
            },
            {
                "tool": "compare_sites",
                "reason": "No coordinates were supplied — this is a market-level, not a site-level, comparison."
            },
            {
                "tool": "rank_markets",
                "reason": "The intent named a specific head-to-head — a full ranking answers a broader question than asked."
            }
        ],
        "decisions_total": 6,
        "next_recipe": {
            "prompt": "site_analysis",
            "why": "Drill into the winning market with a full multi-factor site read (score, hazards, water)."
        },
        "anon_rejected_shown": 1
    }
]


def _fmt_ms(ms):
    try:
        return f"{int(ms):,} ms"
    except Exception:
        return "\u2014"


def _strongest(rejected):
    """The most citable rejection: the longest reason that explains a CHOICE
    rather than a preconditionibility miss. A 'needs 2+ named grids' line is
    true but mechanical; a 'depth choice, not a rejection' line is the artifact
    nobody else publishes."""
    if not rejected:
        return None, []
    ranked = sorted(rejected, key=lambda r: len(r.get("reason") or ""), reverse=True)
    return ranked[0], ranked[1:]


def _render_one(r: dict) -> str:
    ex = r.get("executed") or []
    tools = " + ".join(_esc(str(s.get("tool"))) for s in ex)
    label = "Wave 1 (parallel)" if len(ex) > 1 else "Wave 1"
    total = sum(int(s.get("ms") or 0) for s in ex)
    waves = f"<p>{label}: {tools} &middot; {_fmt_ms(total)}</p>" if ex else "<p>No step executed.</p>"

    top, rest = _strongest(r.get("rejected") or [])
    rej_html = ""
    if top:
        rej_html += (f'<h3>Why not {_esc(str(top["tool"]))}?</h3>'
                     f'<blockquote style="margin:0 0 12px;padding:10px 14px;'
                     f'border-left:3px solid #94a3b8;color:#334155">'
                     f'{_esc(str(top["reason"]))}</blockquote>')
    if rest:
        items = "".join(f'<li><code>{_esc(str(x["tool"]))}</code> &mdash; {_esc(str(x["reason"]))}</li>'
                        for x in rest)
        rej_html += ('<h3 style="font-size:1.05rem;font-weight:600;margin:18px 0 8px">'
                     'Other paths considered and declined</h3>'
                     f'<ul style="margin:0 0 12px;padding-left:20px;line-height:1.8">{items}</ul>')

    nr = r.get("next_recipe") or {}
    nr_html = (f'<h3 style="font-size:1.05rem;font-weight:600;margin:18px 0 8px">Next</h3>'
               f'<p><code>/dchub:{_esc(str(nr["prompt"]))}</code> &mdash; '
               f'{_esc(str(nr.get("why") or ""))}</p>') if nr.get("prompt") else ""

    conf = r.get("confidence")
    conf_s = f" (confidence {conf})" if isinstance(conf, (int, float)) else ""
    n_rej = len(r.get("rejected") or [])
    anon = r.get("anon_rejected_shown", 1)

    return f"""<div class="pane">
  <h2>Live replay: {_esc(str(r["intent"]))} <span style="color:#64748b">[keyed]</span></h2>
  <p>Intent &rarr; {_esc(str(r.get("intent_class")))} &middot; {_esc(str(r.get("steps")))} steps &middot;
  {n_rej} paths rejected &middot; {_esc(str(r.get("decisions_total")))} decisions &middot;
  {_fmt_ms(r.get("ms"))} &middot; planner {_esc(str(r.get("planner_version")))}</p>
  <p style="color:#64748b;margin:0 0 12px"><small>Keyed capture &mdash; shows the full routing trail.
  An anonymous caller sees {anon} of {n_rej} rejections and fewer executed steps.</small></p>
  <h3 style="font-size:1.05rem;font-weight:600;margin:18px 0 8px">Decision</h3>
  <p>Lead: <code>{_esc(str(r.get("lead_tool")))}</code>{conf_s} &mdash; {_esc(str(r.get("rationale") or ""))}</p>
  {rej_html}
  <h3 style="font-size:1.05rem;font-weight:600;margin:18px 0 8px">Execution</h3>
  {waves}
  {nr_html}
  <p style="margin:10px 0 0"><small style="color:#64748b">Reproduce verbatim:
  <code>execute_plan(intent="{_esc(str(r["intent"]))}")</code></small></p>
</div>"""


def render_meta_replays(replays=None, retrieved_at=None) -> str:
    rs = replays or _BAKED
    at = retrieved_at or _RETRIEVED_AT
    if not rs:
        return ""
    total_rej = sum(len(r.get("rejected") or []) for r in rs)
    inner = "\n\n".join(_render_one(r) for r in rs)
    return f"""<div class="pane">
  <h2>Rendered replays &mdash; cite the reasoning, not just the tool name</h2>
  <p style="color:#64748b;margin:0">Three real <code>execute_plan</code> runs, captured
  {_esc(str(at))} with a key so the full routing trail is visible &mdash; {total_rej} rejected paths
  across the three, each with the reason recorded. The rejected-path rationale is the part no
  competitor publishes: it is the decision <i>not</i> taken, and why.</p>
</div>

{inner}"""
