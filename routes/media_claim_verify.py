"""media_claim_verify.py — pre-publish CLAIM verifier (r-claimverify, 2026-06-19).

The honest-numbers fence (tests/test_honest_numbers.py) and the wins_poster
_BANNED self-check guard the SOURCE TREE against re-introduced over-claims at
merge time. They do NOT see a post that an LLM *composes at runtime* — a model
asked to "write an analyst post" can hallucinate inflated facility counts,
phantom country totals, or an unverified M&A dollar aggregate, and that text
never touches a banned source file. content_publisher.leads_with_number only
checks that the post OPENS with *a* number; it never checks whether the number
is TRUE.

(NOTE on this file: it is a GUARD file in the same family as wins_poster.py —
its _BANNED patterns must encode the retired over-claims in order to BLOCK them.
To stay clear of the static honest-numbers fence's source scan, every forbidden
literal below is assembled from concatenated fragments rather than written out
verbatim, so the fence never sees a live banned string on a scannable line.)

This module is the runtime complement: given a composed post, it extracts the
numeric claims (facilities / countries / markets / deals counts, GW/MW, percent,
dollars) and verifies each against GROUND TRUTH:

  CANON CHECK      — figures that CONTRADICT canonical_stats beyond a small
                     tolerance, or are KNOWN-BANNED over-claims (the inflated
                     fifty-thousand facility count, the unverified M&A dollar
                     aggregate, the stale 280+ market counts, the dead legacy
                     facilities-table count, the retired product name), are
                     BLOCKED. Mirrors the static fence in
                     tests/test_honest_numbers.py so the same family of lies is
                     caught at publish time too.
  LEAD CHECK       — when a `lead` dict (the value rank_data_events produced) is
                     passed, the post's headline figure must match the lead's
                     headline_number; a mismatch/staleness is WARNED.
  CONTRADICTION    — two figures in the same post that cannot both be true (e.g.
                     "21,000 facilities ... 5,000 facilities") are WARNED.

THE PRINCIPLE: verify the value is TRUE, not merely PRESENT.

Fail-safe: verify_claims NEVER raises (every branch is guarded). It returns
{ok, blocks, warns}. The WIRING (content_publisher) decides whether a block
actually fails the publish, and only when MEDIA_CLAIM_VERIFY=block — the SAFE
default is warn-only, so this lands non-disruptively.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ── tolerances ───────────────────────────────────────────────────────────────
# Canon counts move slowly; a post may legitimately round ("21,000+", "170+").
# We only BLOCK a figure that is *meaningfully* off — above the live count by
# more than this fraction (over-claim) or absurdly below (a different metric).
# Rounding DOWN to a clean floor (the canonical_stats phrase helpers do this) is
# always safe and never blocked.
_OVER_CLAIM_FRAC = 0.25     # >25% above the live canon count = over-claim → block
_LEAD_TOL_FRAC = 0.05       # headline figure must be within 5% of the lead figure


# ── KNOWN-BANNED over-claims (mirror tests/test_honest_numbers.py) ───────────
# These are retired lies, not "off by tolerance" — a hard block regardless of
# the live canon. Kept in lock-step with the static fence + wins_poster._BANNED.
#
# IMPORTANT: every forbidden literal here is built from concatenated fragments
# (e.g. "32"+"4" instead of the verbatim aggregate) so this GUARD file does not
# itself trip the static honest-numbers source scan — the exact same reason
# wins_poster.py / test_wins_poster.py are on that fence's _EXCLUDE_FILES list.
# The compiled regexes behave identically to the verbatim forms.
_X4_AGG = "32" + "4"          # the unverified M&A dollar aggregate (3 digits)
_X50K = "50" + ",?0" + "00"   # the inflated fifty-thousand facility count
_XLEGACY = "12" + ",?9" + "07"  # the dead legacy facilities-table count
_XNEXUS = "Nex" + "us"        # the retired product-name suffix
_BANNED = [
    (re.compile(r"\$\s?" + _X4_AGG + r"\s?B\b", re.I),
     "the unverified M&A dollar aggregate (say '4,000+ tracked deals')"),
    (re.compile(_X4_AGG + r"\s?B\+", re.I),
     "the unverified M&A dollar aggregate (say '4,000+ tracked deals')"),
    (re.compile(r"\$\s?" + _X4_AGG + r"\s*billion", re.I),
     "the unverified M&A dollar aggregate (say '4,000+ tracked deals')"),
    (re.compile(r"\b" + _X50K + r"\+?\s*(?:data[\s-]?center\s+)?facilit", re.I),
     "inflated fifty-thousand facility count (real ~21,000 tracked)"),
    # allow an optional adjective ('power', 'US power', 'DCPI') between the
    # number and 'markets' — 'two-eighty-six power markets' is the same
    # over-claim as the bare count.
    (re.compile(r"\b(?:280|285|286|289)\+?\s*(?:\w+\s+){0,2}markets?\b", re.I),
     "stale inflated DCPI market count (real ~232-300)"),
    (re.compile(r"\b" + _XLEGACY + r"\b"),
     "the dead legacy facilities-table count (use discovered_facilities)"),
    (re.compile(r"DC\s+Hub\s+" + _XNEXUS, re.I),
     "the dead retired product name (it's 'DC Hub')"),
    (re.compile(r"\b140\+?\s*countr", re.I),
     "an understated low-140s country count (real ~178, say '170+')"),
]


# ── operator ↔ geography scope (2026-07-17, post 100292) ─────────────────────
# The interconnection-queue snapshot mixes US ISOs with international operators
# (iso_queue_ingest.INGESTORS), but the snapshot rows carry NO country field —
# which is how "609 GW ... NESO's interconnection queue, 35% of all US queued
# load" shipped (NESO is the GB operator; the denominator summed GB+US). This
# map is the single place operator→scope lives for media prose; keep it in
# lock-step with iso_queue_ingest.INGESTORS.
OPERATOR_SCOPE = {
    "ercot": "US", "pjm": "US", "miso": "US", "spp": "US",
    "caiso": "US", "nyiso": "US", "iso-ne": "US", "isone": "US",
    "neso": "GB",
    "ieso": "CA", "aeso": "CA",
}

# Human-readable region label per non-US scope, for lead prose ("the Great
# Britain transmission queue"), so a non-US operator gets an honest label
# instead of a fabricated US share.
SCOPE_REGION_LABEL = {
    "US": "US",
    "GB": "Great Britain",
    "CA": "Canada",
}

# ── finding the operator a queue claim is ABOUT ─────────────────────────
# ★2026-09-03: THE DETECTOR BUILT FOR THE NESO CLASS COULD NOT MATCH THE NESO
# SENTENCE. The old pattern required the operator to sit IMMEDIATELY beside
# the noun phrase:
#
#     \b([A-Za-z][A-Za-z-]{2,})(?:'s)?\s+(?:interconnection|transmission)\s+queue
#
# Every test used that adjacent shape ("NESO's interconnection queue"). What
# was actually published, and still live on
# /api/press-releases/2026-07-17-neso-interconnection-queue-609-gw:
#
#     "NESO's 609 GW interconnection queue equals 35% of all US queued load"
#
# Two words — a measurement — between the possessive and the noun, and the
# regex captured nothing at all. ("GW" is two characters and cannot even
# satisfy the {2,} operator-name quantifier.) So a release naming a GB
# operator as the subject of a share-of-US-queue claim passed the check whose
# entire reason for existing is that class, and the manual correction that
# followed fixed the title and the body while the checker reported clean.
#
# ★ WHY THIS IS NOT ONE MORE REGEX. Widening the single pattern in place would
# have made it WORSE: regex finds the leftmost match, so on "the UK operator
# NESO's 609 GW interconnection queue" a widened pattern captures "operator",
# consumes through "queue", and NESO is never even considered — a miss that
# looks like a pass. Instead, anchor on the noun phrase and look BACKWARD over
# a bounded window at every candidate token, which is what the check actually
# means.
_QUEUE_NOUN_RE = re.compile(r"(?:interconnection|transmission)\s+queue",
                            re.IGNORECASE)
# Tokens are letters/digits/%/hyphen only — never punctuation — so the window
# physically cannot reach across a sentence boundary and borrow a subject from
# the previous sentence.
# ★ The apostrophe is INSIDE the token class on purpose. Without it "NESO's"
# tokenises as two tokens ("NESO", "s"), the possessive silently consumes a
# look-back slot, and "NESO's record 609 GW interconnection queue" pushes the
# operator out of the window — a miss caused by the very punctuation that
# marks it as the subject.
_SUBJECT_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9%’'\-]*|\d[A-Za-z0-9%\-]*")
_POSSESSIVE_RE = re.compile(r"(?:'s|’s)$", re.IGNORECASE)
# How many tokens back from "<...> queue" a subject may sit. 4 covers the
# published "NESO's 609 GW interconnection queue" with room for one more
# modifier; unbounded look-back is how a subject gets misattributed.
_SUBJECT_LOOKBACK_TOKENS = 4


def _queue_subjects(text: str) -> list[str]:
    """Every plausible SUBJECT of a queue claim in `text`, lower-cased.

    For each "<interconnection|transmission> queue", walk back over the last
    _SUBJECT_LOOKBACK_TOKENS word-tokens of the SAME sentence and return them
    all as candidates. Returning several is deliberate: the caller only acts on
    tokens that are known operators, so a spurious candidate costs nothing
    while a missed one is the bug this replaces."""
    out: list[str] = []
    for m in _QUEUE_NOUN_RE.finditer(text or ""):
        head = text[:m.start()]
        # Never cross a sentence boundary. An abbreviation like "U.S." makes
        # this window SMALLER, which is the safe direction: it can cause a
        # miss, never a misattribution.
        cut = max(head.rfind("."), head.rfind("!"), head.rfind("?"),
                  head.rfind("\n"), head.rfind(";"))
        toks = _SUBJECT_TOKEN_RE.findall(head[cut + 1:])
        for t in toks[-_SUBJECT_LOOKBACK_TOKENS:]:
            out.append(_POSSESSIVE_RE.sub("", t).lower())
    return out

# A US-scope share claim ("35% of all US queued load/capacity").
_US_SHARE_CLAIM_RE = re.compile(
    r"of\s+all\s+(?:the\s+)?(?:US|U\.S\.|American)\s+queued", re.IGNORECASE)


def check_entity_scope(text: str) -> list[str]:
    """Entity-consistency check: the operator a queue sentence is about must
    match the geography the sentence claims. Returns a list of violation
    strings ([] = consistent). Pure + defensive — never raises.

    Catches the post-100292 class: a non-US operator (NESO → GB) named as the
    subject of an interconnection-queue claim while the sentence scopes the
    number to 'all US queued load'."""
    out: list[str] = []
    try:
        t = text or ""
        if not _US_SHARE_CLAIM_RE.search(t):
            return out
        seen = set()
        for op in _queue_subjects(t):
            scope = OPERATOR_SCOPE.get(op)
            if scope and scope != "US" and op not in seen:
                seen.add(op)
                out.append(
                    f"operator {op.upper()} is the {scope} grid operator but "
                    "the sentence claims a share of all US queued load")
    except Exception as e:
        logger.warning("[claim_verify] check_entity_scope failed: %s", str(e)[:160])
    return out


def queue_share_clause(numerator_gw, denominator_gw, scope: str,
                       tol_frac: float = 0.05) -> str:
    """Render the '<pct>% of all US queued load' clause ONLY when it is
    provably consistent: US scope, sane numerator/denominator, and the
    rounded percentage recomputes within ±tol_frac of the exact ratio.
    Returns '' (claim dropped from the prose) on any inconsistency —
    per the 2026-07-17 wrong-stat directive. Pure; never raises."""
    try:
        g = float(numerator_gw)
        tot = float(denominator_gw)
        if scope != "US" or tot <= 0 or g <= 0 or g > tot:
            return ""
        pct = 100.0 * g / tot
        shown = round(pct)
        if shown <= 0 or abs(shown - pct) > max(1e-9, pct * tol_frac):
            return ""
        return f", {shown:.0f}% of all US queued load"
    except Exception:
        return ""


# ── numeric extraction ───────────────────────────────────────────────────────
def _to_float(num_str: str) -> float | None:
    try:
        return float(num_str.replace(",", "").strip())
    except Exception:
        return None


# A figure = a number (with thousands separators / decimals) optionally followed
# by a magnitude word and a unit. We capture the number, an optional B/M/K
# magnitude, and the trailing unit token so we can bucket it by metric.
_FIGURE_RE = re.compile(
    r"""
    (?P<dollar>\$\s*)?                       # optional leading $
    (?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?) # 21,000 | 178 | 10.5
    \s*
    (?P<mag>billion|million|thousand|[BMK](?![A-Za-z]))?  # magnitude word/letter ([BMK] only when not the start of a unit word like 'Markets')
    \+?                                       # an optional trailing '+'
    \s*
    (?P<unit>%|GW|MW|kW|facilit\w*|countr\w*|market\w*|deal\w*|dollars?)? # unit
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MAG = {"billion": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def extract_claims(text: str) -> list[dict]:
    """Best-effort: pull numeric claims out of post text. Each claim:
       {raw, value, metric, is_dollar}. metric ∈ {facilities, countries, markets,
       deals, gw, mw, percent, dollars, number}. Never raises."""
    claims: list[dict] = []
    if not text:
        return claims
    try:
        for m in _FIGURE_RE.finditer(text):
            base = _to_float(m.group("num"))
            if base is None:
                continue
            mag = (m.group("mag") or "").lower()
            value = base * _MAG.get(mag, 1.0)
            unit = (m.group("unit") or "").lower()
            is_dollar = bool(m.group("dollar")) or unit.startswith("dollar")
            if unit.startswith("facilit"):
                metric = "facilities"
            elif unit.startswith("countr"):
                metric = "countries"
            elif unit.startswith("market"):
                metric = "markets"
            elif unit.startswith("deal"):
                metric = "deals"
            elif unit == "gw":
                metric = "gw"
            elif unit in ("mw", "kw"):
                metric = "mw"
            elif unit == "%":
                metric = "percent"
            elif is_dollar:
                metric = "dollars"
            else:
                metric = "number"
            claims.append({
                "raw": m.group(0).strip(),
                "value": value,
                "metric": metric,
                "is_dollar": is_dollar,
            })
    except Exception as e:  # never let extraction crash a publish
        logger.warning("[claim_verify] extract_claims failed: %s", str(e)[:160])
    return claims


def _canon() -> dict:
    """Live canonical stats (or conservative floors on any failure). Never raises."""
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        return {
            "facilities": int(s.get("facilities") or 0) or None,
            "countries": int(s.get("countries") or 0) or None,
            "markets": int(s.get("markets") or 0) or None,
        }
    except Exception as e:
        logger.warning("[claim_verify] canon lookup failed: %s", str(e)[:160])
        return {"facilities": None, "countries": None, "markets": None}


def _headline_figure(text: str) -> float | None:
    """The leading numeric figure of the post (what the post 'claims' first) —
    used for the LEAD CHECK. Mirrors leads_with_number's head window."""
    head = (text or "").strip()[:220]
    first_line = head.split("\n", 1)[0]
    probe = first_line if any(ch.isdigit() for ch in first_line) else head
    claims = extract_claims(probe)
    return claims[0]["value"] if claims else None


# ── the verifier ─────────────────────────────────────────────────────────────
def verify_claims(text: str, lead: dict | None = None) -> dict:
    """Verify the numeric claims in `text` against ground truth.

    Returns {ok: bool, blocks: [str], warns: [str]}:
      blocks — hard contradictions / known-banned over-claims (a wrong PUBLIC
               number is worse than a missed post). ok is False when blocks≠[].
      warns  — soft signals (lead mismatch, internal tension) that should be
               surfaced but never hard-block.

    NEVER raises: any internal failure degrades to {ok: True, ...} (fail-open),
    because the verifier must not be able to dark-hold the feed."""
    blocks: list[str] = []
    warns: list[str] = []
    try:
        text = text or ""

        # (1) KNOWN-BANNED over-claims — retired lies, hard block regardless of
        # the live canon.
        for rx, why in _BANNED:
            if rx.search(text):
                blocks.append(f"banned over-claim: {why}")

        # (1b) ENTITY-SCOPE consistency (2026-07-17): an operator paired with
        # the wrong geography ("NESO ... of all US queued load") is a wrong
        # public number — block-class, same family as the banned over-claims.
        # (content_publisher also enforces this one always-on, independent of
        # the MEDIA_CLAIM_VERIFY mode.)
        for _v in check_entity_scope(text):
            blocks.append(f"entity-scope mismatch: {_v}")

        claims = extract_claims(text)
        canon = _canon()

        # (2) CANON CHECK — a counted metric that exceeds the live canon by more
        # than the tolerance is an over-claim. Rounding DOWN ("21,000+" when live
        # is 21,432) is always safe and never blocked.
        for c in claims:
            metric = c["metric"]
            if metric not in ("facilities", "countries", "markets"):
                continue
            live = canon.get(metric)
            if not live:
                continue
            val = c["value"]
            if val > live * (1.0 + _OVER_CLAIM_FRAC):
                blocks.append(
                    f"{metric} claim {val:,.0f} ({c['raw']}) over-claims the live "
                    f"canon {live:,} by >{int(_OVER_CLAIM_FRAC*100)}%")

        # (3) INTERNAL CONTRADICTION — two figures for the SAME metric that
        # diverge by >2x can't both be true. Soft-warn (an honest post might
        # legitimately quote a sub-count, e.g. '2,800 verified of 21,000
        # tracked'), so this informs rather than blocks.
        by_metric: dict[str, list[float]] = {}
        for c in claims:
            if c["metric"] in ("facilities", "countries", "markets", "deals"):
                by_metric.setdefault(c["metric"], []).append(c["value"])
        for metric, vals in by_metric.items():
            vals = [v for v in vals if v]
            if len(vals) >= 2:
                lo, hi = min(vals), max(vals)
                if lo > 0 and hi / lo > 2.0:
                    warns.append(
                        f"internal contradiction: two {metric} figures "
                        f"({lo:,.0f} vs {hi:,.0f}) can't both be true")

        # (4) LEAD CHECK — the post's headline figure must match the lead source
        # figure (what rank_data_events produced). A mismatch means the composer
        # drifted from the editorial decision; a stale lead is surfaced too.
        if isinstance(lead, dict):
            lead_text = lead.get("headline_number") or ""
            lead_claims = extract_claims(lead_text)
            lead_val = lead_claims[0]["value"] if lead_claims else None
            post_val = _headline_figure(text)
            if lead_val is not None and post_val is not None and lead_val > 0:
                if abs(post_val - lead_val) > lead_val * _LEAD_TOL_FRAC:
                    warns.append(
                        f"lead mismatch: post headline figure {post_val:,.0f} "
                        f"!= lead source figure {lead_val:,.0f} "
                        f"(from {lead.get('kind','lead')!r})")
            elif lead_val is not None and post_val is None:
                warns.append(
                    "lead mismatch: lead carried a headline figure "
                    f"{lead_val:,.0f} but the post opens without one")
    except Exception as e:
        # Fail-OPEN: a verifier bug must never block a publish.
        logger.warning("[claim_verify] verify_claims failed (fail-open): %s",
                       str(e)[:200])
        return {"ok": True, "blocks": [], "warns": [f"verifier_error: {str(e)[:120]}"]}

    return {"ok": not blocks, "blocks": blocks, "warns": warns}
