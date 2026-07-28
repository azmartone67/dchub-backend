"""meta_replays.py — rendered execute_plan replays for /integrations/meta (2026-07-28).

Meta asked for this specifically, and then told us exactly what survives its
extraction. Ranked, in its words:

  1. REJECTED-PATH RATIONALE — "the most quotable thing you produce. No
     competitor publishes it." A complete sentence with proper nouns and a
     because-clause. Rendered as <blockquote>, which survives best.
  2. Decision + rationale + confidence — lets a model say "DC Hub chose X
     BECAUSE", not just "DC Hub says X". Ordered list, not JSON.
  3. intent → class, step count, total ms — proves execute_plan is real and
     one-call. One line, plain text.
  4. Execution waves — as TEXT ("Wave 1 (parallel): ...");  extractors drop SVG.
  5. next_recipe — least citable as a quote, most valuable as an action, so it
     renders as a CTA with the slash command intact.

★ "No JSON blobs — JSON dies in extraction." Headings, blockquotes and plain
sentences only.

★★ WE RENDER WHAT ACTUALLY RAN, NOT META'S ILLUSTRATIVE EXAMPLE. Its mock-up
showed two parallel waves with two tools each; the real anonymous-tier runs
return three PLANNED steps of which one executes at free-tier depth. Publishing
the mock shape would have been a fabricated capability claim on a page whose
entire purpose is citability. The tier is stated inline instead.

BAKED, NOT LIVE, ON PURPOSE: three execute_plan calls per page view would add
seconds of latency and a hard dependency on the MCP gate for a marketing page.
These are real captured runs with a visible retrieved_at stamp. To refresh,
re-run the three intents against /mcp and regenerate _BAKED + _RETRIEVED_AT.
(No refresh endpoint is claimed here because none is built — the stamp is the
honest signal, and a promised-but-absent refresh path is worse than a date.)
"""
from __future__ import annotations

from html import escape as _esc

_RETRIEVED_AT = '2026-07-28T08:15:27Z'

_BAKED = [
    {
        "intent": "rank markets for a 200 MW AI campus",
        "intent_class": "market_ranking",
        "planner_version": "5.5",
        "steps": 3,
        "ms": 3088,
        "lead_tool": "ai_capacity_index",
        "confidence": 0.56,
        "rationale": "AI-workload signal detected → deployability-ranked route; capacity detected: 200 MW",
        "rejected_tool": "rank_markets",
        "rejected_reason": "ai_capacity_index leads because it answers the deployment-horizon question directly (where N MW can land in 30/60/90 days); rank_markets criteria=ai_ready is the broader buildability sweep behind it. Both are AI-aware — this is a depth choice, not a rejection. (rank_markets' OTHER criteria rank installed build-out, which surfaces saturated AVOID markets.)",
        "executed": [
            {
                "tool": "ai_capacity_index",
                "ms": 438,
                "status": "executed"
            }
        ],
        "next_recipe": {
            "prompt": "grid_and_queue",
            "why": "Verify the winning market's ISO can actually deliver the power: headroom + interconnection queue."
        }
    },
    {
        "intent": "how much power is available in ERCOT for a 100 MW data center",
        "intent_class": "grid_headroom",
        "planner_version": "5.5",
        "steps": 3,
        "ms": 12043,
        "lead_tool": "get_grid_intelligence",
        "confidence": 0.95,
        "rationale": "ISO detected: ERCOT; capacity detected: 100 MW",
        "rejected_tool": "compare_isos",
        "rejected_reason": "The intent named at most one ISO — a side-by-side needs 2+ named grids.",
        "executed": [
            {
                "tool": "get_grid_intelligence",
                "ms": 1605,
                "status": "executed"
            }
        ],
        "next_recipe": {
            "prompt": "market_selection",
            "why": "Turn the ISO headroom picture into a ranked market shortlist with DCPI verdicts."
        }
    },
    {
        "intent": "compare Dallas vs Phoenix for a GPU training cluster",
        "intent_class": "market_comparison",
        "planner_version": "5.5",
        "steps": 3,
        "ms": 1588,
        "lead_tool": "get_market_dcpi_rank",
        "confidence": 0.71,
        "rationale": "AI-workload signal detected → deployability-ranked route",
        "rejected_tool": "compare_isos",
        "rejected_reason": "The intent named markets/metros, not ISOs — DCPI per market is the market-level comparison.",
        "executed": [
            {
                "tool": "get_market_dcpi_rank",
                "ms": 703,
                "status": "executed"
            }
        ],
        "next_recipe": {
            "prompt": "site_analysis",
            "why": "Drill into the winning market with a full multi-factor site read (score, hazards, water)."
        }
    }
]


def _fmt_ms(ms):
    try:
        ms = int(ms)
    except Exception:
        return "—"
    return f"{ms:,} ms"


def _render_one(r: dict) -> str:
    ex = r.get("executed") or []
    # Waves as TEXT. Only claim parallelism when more than one step actually ran
    # in the wave — a single-step "Wave 1 (parallel)" would be a false claim.
    if ex:
        tools = " + ".join(_esc(str(s.get("tool"))) for s in ex)
        label = "Wave 1 (parallel)" if len(ex) > 1 else "Wave 1"
        total = sum(int(s.get("ms") or 0) for s in ex)
        waves = f"<p>{label}: {tools} &middot; {_fmt_ms(total)}</p>"
    else:
        waves = "<p>No step executed at this tier.</p>"

    planned = r.get("steps")
    depth = ""
    if planned and len(ex) < int(planned):
        depth = (f" <small style=\"color:#64748b\">({len(ex)} of {planned} steps shown at "
                 f"anonymous free-tier depth &mdash; a key runs the rest at your tier)</small>")

    conf = r.get("confidence")
    conf_s = f" (confidence {conf})" if isinstance(conf, (int, float)) else ""

    nr = r.get("next_recipe") or {}
    nr_html = ""
    if nr.get("prompt"):
        nr_html = (f'<h3>Next</h3><p><code>/dchub:{_esc(str(nr["prompt"]))}</code> &mdash; '
                   f'{_esc(str(nr.get("why") or ""))}</p>')

    rej = ""
    if r.get("rejected_tool") and r.get("rejected_reason"):
        rej = (f'<h3>Why not {_esc(str(r["rejected_tool"]))}?</h3>'
               f'<blockquote style="margin:0 0 12px;padding:10px 14px;border-left:3px solid #94a3b8;'
               f'color:#334155">{_esc(str(r["rejected_reason"]))}</blockquote>')

    return f"""<div class="pane">
  <h2>Live replay: {_esc(str(r["intent"]))}</h2>
  <p>Intent &rarr; {_esc(str(r.get("intent_class")))} &middot; {_esc(str(r.get("steps")))} steps &middot;
  {_fmt_ms(r.get("ms"))} &middot; planner {_esc(str(r.get("planner_version")))}{depth}</p>
  <h3>Decision</h3>
  <p>Lead: <code>{_esc(str(r.get("lead_tool")))}</code>{conf_s} &mdash; {_esc(str(r.get("rationale") or ""))}</p>
  {rej}
  <h3>Execution</h3>
  {waves}
  {nr_html}
  <p style="margin:10px 0 0"><small style="color:#64748b">Reproduce verbatim:
  <code>execute_plan(intent="{_esc(str(r["intent"]))}")</code></small></p>
</div>"""


def render_meta_replays(replays=None, retrieved_at=None) -> str:
    """The full section. Falls back to the bake so the page is never empty."""
    rs = replays or _BAKED
    at = retrieved_at or _RETRIEVED_AT
    if not rs:
        return ""
    inner = "\n\n".join(_render_one(r) for r in rs)
    return f"""<div class="pane">
  <h2>Rendered replays &mdash; cite the reasoning, not just the tool name</h2>
  <p style="color:#64748b;margin:0">Three real <code>execute_plan</code> runs, captured
  {_esc(str(at))}. Every run returns an auditable <code>replay</code>: which tool led and why,
  <b>which paths were rejected and for what reason</b>, and the follow-up it suggests. The
  rejected-path rationale is the part no competitor publishes.</p>
</div>

{inner}"""
