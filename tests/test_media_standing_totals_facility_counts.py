"""tests/test_media_standing_totals_facility_counts.py — the LinkedIn desk's
standing totals quote BUILDINGS and the DEDUPED deal count, never a row pile and
never a frozen literal.

THE BUG, in copy that actually reached LinkedIn (read back off
/api/v1/linkedin-quad/status, which has exposed post_text since #3105):

  • 2026-08-20 h8   operator_spotlight
      "Live tracking across 26,334+ facilities, 320+ markets and 4,000+ deals"
  • 2026-08-19 h12  interconnection lead
      "Standing totals behind that query: 26,327+ facilities across 320+
       markets and 4,000+ tracked deals"
  • 2026-08-17 h12  deal lead
      "Standing totals: 26,136+ facilities across 320+ markets, 4,000+ tracked
       deals"

Two defects in one boilerplate, both in `_build_user_prompt`, both handed to the
model under "use these EXACT figures, do not invent others" — so the model
published them faithfully:

  (1) ROWS AS BUILDINGS. `_t_fac` read canonical_stats["facilities"] =
      COUNT(*) FROM discovered_facilities = raw source ROWS (26,388 live),
      ~1.4x the building count. The citeable figure is `facilities_verified` =
      COUNT(DISTINCT canonical_slug) WHERE COALESCE(is_duplicate,0)=0 AND
      canonical_slug IS NOT NULL (18,656 live) — and that is exactly the ceiling
      media_fact_check_guard.check_facility_count_claims measures published copy
      against. Same class as #3111's 16:00 capability slot; claim_breaker's
      rows_ne_buildings was armed 2026-08-21, AFTER all three posts above, so
      this was an armed production refusal waiting for the next rotation.

  (2) STALE DEALS CANON. "4,000+ tracked deals" was a hardcoded literal against
      a live 1,932 distinct — a >2x over-claim, and a value ai_surface_canon
      already carries in PINNED["stale_markers"]. Deal ROWS over-state ~2.9x
      (the AUTO deal id embeds the ingest date, so one deal accrues a row per
      day). The same literal sat in the served SEO footer, the Moltbook blurb,
      the welcome email — and in the honesty guard's own advice string, which
      told composers to "say '4,000+ tracked deals'".

WHY A FENCE AND NOT JUST THE EDIT: the claim-breaker reads the FINAL composed
text, so it catches this only at post time, in production, as a refusal — which
is how #3111 was found (twice, on consecutive days). This runs the REAL gate
over the REAL rendered prompt at commit time.

WHAT MAKES IT NON-VACUOUS, four ways:
  • ONE monkeypatch pins canonical_stats.get_canonical_stats, and BOTH the
    composer and the gate read through it. The shared-source property is
    exercised, not assumed.
  • The poisoned-key test moves the row pile to an absurd value and re-renders:
    any claim that moves with it is derived from rows, whatever today's reading
    happens to be. (The gate's 5% tolerance means magnitude alone can't decide.)
  • The census bans a TYPED count outright, not merely an over-claiming one, so
    re-hardcoding TODAY'S correct value is a finding too — the presence-vs-census
    blind spot, and the one that silently re-rots a surface after it is fixed.
  • The verbatim published copy must still be REFUSED. If that ever passes,
    every other assertion here is vacuous.

MUTATION-TESTED: 22 mutations, each verified as actually applied, __pycache__
cleared around every one (a same-length edit otherwise reuses stale bytecode and
reports the previous mutation's result). 22/22 red. The first pass was 19/20 —
mutation-testing, not the fence, found the twentieth: a stale literal parked in
`_canon_deals_phrase`'s `or` fallback stayed invisible because a healthy canon
never evaluates that branch. test_canon_helper_bodies_contain_no_digits_at_all
and the three fail-open tests close it.

CI-SAFETY: the census half is pure (ast over source text). The render half
imports routes.linkedin_content_engine + routes.media_fact_check_guard, both of
which are stdlib-only, and skips the Flask-dependent modules where Flask is
absent.

Run:  python3 -m pytest tests/test_media_standing_totals_facility_counts.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The live reading on 2026-08-23, the day this was found. facilities_records is
# what the three posts above published as facilities; facilities_verified is
# what they should have. Replaying the failing day is the point.
RECORDS, DISTINCT, DEALS_DISTINCT, DEAL_ROWS, MARKETS = 26388, 18656, 1932, 5121, 320

LIVE = {
    "facilities": RECORDS,              # COUNT(*) — raw source ROWS
    "facilities_verified": DISTINCT,    # COUNT(DISTINCT canonical_slug) — BUILDINGS
    "deals": DEALS_DISTINCT,            # deduped distinct deals
    "countries": 178,
    "countries_verified": 178,
    "markets": MARKETS,
    "isos": 7,
}

# Copy that shipped, verbatim. Two-sided controls: each must be REFUSED.
PUBLISHED_08_20 = ("Live tracking across 26,334+ facilities, 320+ markets and "
                   "4,000+ deals")
PUBLISHED_08_19 = ("Standing totals behind that query: 26,327+ facilities across "
                   "320+ markets and 4,000+ tracked deals")
PUBLISHED_08_17 = ("Standing totals: 26,136+ facilities across 320+ markets, "
                   "4,000+ tracked deals")

# Every module that composes or serves a DC Hub facility/deal count in copy.
# A path that stops existing fails loudly rather than silently dropping out of
# the census — coverage is the dominant failure mode for a fence like this.
COPY_MODULES = (
    os.path.join("routes", "linkedin_content_engine.py"),
    os.path.join("routes", "linkedin_quad_daily.py"),
    os.path.join("routes", "seo_pages.py"),
    os.path.join("routes", "onboarding_recover.py"),
    os.path.join("routes", "media_fact_check_guard.py"),
    os.path.join("routes", "agent_broadcast.py"),
    os.path.join("routes", "openapi_dynamic.py"),
    os.path.join("routes", "competitor_intel.py"),
)


# ── the two halves of the chain, imported for real ───────────────────────────

import canonical_stats  # noqa: E402  (path is set above)
from routes import media_fact_check_guard as _guard  # noqa: E402
from routes import linkedin_content_engine as _engine  # noqa: E402


@pytest.fixture
def pinned_canon(monkeypatch):
    """Pin the 2026-08-23 reading at canonical_stats — the ONE source the
    composer and the gate share. Patching here rather than at each end is
    deliberate: if a future edit gives either side its own connection, the
    render tests below stop being pinned and start failing, which is the
    correct alarm."""
    def _fake(force: bool = False) -> dict:
        return dict(LIVE)
    monkeypatch.setattr(canonical_stats, "get_canonical_stats", _fake)
    return dict(LIVE)


# ★2026-09-02 — A SECOND ERA, deliberately NOT folded into LIVE above.
# ai_surface_canon.PINNED['public'] was walked forward to the live resolver
# (facilities "18,500+" -> "20,100+", deals "1,900+" -> "2,000+") against a
# measured 20,198 distinct buildings and 2,069 distinct deals — the reading
# recorded on the canon lines themselves. LIVE stays frozen at 2026-08-23:
# replaying the failing day is the point of the composer half above, and both
# counts there are load-bearing (26,388 rows vs 18,656 buildings is the 1.4x
# the fence is about).
#
# The two served-HTML surfaces at the bottom of this file are the one place the
# eras collide. They bind ai_surface_canon at IMPORT time — onboarding_recover
# and seo_pages both call canon_text() at module scope — so no fixture can pin
# what they state: they always carry TODAY'S canon. Gating today's canon against
# the 2026-08-23 ceiling measures ten days of growth as an over-claim, which is
# era drift, not a defect in the copy.
#
# The ceiling is a MEASURED literal and is deliberately NOT read back off
# ai_surface_canon: a canon checked against a ceiling derived from itself is
# self-certifying, and "the canon publishes the row pile" is precisely what this
# fence exists to catch. It keeps its teeth at the new value — the 26,388-row
# pile is still ~1.3x over it, and all three published strings above are still
# refused. `facilities` (the raw pile) is carried over from the 08-23 reading
# unmeasured: the gate reads it only for claims explicitly qualified as source
# records, and neither of these surfaces makes one.
CANON_ERA_DISTINCT, CANON_ERA_DEALS = 20198, 2069
CANON_ERA = dict(LIVE, facilities_verified=CANON_ERA_DISTINCT,
                 deals=CANON_ERA_DEALS)


@pytest.fixture
def pinned_canon_today(monkeypatch):
    """Pin the 2026-09-02 reading — the era the canon-bound surfaces publish.

    Same single patch point as `pinned_canon`, for the same reason; only the
    reading differs. See the CANON_ERA note above for why the two eras are kept
    apart rather than reconciled by editing LIVE.
    """
    def _fake(force: bool = False) -> dict:
        return dict(CANON_ERA)
    monkeypatch.setattr(canonical_stats, "get_canonical_stats", _fake)
    return dict(CANON_ERA)


def _over(text: str):
    """The REAL rows_ne_buildings gate's verdict on `text`."""
    return _guard.check_facility_count_claims(text or "").get("over") or []


# ── the composed prompt: every story type, through the real gate ─────────────

STORY_TYPES = sorted(set(_engine._PULLERS) | {"capability_update"})


def _render_prompt(story_type: str) -> str:
    """The real prompt builder, with empty data — the standing-totals boilerplate
    does not depend on the pulled payload, which is exactly why it went out on
    story types whose own data was fine."""
    return _engine._build_user_prompt(
        story_type, {}, "https://dchub.cloud/whats-new") or ""


def test_story_type_list_is_not_empty():
    """A fence that iterates nothing is green forever."""
    assert len(STORY_TYPES) >= 5, (
        "the composer's story-type registry collapsed to %r — every parametrized "
        "assertion below would be vacuous" % (STORY_TYPES,))


@pytest.mark.parametrize("story_type", STORY_TYPES)
def test_composed_prompt_clears_the_rows_ne_buildings_gate(story_type, pinned_canon):
    """No prompt may hand the model a facility count above the citeable ceiling.

    The model is told these are EXACT figures it must not deviate from, so a
    row pile here is published verbatim — that is the whole mechanism behind the
    three posts in this file's docstring.
    """
    prompt = _render_prompt(story_type)
    over = _over(prompt)
    assert not over, (
        "linkedin_content_engine prompt for story_type=%r hands the composer a "
        "row count labelled as facilities — claim_breaker's rows_ne_buildings "
        "will refuse the post:\n  over: %s\nRead facilities_verified (distinct "
        "buildings) via _canon_media_phrases(), never canonical_stats"
        "['facilities'].""" % (story_type, [c.get("raw") for c in over]))


def test_at_least_one_prompt_actually_carries_a_facility_claim(pinned_canon):
    """Anti-vacuity for the parametrized test above.

    Every prompt passing because none of them mentions facilities at all would
    look identical to every prompt passing because they all quote buildings.
    """
    seen = {st: _guard.check_facility_count_claims(_render_prompt(st))["claims"]
            for st in STORY_TYPES}
    carriers = {st: c for st, c in seen.items() if c}
    assert carriers, (
        "no composed prompt states a facility count any more. If the standing-"
        "totals anchor was removed, re-point this fence at whatever replaced it "
        "— do not delete it: an unfenced anchor is how the row pile got in.")


def test_standing_totals_anchor_renders_both_canonical_numbers(pinned_canon):
    """RENDERED output, not a shape check.

    A count-free anchor satisfies "no over-claim" identically to a correct one,
    and so does a re-hardcoded-but-currently-right literal. Assert the anchor
    carries the two canon phrases as canonical_stats spells them.
    """
    prompt = _render_prompt("shipped_this_week")
    fac_phrase = canonical_stats.facilities_verified_phrase()
    deals_phrase = canonical_stats.deals_phrase()
    assert fac_phrase == "18,600+" and deals_phrase == "1,900+", (
        "the pinned reading no longer floors to the phrases this fence was "
        "written against (%r/%r) — re-derive the expectations from the SoT "
        "rather than editing LIVE to suit them" % (fac_phrase, deals_phrase))
    assert f"{fac_phrase} facilities" in prompt, (
        "the standing-totals anchor no longer states the canonical BUILDING "
        "count. Rendered prompt:\n" + prompt[:1200])
    assert f"{deals_phrase} tracked deals" in prompt, (
        "the standing-totals anchor no longer states the canonical DEDUPED deal "
        "count. Rendered prompt:\n" + prompt[:1200])


def test_no_prompt_claim_moves_with_the_row_pile(pinned_canon, monkeypatch):
    """THE PROPERTY, independent of today's magnitude.

    The gate tolerates 5%, so a row count that happens to sit just under the
    ceiling passes it while still being rows sold as buildings. Poison the two
    definitionally-row-pile inputs — `facilities` (COUNT(*)) and the raw deal
    ROW count — and re-render. A claim that moves with them is derived from a
    row pile, whatever the live reading happens to be that day.
    """
    baseline = {st: _render_prompt(st) for st in STORY_TYPES}
    poisoned_live = dict(LIVE, facilities=999999)
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda force=False: dict(poisoned_live))
    moved = []
    for st in STORY_TYPES:
        after = _render_prompt(st)
        if after != baseline[st]:
            moved.append("%s:\n    before: %s\n    after:  %s"
                         % (st, baseline[st][:300], after[:300]))
    assert not moved, (
        "a composed prompt moves when canonical_stats['facilities'] moves — "
        "that key is COUNT(*) FROM discovered_facilities, a ROW pile, never a "
        "building count. Read facilities_verified:\n  " + "\n  ".join(moved))


# ── two-sided: the gate and the pinning really can refuse ────────────────────

@pytest.mark.parametrize("published,label", [
    (PUBLISHED_08_20, "2026-08-20 h8 operator_spotlight"),
    (PUBLISHED_08_19, "2026-08-19 h12 interconnection"),
    (PUBLISHED_08_17, "2026-08-17 h12 deal"),
])
def test_the_published_copy_still_fails(published, label, pinned_canon):
    """Verbatim the strings that reached LinkedIn. If any of these passes, every
    assertion above is vacuous."""
    assert _over(published), (
        "the rows_ne_buildings gate no longer refuses the %s copy — this fence "
        "now proves nothing: %r" % (label, published))


def test_the_gate_is_measuring_against_buildings_not_rows(pinned_canon):
    """The ceiling itself. If check_facility_count_claims ever starts reading
    `facilities`, the row pile becomes self-certifying and every test in this
    file goes green on broken copy."""
    result = _guard.check_facility_count_claims("18,600+ facilities")
    assert result["live_distinct"] == DISTINCT, (
        "check_facility_count_claims' ceiling is %r, not the distinct-building "
        "count %r — composer and gate are measuring different things again"
        % (result["live_distinct"], DISTINCT))
    assert result["live_records"] == RECORDS, (
        "the gate lost sight of the raw record count, which is what lets copy "
        "legitimately say 'N source records'")


# ── the deals half: the canon phrase, never a literal ────────────────────────

def _load_drift_fence():
    """The REAL BANNED_STALE table from the count drift-fence, loaded by path so
    this does not depend on tests/ being importable as a package."""
    path = os.path.join(ROOT, "tests", "test_canonical_counts_drift.py")
    assert os.path.isfile(path), (
        "tests/test_canonical_counts_drift.py is gone — it OWNS the retired-count "
        "denylist this file reuses; do not re-declare the patterns here, that is "
        "how two canons drift apart")
    spec = importlib.util.spec_from_file_location("_drift_fence_for_media", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DRIFT = _load_drift_fence()
DEALS_STALE_RE = dict((tid, pat) for tid, pat, *_ in _DRIFT.BANNED_STALE)["deals_stale_floor"]


def _stale_deal_floor(text: str):
    """deals_stale_floor as the fence that OWNS it now EVALUATES it — the matched
    token, or None.

    ★2026-09-02: the raw pattern bans "2,000+ deals" outright, and 2,000+ is now
    the canon (PINNED['public']['deals'] walked 1,900+ -> 2,000+ against a live
    2,069 distinct). test_canonical_counts_drift closed exactly that hole the
    same day: _banned_stale_hits() exempts a token whose value IS the entry's own
    canonical_phrase — "a token whose value IS the canon value is not stale, by
    definition" — so RENDERED copy is judged through its evaluator rather than by
    re-matching its regex here, which would re-open the hole the owning fence just
    shut and make the honest number unpublishable.

    It is EQUALITY, not a floor: 4,000+ / 3,000+ / 2,200+ are still refused, and
    2,000+ is banned again the moment the canon floor moves past it, with no edit
    here. The SOURCE census below deliberately keeps using the raw pattern — a
    literal typed into shipped copy is a finding even when it is right today.
    """
    for tok_id, token, *_ in _DRIFT._banned_stale_hits(text or ""):
        if tok_id == "deals_stale_floor":
            return token
    return None


def test_borrowed_deals_pattern_is_not_vacuous():
    """It must fire on the literal that shipped and stay clean on the canon
    phrase — a borrowed regex that matches nothing is a silent green."""
    assert DEALS_STALE_RE.search("4,000+ tracked deals"), (
        "deals_stale_floor stopped matching the exact literal this PR removed")
    assert DEALS_STALE_RE.search("4,000+ M&amp;A deals tracked"), (
        "deals_stale_floor stopped matching the HTML-ESCAPED spelling — the "
        "served-footer form")
    assert not DEALS_STALE_RE.search("1,900+ tracked deals"), (
        "deals_stale_floor false-positives on the canonical floor")


def test_canon_aware_deal_check_still_refuses_the_retired_floor():
    """Anti-vacuity for _stale_deal_floor: the canon exemption must be EQUALITY,
    not an amnesty. If the canon-aware path stopped refusing "4,000+ tracked
    deals", the served-HTML test below would be proving nothing."""
    assert _stale_deal_floor("4,000+ tracked deals") == "4,000+ tracked deals", (
        "the canon-aware deals check no longer refuses the literal that shipped")
    assert _stale_deal_floor("M&amp;A across 4,000+ tracked deals"), (
        "the canon-aware deals check lost the HTML-ESCAPED served-footer form")
    canon_deals = (_canon_public_phrases() or ("", ""))[1]
    assert canon_deals, "ai_surface_canon publishes no deal phrase to exempt"
    assert _stale_deal_floor(f"{canon_deals} tracked deals") is None, (
        "the canon-aware check refuses the deal floor the canon itself publishes "
        "(%r) — that is the fence banning the number it advises" % canon_deals)


@pytest.mark.parametrize("story_type", STORY_TYPES)
def test_composed_prompt_carries_no_retired_deal_floor(story_type, pinned_canon):
    """No prompt may state a retired deal floor, at any magnitude."""
    prompt = _render_prompt(story_type)
    hit = DEALS_STALE_RE.search(prompt)
    assert not hit, (
        "linkedin_content_engine prompt for story_type=%r states %r — that "
        "floors duplicate deal ROWS against ~%s distinct. Bind "
        "canonical_stats.deals_phrase() via _canon_media_phrases()."
        % (story_type, hit.group(0), format(DEALS_DISTINCT, ",")))


def test_honesty_guard_advice_quotes_the_canonical_deal_floor(pinned_canon):
    """The guard told composers to publish the stale over-claim.

    An honesty guard handing out a retired number is the worst placement of the
    whole class: it launders it. Execute the real advice path and read what it
    would tell a composer to say.
    """
    advice = _guard._canon_deals_phrase()
    assert advice == canonical_stats.deals_phrase(), (
        "media_fact_check_guard's advice phrase (%r) no longer agrees with "
        "canonical_stats.deals_phrase() (%r)"
        % (advice, canonical_stats.deals_phrase()))
    assert not DEALS_STALE_RE.search(f"say '{advice} tracked deals'"), (
        "the honesty guard is advising a retired deal floor again: " + advice)


# ── source census: no module may re-type either count ────────────────────────

def _non_docstring_str_constants(path):
    """Every string constant in a module EXCEPT docstrings, with its line number.

    Docstrings are excluded precisely (first statement of Module/ClassDef/
    FunctionDef), not by keyword heuristic — this file and the modules it
    guards quote the retired numbers in their own docstrings on purpose, and a
    keyword allow-list would have to be widened every time someone words a
    comment differently. Comments are not Constant nodes, so they never reach
    here. f-string fragments DO: `f"21,401 data center facilities. "` is a
    Constant inside a JoinedStr, and that is exactly the shape that shipped.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    return [(getattr(n, "lineno", 0), n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in doc_ids]


# ★ The census bans a TYPED count outright, not just an over-claiming one.
#   Re-hardcoding today's correct value ("18,600+ facilities") is the blind spot
#   that outranks the others: it satisfies every over-claim check and every
#   "renders from canon?" membership check, and goes stale the instant the data
#   moves. So the rule is the strong one — no integer literal in front of the
#   noun at all; the number must be interpolated from canon.
#
#   The facility half reuses the guard's OWN _FACILITY_CLAIM_RE, so the census
#   and the gate recognise a claim identically (its 3-digit floor is why "added
#   19 facilities this week" is not a standing-total claim).
#
#   The deal half is deliberately narrowed to the STANDING-TOTAL shape — the
#   count must be qualified as tracked/distinct/M&A. Without that narrowing it
#   fires on the shipped_this_week prompt's worked example ("DC Hub added 133
#   data-center deals ... this week"), which is a weekly ADD, not an inventory
#   claim, and is followed by "pick the largest real numbers from the data
#   above". A fence that has to be silenced with an exemption list stops being
#   read; one that only ever fires on the real shape keeps its authority.
_TYPED_DEALS_TOTAL_RE = re.compile(
    r"\b\d[\d,]{2,12}\+?\s+"
    r"(?:(?:tracked|distinct|verified|deduped)\s+)*"
    r"(?:M&(?:amp;)?A\s+)?"
    r"(?:deals|transactions)\b(?:\s+tracked)?",
    re.I,
)


def _typed_counts(text):
    """Every typed facility / standing-total deal count in one string."""
    return ([m.group(0) for m in _guard._FACILITY_CLAIM_RE.finditer(text or "")]
            + [m.group(0) for m in _TYPED_DEALS_TOTAL_RE.finditer(text or "")])


@pytest.mark.parametrize("rel", COPY_MODULES, ids=[os.path.basename(p) for p in COPY_MODULES])
def test_copy_module_types_no_facility_or_deal_count(rel, pinned_canon):
    """No shipped string in these modules may TYPE either count.

    This is the half the render tests cannot cover: a derived value is invisible
    to a source scan, and a typed one is invisible to a render that never takes
    that branch. `21,401 facilities` sat in linkedin_quad_daily's legacy
    fallback — a live publish path (the engine's except-branch, and run_slot's
    "absolute last resort") that no composer test would ever have entered.
    """
    path = os.path.join(ROOT, rel)
    assert os.path.isfile(path), (
        "%s is missing — this census anchors to it. If the module moved, follow "
        "it here rather than dropping the surface." % rel)
    consts = _non_docstring_str_constants(path)
    assert consts, "%s yielded no scannable strings — the census went vacuous" % rel
    bad = []
    for lineno, text in consts:
        for hit in _typed_counts(text):
            bad.append("%s:%d types %r" % (rel, lineno, hit.strip()))
        stale = DEALS_STALE_RE.search(text)
        if stale:
            bad.append("%s:%d states %r — a retired deal floor"
                       % (rel, lineno, stale.group(0)))
    assert not bad, (
        "hardcoded facility/deal counts in shipped copy:\n  " + "\n  ".join(bad)
        + "\nInterpolate from canon — canonical_stats "
          "(facilities_verified_phrase / deals_phrase) or ai_surface_canon's "
          "{canon_facilities} / {canon_deals}. A literal that is right today is "
          "still a finding: it is right by coincidence and stale by the next "
          "ingest.")


def test_census_would_catch_the_literals_this_pr_removed(pinned_canon):
    """Anti-vacuity for the census.

    The first five are verbatim what was in the tree on 2026-08-23. The last two
    are the harder case: a literal re-typed with TODAY'S CORRECT value, which
    passes every over-claim check and every placeholder-presence check and is
    exactly how a fixed surface silently rots.
    """
    removed = [
        '21,401 data center facilities. 4,000+ M&A deals. 10 ISOs tracked in real time.',
        'Refreshed Fridays. Sourced from 21,401 facilities + DCPI.',
        'in milliseconds — pulled from 21,401 facilities, 285 ',
        '<p>{_CANON_FAC} facilities · 7 ISO grid feeds · 4,000+ M&amp;A deals tracked</p>',
        "dollar aggregate not corroborated — say '4,000+ tracked deals'",
        '18,600+ facilities',       # correct on 2026-08-23, stale by the next ingest
        '1,900+ tracked deals',     # ditto
    ]
    missed = [s for s in removed
              if not (_typed_counts(s) or DEALS_STALE_RE.search(s))]
    assert not missed, (
        "the census no longer catches literals that were live in this repo on "
        "2026-08-23, or a re-hardcoded current value — it has gone blind to the "
        "class it exists for:\n  " + "\n  ".join(missed))


def test_census_stays_clean_on_honest_copy(pinned_canon):
    """The other half of a non-vacuous pattern: it must NOT fire on copy that is
    already correct, or the fence gets suppressed rather than fixed."""
    honest = [
        '(e.g. "DC Hub added 133 data-center deals and 19 facilities to its live '
        'index this week" — pick the largest real numbers from the data above)',
        "26,388 source records behind 18,656 distinct buildings",
        "Search facilities by city/state/operator/MW.",
        " facilities, ",
        "7 ISO grid feeds",
    ]
    noisy = [s for s in honest if _typed_counts(s) or DEALS_STALE_RE.search(s)]
    assert not noisy, (
        "the census fires on copy that is honest — a fence that cries wolf gets "
        "exemption-listed, and then it is not a fence:\n  " + "\n  ".join(noisy))


# ── the other surfaces, RENDERED (a derived read is invisible to the census) ──
#
# The census above only sees TYPED numbers. A module that reads the wrong
# canonical KEY — `facilities` (COUNT(*)) instead of `facilities_verified` —
# types nothing at all and sails straight through it. That is the original
# defect's exact shape, so each surface below is executed and its real output
# run through the real gate.

def test_agent_broadcast_coverage_item_quotes_buildings(pinned_canon):
    """The ~93K-agent broadcast feed published `facilities` — the row pile — as
    "N data-center facilities"."""
    ab = pytest.importorskip("routes.agent_broadcast", reason="needs Flask")
    items = ab._fetch_data_growth(7)
    assert items, (
        "the data_coverage broadcast item vanished — if the canonical key it "
        "reads was renamed, _fetch_data_growth returns [] and the item silently "
        "stops reaching agents; re-point it rather than deleting this test")
    for it in items:
        blob = " ".join(str(it.get(k) or "") for k in ("title", "summary"))
        over = _over(blob)
        assert not over, (
            "agent_broadcast publishes a row count as facilities to the agent "
            "feed: %s\n  %s" % ([c.get("raw") for c in over], blob))
    assert any(format(DISTINCT, ",") in (it.get("summary") or "") for it in items), (
        "the coverage item no longer states the canonical BUILDING count %s — "
        "avoiding a claim entirely passes the gate identically to quoting the "
        "right one, so assert the number is really there: %r"
        % (format(DISTINCT, ","), [i.get("summary") for i in items]))


def test_quad_legacy_fallback_bodies_clear_the_gate(pinned_canon):
    """The legacy quad templates ARE a publish path, for every real slot.

    run_slot reaches them twice — the engine's except-branch, and the "absolute
    last resort" when the composer returns nothing — and its generic branch
    carried "21,401 data center facilities. 4,000+ M&A deals. 10 ISOs tracked in
    real time." Render each real slot with no payload (the shape that lands on
    the generic fallback) and gate the body.
    """
    quad = pytest.importorskip("routes.linkedin_quad_daily", reason="needs Flask")
    slots = list(quad.SLOTS)
    assert slots, "SLOTS is empty — this test would iterate nothing"
    bodies = {}
    for slot in slots:
        body = quad._format_post_base(slot, {})
        bodies[slot["topic"]] = body
        over = _over(body)
        assert not over, (
            "quad fallback for slot %r states a facility count above the "
            "citeable ceiling: %s\n%s"
            % (slot["topic"], [c.get("raw") for c in over], body))
        stale = DEALS_STALE_RE.search(body)
        assert not stale, ("quad fallback for slot %r states a retired deal "
                           "floor %r" % (slot["topic"], stale.group(0)))
    fac = canonical_stats.facilities_verified_phrase()
    deals = canonical_stats.deals_phrase()
    generic = [b for b in bodies.values() if "DC Hub Media ·" in b]
    assert generic, (
        "no slot now reaches the generic last-resort body — if the branch moved, "
        "re-point this test at it; that body is what ships when the composer "
        "returns nothing at all")
    assert all(fac in b and deals in b for b in generic), (
        "the generic quad fallback no longer states both canonical phrases (%r / "
        "%r) — a count-free body passes the gate identically to a correct one:\n%s"
        % (fac, deals, generic[0]))


def test_openapi_spec_counts_are_canonical(pinned_canon, monkeypatch):
    """/openapi-live.json advertised pg_class.reltuples for BOTH counts —
    26,388 "facilities" and 4,979 "M&A deals", to machine readers."""
    od = pytest.importorskip("routes.openapi_dynamic", reason="needs Flask")
    monkeypatch.setitem(od._COUNT_CACHE, "counts", None)
    monkeypatch.setitem(od._COUNT_CACHE, "ts", 0)
    counts = od._get_counts()
    assert counts["facilities"] == DISTINCT, (
        "openapi_dynamic publishes %r as facilities; the citeable building count "
        "is %r (%r is the raw ROW pile)"
        % (counts["facilities"], DISTINCT, RECORDS))
    assert counts["deals"] == DEALS_DISTINCT, (
        "openapi_dynamic publishes %r as M&A deals; the deduped distinct count "
        "is %r (deal ROWS over-state ~2.9x)" % (counts["deals"], DEALS_DISTINCT))


def test_competitive_comparison_self_metrics_are_canonical(pinned_canon, monkeypatch):
    """/api/v1/competitive/comparison served "21,374 facilities · 178 countries ·
    1,852 M&A deals tracked" — the frozen except-branch literals, which were the
    only values it had ever served (its /api/health probe carries no
    facility_count at all)."""
    ci = pytest.importorskip("routes.competitor_intel", reason="needs Flask")
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no self-probe in CI")))
    m = ci._dchub_self_metrics()
    assert m.get("facilities") == DISTINCT, (
        "competitor_intel reports %r facilities; canonical buildings is %r"
        % (m.get("facilities"), DISTINCT))
    assert m.get("deals") == DEALS_DISTINCT, (
        "competitor_intel reports %r deals; canonical distinct deals is %r"
        % (m.get("deals"), DEALS_DISTINCT))


def test_welcome_email_and_seo_footer_clear_the_gate(pinned_canon_today):
    """Served HTML: the SEO footer and the new-payer welcome email both typed
    "4,000+ M&A deals". These render from ai_surface_canon's {canon_*}
    placeholders, so this also catches the worse failure — a placeholder that
    stops being passed through canon_text() and serves the literal
    "{canon_deals}" to a reader.

    ★2026-09-02: this is the ONE test in the file pinned to the canon era rather
    than to the 2026-08-23 replay, because it is the one whose surfaces bind
    ai_surface_canon at import time — see the CANON_ERA note at the top. The
    canon walk (facilities "18,500+" -> "20,100+", deals "1,900+" -> "2,000+")
    left it gating today's canon against last month's 18,656-building ceiling,
    where honest growth read as an over-claim; and the borrowed deals regex bans
    "2,000+ deals", which is now the canon itself, so the rendered halves go
    through the owning fence's canon-aware evaluator.
    """
    orec = pytest.importorskip("routes.onboarding_recover", reason="needs Flask")
    seo = pytest.importorskip("routes.seo_pages", reason="needs Flask")
    email = orec._welcome_html("Jordan Lee", "Starter", "jordan@example.com")
    footer = seo._base_html(title="t", description="d", canonical="https://dchub.cloud/x",
                            og_image="https://dchub.cloud/og.png", schema_jsonld="{}",
                            body_html="<p>body</p>")
    for label, blob in (("welcome email", email), ("seo footer", footer)):
        over = _over(blob)
        assert not over, ("%s states a facility count above the citeable ceiling: %s"
                          % (label, [c.get("raw") for c in over]))
        stale = _stale_deal_floor(blob)
        assert not stale, ("%s states a retired deal floor: %r" % (label, stale))
        assert "{canon_" not in blob, (
            "%s is serving an UNRESOLVED canon placeholder — worse than the stale "
            "number it replaced. Wrap the string in ai_surface_canon.canon_text()."
            % label)
    fac, deals = _canon_public_phrases()
    assert fac and fac in email and deals and deals in email, (
        "the welcome email no longer states the canonical facility (%r) and deal "
        "(%r) phrases — a count-free email passes the gate identically to a "
        "correct one" % (fac, deals))
    assert fac in footer and deals in footer, (
        "the SEO footer no longer states both canonical phrases")
    # ★2026-09-02 — the DEALS magnitude gate, which until now the value-pinned
    # denylist stood in for. _stale_deal_floor exempts a token whose value IS the
    # canon, so the canon must itself be measured against the live deduped count,
    # or a canon walked back onto the deal ROW pile would exempt itself here —
    # and "4,000+" was exactly that (rows, ~2.9x distinct). The facilities half
    # has had this all along, in check_facility_count_claims' ceiling.
    deals_floor = int(re.sub(r"\D", "", deals.split("+")[0]) or 0)
    assert 0 < deals_floor <= pinned_canon_today["deals"], (
        "ai_surface_canon publishes a deal floor of %r against %s live DEDUPED "
        "deals — a floor rounds DOWN and never above the resolver, and a floor "
        "over the row pile is how '4,000+ tracked deals' shipped"
        % (deals, format(pinned_canon_today["deals"], ",")))


def _canon_public_phrases():
    """(facilities, deals) as ai_surface_canon publishes them — the values the
    served-HTML surfaces bind at import time via canon_text()."""
    import ai_surface_canon as _asc
    nums = _asc.canon_nums()
    return nums.get("{canon_facilities}") or "", nums.get("{canon_deals}") or ""


# ── the one source both sides must read ──────────────────────────────────────

def _reads_key(path, fn_name, key):
    """True if function `fn_name` in `path` reads dict key `key` anywhere."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn, "%s not found in %s" % (fn_name, path)
    return any(isinstance(n, ast.Constant) and n.value == key
               for n in ast.walk(fn))


def test_composer_and_gate_read_the_same_canonical_key():
    """The composer's ceiling and the gate's ceiling must be ONE key.

    canonical_stats fills `facilities_verified` from COUNT(DISTINCT
    canonical_slug) WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT
    NULL. The gate measures against it; facilities_verified_phrase() floors it
    DOWN, so the anchor can never sit above the ceiling. Drift here re-opens the
    bug with both sides looking correct in isolation — the #3111 failure mode.
    """
    cs_path = os.path.join(ROOT, "canonical_stats.py")
    guard_path = os.path.join(ROOT, "routes", "media_fact_check_guard.py")
    assert _reads_key(guard_path, "_live_facility_counts", "facilities_verified"), (
        "media_fact_check_guard._live_facility_counts stopped reading "
        "facilities_verified — the gate's ceiling moved off distinct buildings")
    assert _reads_key(cs_path, "facilities_verified_phrase", "facilities_verified"), (
        "canonical_stats.facilities_verified_phrase stopped reading "
        "facilities_verified — the composer's anchor moved off the gate's key")
    distinct_sql = ("SELECT COUNT(DISTINCT canonical_slug) FROM "
                    "discovered_facilities WHERE COALESCE(is_duplicate,0)=0 "
                    "AND canonical_slug IS NOT NULL")
    folded = {" ".join(n.value.split()) for n in
              ast.walk(ast.parse(open(cs_path, encoding="utf-8").read()))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert distinct_sql in folded, (
        "canonical_stats no longer runs the distinct-building query this fence "
        "pins. Every 'N facilities' claim in the repo measures against it — "
        "re-point BOTH the composer and the gate together, never one.")


@pytest.mark.parametrize("rel", [
    os.path.join("routes", "linkedin_content_engine.py"),
    os.path.join("routes", "linkedin_quad_daily.py"),
], ids=["content_engine", "quad_daily"])
def test_media_modules_never_bind_the_raw_facilities_phrase(rel):
    """`facilities_phrase()` is the RAW discovery-pile floor (back-compat, and
    correctly named as such in canonical_stats). It is one character away from
    facilities_verified_phrase() and reintroduces the exact bug, so no media
    composer may import or call it."""
    tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            offenders += [a.lineno for a in node.names if a.name == "facilities_phrase"]
        elif isinstance(node, ast.Name) and node.id == "facilities_phrase":
            offenders.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "facilities_phrase":
            offenders.append(node.lineno)
    assert not offenders, (
        "%s binds canonical_stats.facilities_phrase() at line(s) %s — that is "
        "the RAW row floor (~26,000), not buildings (~18,600). Use "
        "facilities_verified_phrase()." % (rel, offenders))


@pytest.mark.parametrize("modname", ["routes.linkedin_content_engine",
                                     "routes.linkedin_quad_daily"])
def test_canon_media_phrases_fails_open_to_no_number(modname, monkeypatch):
    """An unreadable canon must yield a count-FREE sentence, never a literal.

    A literal in the fallback slot is exactly how "4,000+" survived here: it
    looked like a rare degrade path and was in fact the only value some surfaces
    ever served (see competitor_intel, whose except-branch was the product).
    """
    mod = pytest.importorskip(modname, reason="needs Flask")
    # a canonical_stats with no phrase helpers => the import inside raises
    monkeypatch.setitem(sys.modules, "canonical_stats", type(sys)("canonical_stats"))
    assert mod._canon_media_phrases() == ("", ""), (
        "%s._canon_media_phrases() returned a value with the canon unreadable — "
        "the fallback must carry no number at all" % modname)


def test_guard_deal_phrase_fails_open_to_no_number(monkeypatch):
    """The honesty guard's own advice helper must degrade to a count-free
    sentence, not to a literal.

    ★ Found by mutation-testing this file, not by it firing: restoring
    `return cs.deals_phrase() or "4,000+"` left every other assertion green.
    Under a healthy canon the left side always wins, so the fallback is never
    evaluated — a stale value can sit there indefinitely and surface only on the
    day the DB is unreachable, which is the day nobody is reading advice strings
    carefully. This is the `or <literal>` shape that made /by-the-numbers serve
    a frozen "33 tools" for months: the fallback WAS the product.
    """
    monkeypatch.setitem(sys.modules, "canonical_stats", type(sys)("canonical_stats"))
    assert _guard._canon_deals_phrase() == "", (
        "media_fact_check_guard._canon_deals_phrase() returned a value with the "
        "canon unreadable — the fallback must carry no number at all")


# Every helper whose job is "resolve a canon phrase, or nothing".
_CANON_HELPERS = [
    (os.path.join("routes", "linkedin_content_engine.py"), "_canon_media_phrases"),
    (os.path.join("routes", "linkedin_quad_daily.py"), "_canon_media_phrases"),
    (os.path.join("routes", "media_fact_check_guard.py"), "_canon_deals_phrase"),
]


@pytest.mark.parametrize("rel,fn_name", _CANON_HELPERS,
                         ids=["%s:%s" % (os.path.basename(r), f) for r, f in _CANON_HELPERS])
def test_canon_helper_bodies_contain_no_digits_at_all(rel, fn_name):
    """Structural companion to the fail-open tests above.

    Those exercise ONE degrade path (the import failing). A count could still be
    parked in an `or` fallback, a bare `except: return "1,900+"`, or a default
    argument, and stay unreachable until the day it is not. So require the whole
    helper body to be free of any digit-bearing string — the only form that
    cannot hide a stale value anywhere in it. Docstrings are exempt: they are
    where the retired numbers get explained.
    """
    path = os.path.join(ROOT, rel)
    fn = next((n for n in ast.walk(ast.parse(open(path, encoding="utf-8").read()))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn, "%s not found in %s — it is the module's single canon binding" % (fn_name, rel)
    doc = ast.get_docstring(fn, clean=False)
    numeric = sorted({n.value for n in ast.walk(fn)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and n.value != doc and any(ch.isdigit() for ch in n.value)})
    assert not numeric, (
        "%s.%s parks a digit-bearing literal in its body: %s. A canon helper "
        "resolves the live phrase or returns nothing — a number here is "
        "unreachable until the canon breaks, and then it is the only value "
        "anyone sees." % (rel, fn_name, numeric))


def test_unreadable_canon_drops_the_number_from_the_prompt(monkeypatch):
    """End-to-end on the fail-open path: the rendered anchor loses its counts
    rather than falling back to a frozen pair."""
    monkeypatch.setattr(_engine, "_canon_media_phrases", lambda: ("", ""))
    prompt = _render_prompt("shipped_this_week")
    assert not _guard.check_facility_count_claims(prompt)["claims"], (
        "the standing-totals anchor still states a facility count with the canon "
        "unreadable:\n" + prompt[:1200])
    assert not DEALS_STALE_RE.search(prompt)
    assert re.search(r"the live index — updated daily", prompt), (
        "the count-free anchor is missing entirely; the prompt should still tell "
        "the model the index is updated daily:\n" + prompt[:1200])
