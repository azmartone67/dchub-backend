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
from routes.url_registry import build_public_url
from flask import Blueprint, jsonify, request, Response
from util import plan_tease as _plan_tease

# ★ Prices are READ from tier_registry, never restated. These three unlock
# blocks carried a literal pro_usd_month: 199 through TWO repricings
# (199 -> 299 at r-reprice 2026-06-19, 299 -> 99 at r-price-collapse
# 2026-09-05) and were quoting $199 live on 2026-09-08. A restated
# number is a second source of truth; this import is the fix for the
# class, not just for the value.
from tier_registry import price as _canon_price
try:
    from util.constraint_coverage_shape import annotate as _cc_annotate
except Exception:                                    # pragma: no cover
    _cc_annotate = None

logger = logging.getLogger(__name__)
site_selection_canvas_bp = Blueprint("site_selection_canvas", __name__)

def _derive_paid_upper(fallback):
    """UPPERCASED paid plan names from tier_registry, plus the ADMIN/INTERNAL
    gate words; fails CLOSED to `fallback`. The same rule as
    routes/deal_autopsy.py and routes/grid_transition_radar.py:
    _resolve_caller_tier returns the full plan vocabulary uppercased, and the
    literal this replaces teased paying STARTER, TEAM and RESEARCH_SEED
    customers."""
    try:
        from tier_registry import paid_plan_names
        got = {str(n).upper() for n in paid_plan_names()}
        return (got | {"ADMIN", "INTERNAL"}) if got else set(fallback)
    except Exception:
        return set(fallback)


# Tiers that unlock the synthesis (decision) layer. FREE/IDENTIFIED get the
# shortlist + a teaser only.
_PAID = _derive_paid_upper({"DEVELOPER", "PRO", "ENTERPRISE", "FOUNDING", "ADMIN"})

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


def _verdict_reasons(excess, constraint, ttp, verdict):
    """Fail-soft wrapper. An explanatory block must never be the thing that
    breaks a shortlist row — the same contract withProvenance and the citation
    stamper run under.
    """
    try:
        from routes.dcpi import verdict_reasons
        return verdict_reasons(excess, constraint, ttp, verdict)
    except Exception:
        return []


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
        "dcpi_url": build_public_url("dcpi", m.get('market_slug')),
        # r-verdict-reasons (2026-08-30, Gemini's spec): typed reasons, so code
        # can branch on `code` and an LLM can narrate `message`. Derived by
        # routes.dcpi.verdict_reasons from the SAME band table derive_verdict
        # walks — never a second copy of the thresholds. The row already carries
        # the components; this says which one decided the verdict, which is the
        # part a reader cannot compute without the bands.
        "verdict_reasons": _verdict_reasons(
            m.get("excess_power_score"), m.get("constraint_score"),
            m.get("time_to_power_months"), m.get("verdict")),
    }


def _empty_result(markets, region, max_months, verdicts, limit):
    """The `empty_result` block for a shortlist that matched nothing.

    PURE and importable ON PURPOSE. The first version lived inline in the view,
    so the only way to exercise it was a live HTTP call — the behaviour three
    external agents asked for could not be guarded by a test that runs in CI.
    A guard that cannot reach the code it guards is not a guard.

    ── WHY THE ROWS RIDE THE FIRST RESPONSE (r-excluded-rows, 2026-08-29) ──
    #2582 shipped the explanation, and three external agents ran it. All three
    came back with the same remaining gap, in their own words:

      Grok       "I can now tell a human 'Ohio has 9 markets, all AVOID'. I still
                  cannot show scores without a second call. Bar not cleared."
                  Asked for the rows — "or at least the top one with
                  composite/excess/constraint" — on the DEFAULT call.
      Gemini     asked for verdict counts carrying the ZEROES, so
                  {"BUILD":0,"CAUTION":0,"AVOID":9} reads as a location risk
                  rather than a data gap.
      Perplexity asked for next_best_action on the empty path specifically —
                  "not just after a successful answer but especially after an
                  empty result".

    A hint costs a round trip an agent may not spend, and a stateless caller that
    has already composed its final answer never spends it.

    The rows deliberately do NOT go in `shortlist`: that field means "markets that
    met your bar", and widening it would make the filter a lie. They ride
    `excluded_top` with the SAME row shape, so a caller reuses its shortlist
    parser unchanged. Tier honesty is preserved by construction — the rows go
    through _row_public like any other and `excluded_total` names the full count
    however the array is later trimmed.

    Returns None when `markets` yields a non-empty ranked set (nothing to explain).
    """
    in_region = _rank(markets, region, max_months, None)
    by_verdict = {}
    for m in in_region:
        v = (m.get("verdict") or "unscored").upper()
        by_verdict[v] = by_verdict.get(v, 0) + 1
    asked = sorted(verdicts) if verdicts else "ALL"

    excluded_top = [_row_public(m) for m in in_region[:limit]]

    # Every verdict DC Hub can assign, zeros included. A bare {"AVOID": 9} still
    # reads as "9 of something"; {"BUILD":0,"CAUTION":0,"AVOID":9} reads as the
    # finding — nothing here clears the bar, and it is not close.
    verdict_counts = {v: by_verdict.get(v, 0) for v in ("BUILD", "CAUTION", "AVOID")}
    for v, n in by_verdict.items():          # keep unscored / LOW_SIGNAL visible
        verdict_counts.setdefault(v, n)

    return {
        "reason": "no_market_met_the_verdict_filter" if in_region
                  else "no_tracked_market_in_region",
        "markets_in_region": len(in_region),
        "verdicts_present": by_verdict,
        "verdict_counts": verdict_counts,
        "verdicts_requested": asked,
        "meaning": (
            f"{len(in_region)} tracked market(s) match this geography, but none "
            f"carry a {asked} verdict. That is a real answer — the markets exist "
            f"and DC Hub scores them below your bar — not missing data."
            if in_region else
            "No tracked market matches this geography at all. This is a coverage "
            "gap, not a scoring result."
        ),
        "excluded_top": excluded_top,
        "excluded_total": len(in_region),
        "excluded_note": (
            "The markets that WERE found, ranked, with their scores — same row "
            "shape as `shortlist`. They are here rather than in `shortlist` "
            "because none met your verdict filter; `shortlist` only ever means "
            "markets that cleared your bar. Answer from these without a second "
            "call."
        ) if in_region else None,
        "next_best_action": ({
            "action": "answer_from_excluded_top",
            "reason": (
                f"The rows are already in this response. Report that DC Hub scores "
                f"{len(in_region)} market(s) in this geography and rates none of "
                f"them {asked} — that is the decision-grade answer, and it says do "
                f"not build here, and why."
            ),
            "then": ("re-run with verdict=ALL only if you need more than the top "
                     f"{len(excluded_top)} row(s)"),
        } if in_region else {
            "action": "widen_geography",
            "reason": "No tracked market matches this geography at all, so no "
                      "verdict filter will produce one. Try a broader region (a US "
                      "state code, an ISO, or us) or check the spelling.",
        }),
        "to_see_them": "re-run with verdict=ALL to get the rows and their scores",
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


def _row_tease(m):
    """A shortlist row for a keyless or free caller: the same keys as
    _row_public, so a parser needs no second branch, with every score null.
    The verdict stays: DCPI verdicts are free. verdict_reasons goes, because
    each reason carries the score it compares against the band."""
    row = _row_public_ids(m)
    row.update({k: None for k in _TEASE_LOCKED_ROW_FIELDS})
    return row


def _row_public_ids(m):
    return {
        "market": m.get("market_name") or m.get("market_slug"),
        "slug": m.get("market_slug"),
        "state": m.get("state"),
        "iso": m.get("iso"),
        "verdict": m.get("verdict"),
        "dcpi_url": build_public_url("dcpi", m.get('market_slug')),
    }


# Every field of a shortlist row that the tease withholds.
_TEASE_LOCKED_ROW_FIELDS = ("excess_power_score", "constraint_score",
                            "time_to_power_months", "composite_score",
                            "verdict_reasons")


def _locked_synthesis():
    """The decision layer, as a keyless or free caller sees it.

    ★ 2026-09-21. The page used to say "The verdict for <market> is one click
    away" and put "The BUILD / CAUTION / AVOID verdict" first in the list of
    what paying unlocks. Every shortlist row already carries its verdict, free.
    What a plan opens is the numbers behind it and the decision layer.
    """
    return {
        "locked": True,
        "message": ("The verdicts above are free. The scores behind them (excess "
                    "power, constraint, time to power, composite) and the decision "
                    "layer (the pick, the why, the build sequence and the risk "
                    "flags) come with a plan or a credit pack: see upgrade_options."),
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
            "developer_usd_month": _canon_price("developer"),
            "pro_usd_month": _canon_price("pro"),
            "pitch": (f"Developer (${_canon_price('developer')}/mo) unlocks the full "
                      "Site Selection Canvas synthesis + the MCP decision tools."),
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

    # Three answers share one body. An internal caller (the MCP server,
    # which applies its own masks) gets exactly what it got before; a caller
    # the gate admits gets every score and the decision layer; everyone else
    # gets the tease: verdicts free, the numbers behind them withheld
    # (util/plan_tease.py).
    def _body(ranked, shortlist, out_empty, applied_max_months):
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
            # ── applied_filters (2026-08-29, Perplexity) ────────────────────────
            # `inputs` echoes what was PARSED. That is not what was APPLIED, and
            # conflating the two is exactly what made capacity_mw look honored for
            # months. This names only the filters that actually narrowed the set, so
            # a caller can tell "you ignored my filter" from "your filter matched
            # nothing" without reading constraint_coverage first.
            "applied_filters": {
                "region": region,
                "verdicts": sorted(verdicts) if verdicts else "ALL",
                "max_months": applied_max_months,
            },
            # ── capacity_mw is NOT a filter, and saying so is the whole point ──
            # Reported live 2026-08-10: capacity_mw=5, 200 and 2000 against
            # region=TX all returned matched=20 and a byte-identical shortlist.
            # _rank() takes (markets, region, max_months, verdicts) — capacity_mw
            # has never been passed to it. It is parsed, echoed in `inputs`, and
            # used only for PROSE inside the paid _synthesis narrative.
            #
            # Echoing a parameter back unchanged is an implicit claim that it was
            # honored. An agent passes 200 MW, sees "capacity_mw": 200 in inputs,
            # and reasonably concludes the shortlist is sized. It is not. That is a
            # silent wrong answer — the same class as the planner answering Texas
            # with Virginia, just quieter, and it is exactly what constraint
            # coverage exists to prevent.
            #
            # WHY THIS IS DECLARED RATHER THAN IMPLEMENTED: the market rows carry
            # excess_power_score, a 0-100 INDEX — not megawatts. There is no MW
            # quantity here to compare a target against. Filtering "can this market
            # land 200 MW" against a score would require inventing a score→MW
            # mapping, which is precisely the fabrication this codebase refuses to
            # make. Publishing the limit is the honest answer; a made-up filter is
            # not. If per-market deliverable MW is ever ingested, implement the
            # filter and delete this block.
            "constraint_coverage": {
                "capacity_mw": {
                    "applied": False,
                    "status": "unavailable",
                    "reason": (
                        "The shortlist is NOT sized to this target. Ranking uses "
                        "region, time-to-power, DCPI verdict and composite score. "
                        "Market rows carry excess_power_score (a 0-100 index), not "
                        "megawatts, so there is no MW quantity to filter against — "
                        "and DC Hub will not invent a score-to-MW mapping to "
                        "manufacture one."
                    ),
                    "what_it_does_affect": (
                        "Paid-tier synthesis prose only, where the figure is quoted "
                        "back to describe your load."
                    ),
                    "instead": (
                        "Read excess_power_score and time_to_power_months per row "
                        "and judge headroom yourself, or call get_grid_intelligence "
                        "for the finalist's ISO."
                    ),
                },
            },
            "universe": len(markets),
            "matched": len(ranked),
            **({"empty_result": out_empty} if out_empty else {}),
            "shortlist": shortlist,
            "citation": "DC Hub Site Selection Canvas — dchub.cloud/site-selection (CC BY 4.0)",
        }
        return out

    def _finish(out):
        # ★2026-08-25: `constraint_coverage` ships in FOUR incompatible shapes
        # across the fleet under one name — a list of caveat strings on the timeline
        # tool, three different object forms elsewhere. THIS one is the
        # argument_disposition form (`applied:false` per argument you sent), which
        # is the shape an agent most needs to notice, because it means an argument
        # the schema ACCEPTED was then not used. Stamp the shape so a consumer can
        # branch instead of sniffing types. Derived from the value, never declared.
        if _cc_annotate:
            _cc_annotate(out)
        return out

    def _answer(tier, is_paid):
        ranked = _rank(markets, region, max_months, verdicts)
        shortlist_full = ranked[:limit]
        shortlist = [_row_public(m) for m in shortlist_full]

        # ── An empty shortlist is an ANSWER. Say it. ────────────────────────────
        # Found by an external agent running the live planner, 2026-08-11:
        #   region=OH → matched 0, shortlist []
        #   region=GA → matched 0, shortlist []
        # Both are CORRECT. Ohio has 9 tracked markets and Georgia 8 — every one
        # of them AVOID, so none survive the default BUILD,CAUTION filter.
        #
        # But returning a bare [] throws the answer away. "Ohio has 9 tracked
        # markets, all AVOID, none meeting your BUILD/CAUTION bar" is a
        # decision-grade result — arguably a more useful one than a shortlist,
        # because it says do not build here and why. "[]" is indistinguishable
        # from "DC Hub has no data for Ohio" or "the tool broke", and an agent
        # that guesses between those three gets it wrong two times in three.
        #
        # Downstream this mattered more than it looks: the caller's next step
        # needs a market_slug, gets nothing, and reports skipped_unresolved with a
        # constraint_check FAIL — so a truthful "no market clears your bar" read
        # as a broken execution. The planner was right; the payload could not say
        # so.
        #
        # Costs one extra pass over the same in-memory list, only when empty.
        # An empty shortlist is an ANSWER. _empty_result carries the full reasoning
        # and the three agent reports that shaped it.
        out_empty = _empty_result(markets, region, max_months, verdicts, limit) if not ranked else None
        out = _body(ranked, shortlist, out_empty, max_months)
        out["tier"] = tier
        if is_paid:
            out["synthesis"] = _synthesis(shortlist_full, capacity_mw, max_months)
        else:
            out["synthesis"] = _teaser(shortlist_full)
        _finish(out)
        # `return jsonify(out)` on the name the body literal is bound to keeps the
        # response statically resolvable by scripts/api_response_contract.py.
        return jsonify(out), 200

    def _unchanged():
        # Resolve tier for the paywall (best-effort; defaults to FREE).
        try:
            from routes.tier_gate import _resolve_caller_tier
            tier, _ = _resolve_caller_tier()
        except Exception:
            tier = "FREE"
        return _answer(tier, (tier or "FREE").upper() in _PAID)

    def _entitled():
        # require_plan admitted this caller at Developer or above, or
        # serve_below_plan opened it for one pack credit (g.user_tier 'pack').
        from flask import g
        tier = getattr(g, "user_tier", None) or getattr(request, "user_plan", None) or "developer"
        return _answer(str(tier).upper(), True)

    def _tease():
        # The deadline filter is not applied: it narrows by the time-to-power
        # figures, which are part of the paid answer.
        n = min(limit, _plan_tease.TEASE_ROWS)
        ranked = _rank(markets, region, None, verdicts)
        shortlist = [_row_tease(m) for m in ranked[:n]]
        out_empty = _empty_result(markets, region, None, verdicts, n) if not ranked else None
        if out_empty:
            in_region = _rank(markets, region, None, None)
            out_empty["excluded_top"] = [_row_tease(m) for m in in_region[:n]]
            if out_empty.get("excluded_note"):
                out_empty["excluded_note"] = (
                    "The markets that WERE found, ranked, with their verdicts — "
                    "same row shape as `shortlist`. They are here rather than in "
                    "`shortlist` because none met your verdict filter. Their scores "
                    "come with a plan or a credit pack.")
            out_empty["to_see_them"] = "re-run with verdict=ALL to get the rows"
        out = _body(ranked, shortlist, out_empty, None)
        if max_months:
            out["constraint_coverage"]["max_months"] = {
                "applied": False,
                "status": "locked",
                "reason": ("This preview ranks without the deadline filter: it "
                           "narrows by time to power, which comes with a plan "
                           "or a credit pack."),
                "instead": "see upgrade_options",
            }
        out["tier"] = "FREE"
        out["synthesis"] = _locked_synthesis()
        return _finish(out), list(_TEASE_LOCKED_ROW_FIELDS) + ["synthesis"], len(ranked)

    return _plan_tease.gate_or_tease("developer", _entitled, _tease, serve_unchanged=_unchanged)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site Selection Canvas · DC Hub</title>
<meta name="description" content="Guided data-center site selection: input capacity, geography, and deadline — get a ranked shortlist of US markets with free BUILD/CAUTION/AVOID verdicts, powered by the DC Hub Power Index.">
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
 .lock{border-color:rgba(99,102,241,.4)}.lock-badge{display:inline-block;background:var(--ind);color:#fff;font-size:10px;font-weight:700;padding:2px 9px;border-radius:6px;letter-spacing:.08em;margin-bottom:8px}.rungs{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}.rung{flex:1 1 200px;display:block;background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.35);border-radius:10px;padding:12px 14px;color:var(--ink);text-decoration:none}.rung b{display:block;font-size:15px;color:#fff}.rung span{display:block;font-size:12px;color:var(--mut);margin-top:3px}.rung:hover{border-color:var(--ind)}.lk{color:#64748b}.cta-big{display:inline-block;margin-top:12px;background:var(--ind);color:#fff;text-decoration:none;padding:12px 22px;border-radius:9px;font-weight:800;font-size:15px;box-shadow:0 4px 14px rgba(99,102,241,.35)}.cta-sub{margin-top:7px;font-size:11px;color:#64748b}
 .cta{display:inline-block;margin-top:10px;background:var(--ind);color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:700}
 li{margin:.3em 0}.flag{color:var(--amb)}
 a{color:#a5b4fc}
</style></head><body><div class="wrap">
<span class="badge">DC Hub · Site Selection Canvas</span>
<h1>Where should this data center go?</h1>
<p class="sub">Set your capacity, geography, and deadline. The Canvas ranks US markets by the DC Hub Power Index (excess-power headroom, grid constraint, and time-to-power) and returns a shortlist with each market's BUILD / CAUTION / AVOID verdict — free. The scores behind the verdicts and the build plan are the paid decision layer.</p>
<form id="f" onsubmit="run(event)">
 <div><label>Capacity (MW)</label><input id="cap" type="number" placeholder="100" style="width:110px"></div>
 <div><label>Region (state or ISO)</label><input id="reg" placeholder="e.g. TX, ERCOT, VA" style="width:160px"></div>
 <div><label>Deadline (months)</label><input id="dl" type="number" placeholder="24" style="width:120px"></div>
 <div><label>Verdicts</label><select id="vd"><option value="BUILD,CAUTION">Build + Caution</option><option value="BUILD">Build only</option><option value="ALL">All</option></select></div>
 <button>Run Canvas →</button>
</form>
<div id="out"><p class="sub">Run the Canvas to see your shortlist.</p></div>
<script>
function esc(x){return String(x==null?'':x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
// The canvas resolves a caller the way every REST gate does: login cookie,
// signed-in session token, or API key. Send what this browser holds.
function authHeaders(){var h={};try{var t=localStorage.getItem('dchub_token');if(t)h['Authorization']='Bearer '+t;var k=localStorage.getItem('dc_api_key')||localStorage.getItem('dchub_api_key');if(k)h['X-API-Key']=k;}catch(e){}return h;}
function num(v,fmt){return v==null?'<span class="lk" title="Comes with a plan or a credit pack">🔒</span>':fmt(v);}
function rungs(d){var o=d.upgrade_options||[];if(!o.length)return '';var h='<div class="rungs">';o.forEach(function(r){if(!r||!r.plan)return;var u=(r.url&&r.url.indexOf('https://dchub.cloud/go/c/')===0)?r.url:'/go/p/'+(r.plan==='pack'?'metered':r.plan);h+='<a class="rung" href="'+esc(u)+'"><b>'+esc(r.price||r.plan)+'</b><span>'+esc(r.label||'')+'</span></a>';});return h+'</div>';}
async function run(e){if(e)e.preventDefault();
 var out=document.getElementById('out');out.innerHTML='<p class="sub">Scoring markets…</p>';
 var qs=new URLSearchParams();
 var cap=document.getElementById('cap').value;if(cap)qs.set('capacity_mw',cap);
 var reg=document.getElementById('reg').value;if(reg)qs.set('region',reg);
 var dl=document.getElementById('dl').value;if(dl)qs.set('max_months',dl);
 qs.set('verdict',document.getElementById('vd').value);qs.set('limit','12');
 try{
  var r=await fetch('/api/v1/site-selection/canvas?'+qs.toString(),{headers:authHeaders(),credentials:'same-origin'});var d=await r.json();
  if(!d.ok){out.innerHTML='<p class="sub">'+esc(d.error||d.message||'No data')+'</p>';return;}
  var gated=!!d._gated;
  var h='<p class="sub">'+esc(d.matched)+' of '+esc(d.universe)+' scored markets match'+(gated&&d.shortlist.length<d.matched?' — showing the top '+d.shortlist.length+' with their verdicts':'')+'.</p>';
  h+='<table><thead><tr><th>#</th><th>Market</th><th>ISO</th><th>Verdict</th><th>Excess</th><th>Constraint</th><th>Time-to-power</th></tr></thead><tbody>';
  d.shortlist.forEach(function(m,i){h+='<tr><td>'+(i+1)+'</td><td><a href="'+esc(m.dcpi_url)+'">'+esc(m.market)+'</a> <span style="color:#64748b">'+esc(m.state)+'</span></td><td>'+esc(m.iso)+'</td><td><span class="v '+esc(m.verdict)+'">'+esc(m.verdict)+'</span></td><td>'+num(m.excess_power_score,esc)+'</td><td>'+num(m.constraint_score,esc)+'</td><td>'+num(m.time_to_power_months,function(v){return '~'+esc(v)+'mo';})+'</td></tr>';});
  h+='</tbody></table>';
  var s=d.synthesis||{};
  if(gated||s.locked){
   // ★ 2026-09-21: the verdicts are in the table above, free. This card sells
   // what is actually withheld: the scores behind them and the decision layer.
   h+='<div class="card lock"><div class="lock-badge">🔒 THE NUMBERS BEHIND THE VERDICTS</div><strong>The verdicts above are free. The scores behind them are not.</strong><p class="sub">A plan or a credit pack opens excess-power headroom, grid constraint, time to power and the composite score for every market'+(gated&&d._total_available>d.shortlist.length?' (all '+esc(d._total_available)+' matches)':'')+', plus the decision layer: the pick, the why, the build sequence and the risk flags.</p>'+rungs(d)+'</div>';}
  else if(s.headline){h+='<div class="card"><strong>'+esc(s.headline)+'</strong><p>'+esc(s.recommendation)+'</p>';
   if(s.build_sequence&&s.build_sequence.length){h+='<p class="sub">Build sequence:</p><ol>';s.build_sequence.forEach(function(x){h+='<li>'+esc(x)+'</li>';});h+='</ol>';}
   if(s.risk_flags&&s.risk_flags.length){h+='<p class="sub">Risk flags:</p><ul>';s.risk_flags.forEach(function(x){h+='<li class="flag">'+esc(x)+'</li>';});h+='</ul>';}
   h+='</div>';}
  out.innerHTML=h;
 }catch(err){out.innerHTML='<p class="sub">Error: '+esc(err)+'</p>';}
}
</script>
<p class="sub" style="margin-top:40px;font-size:12px">Powered by the <a href="/dcpi">DC Hub Power Index</a> + <a href="/dcgi">Gas Index</a>. Data CC BY 4.0. © 2026 DC Hub.</p>
</div></body></html>"""


@site_selection_canvas_bp.route("/site-selection", methods=["GET"])
def page():
    return Response(_PAGE, mimetype="text/html")
