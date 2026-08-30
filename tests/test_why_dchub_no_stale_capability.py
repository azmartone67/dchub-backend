"""why_dchub must not sell a withdrawn capability, or a frozen tool count.

WHY THIS EXISTS, and why the guard that was supposed to cover it could not.

dchub-mcp-server/test/no-live-dcgi-claims.test.mjs was written on 2026-08-22 for
exactly this defect. Its own comment names the offender:

    server.mjs is covered by its own narrower assertion: the tool descriptions
    legitimately name the DCGI many times WITH the withdrawal; the one sales
    string that did not was why_dchub.

Correct diagnosis. The assertion it then wrote:

    expect(read('server.mjs').includes('DCPI + DCGI indices')).toBe(false);

Three things wrong with it, each independently fatal:

  1. WRONG STRING. The shipped label reads "Proprietary live indices
     (DCPI + DCGI)". The literal 'DCPI + DCGI indices' has never existed
     anywhere in the codebase, so the assertion could only ever pass.
  2. WRONG FILE. why_dchub's sales copy is not in server.mjs.
  3. WRONG REPO. It is built HERE, in dchub-backend/routes/competitive_intel.py,
     which that test cannot see at all.

So the guard was green for eight days while the live server kept advertising a
score withdrawn on 2026-08-08 — with a proof URL and a citation line, on the one
tool whose entire job is to hand agents citable positioning facts. Measured live
2026-08-30, why_dchub still returned:

    "Two proprietary indices recomputed daily: ... and the DC Hub Gas Index
     (DCGI) scores gas access and cost by state."

Pulling the DCGI rather than shipping a 5.5x disagreement was the right call.
Leaving this string live spent that integrity anyway, and an outside AI auditing
DC Hub caught it from the public side before any of our own checks did.

This test asserts over the ACTUAL objects why_dchub serves, not over a source
literal, so it cannot go stale when the wording changes — which is precisely how
the previous guard failed.
"""
import re

import pytest

competitive_intel = pytest.importorskip("routes.competitive_intel")


def _claim_strings():
    """Every human-readable string in the EDGES list why_dchub returns."""
    edges = getattr(competitive_intel, "EDGES", None)
    if edges is None:
        for name in dir(competitive_intel):
            val = getattr(competitive_intel, name)
            if isinstance(val, (list, tuple)) and val and isinstance(val[0], dict) \
                    and "proof" in val[0] and "label" in val[0]:
                edges = val
                break
    assert edges, "could not locate why_dchub's edges list — it was renamed or removed"
    out = []
    for e in edges:
        for key in ("label", "value"):
            v = e.get(key)
            if isinstance(v, str):
                out.append(v)
    return out


def test_no_edge_names_the_dcgi_without_saying_it_was_withdrawn():
    # The word may appear — an agent asking about the DCGI deserves the honest
    # answer. It may never appear as a live capability.
    offenders = [s for s in _claim_strings()
                 if re.search(r"\bDCGI\b|Gas Index", s) and not re.search(r"withdrawn", s, re.I)]
    assert offenders == [], (
        "why_dchub advertises the withdrawn DCGI as live:\n  " + "\n  ".join(offenders))


def test_no_edge_claims_two_live_proprietary_indices():
    # The specific phrasing that shipped. DCPI is live and singular; any copy
    # promising a second live index is selling the withdrawn one.
    offenders = [s for s in _claim_strings()
                 if re.search(r"\btwo\b[^.]{0,40}\bindices\b", s, re.I)]
    assert offenders == [], (
        "why_dchub still promises two live proprietary indices:\n  " + "\n  ".join(offenders))


def test_no_claim_string_hardcodes_a_tool_count():
    # This module's own header records the class: "the hardcoded '40+ tools'
    # here drifted 40->82 unnoticed". That fix landed on ONE edge and missed the
    # pitch and the outreach blurb, so a single response shipped "83+ tools" and
    # "40+ tools" at once. Every count must come from _TOOLS_FLOOR (canon-bound).
    floor = int(competitive_intel._TOOLS_FLOOR)
    src = open(competitive_intel.__file__, encoding="utf-8").read()
    live = [ln for ln in src.split("\n")
            if not ln.lstrip().startswith("#") and re.search(r"\b\d{2,3}\+ tools\b", ln)]
    bad = [ln.strip() for ln in live
           if not re.search(rf"\b{floor}\+ tools\b", ln)]
    assert bad == [], (
        "a tool count is typed as a literal instead of derived from _TOOLS_FLOOR "
        f"(canon = {floor}):\n  " + "\n  ".join(bad))


def test_the_tool_floor_is_canon_bound_not_a_frozen_literal():
    # Fail-open literal is allowed (canon_text's contract: never a wrong number),
    # but it must not be the value actually served when canon is readable.
    floor = int(competitive_intel._TOOLS_FLOOR)
    assert floor >= 40, "tool floor collapsed below the retired 40 literal"
    try:
        from ai_surface_canon import PINNED
    except Exception:
        pytest.skip("ai_surface_canon unreadable in this environment")
    assert floor == int(PINNED["tools_advertised"]), (
        "why_dchub's tool floor has drifted from ai_surface_canon.PINNED"
        f" ({floor} vs {PINNED['tools_advertised']})")


# ── One payload, one facility count ────────────────────────────────────────
#
# ★2026-08-30. why_dchub served THREE magnitudes for "facilities" in a single
# response: 18,500+ on the coverage edge, 19,700+ in the pitch, and 21,900+ in
# the provenance note. The third is a genuinely different population (raw
# tracked, pre-dedup) and is labelled as such. The first two were the SAME
# population disagreeing with itself: the edge baked
# PINNED["public"]["facilities"] into a string at IMPORT time while the pitch
# went through canon_text() at request time.
#
# ai_surface_canon._live_public_floors() had already written the rule down —
# "The pin is now a COLD-START floor; canonical_stats' cache is what publishes",
# and any surface reading PINNED without the resolver "served a floor one cycle
# stale, forever". Nothing enforced it. These two tests do.

import ast


def _canon_facilities():
    from ai_surface_canon import canon_nums
    return canon_nums().get("{canon_facilities}")


def test_the_facility_edge_carries_the_resolver_value_not_a_frozen_pin():
    val = _canon_facilities()
    assert val, "canon_nums() has no {canon_facilities} — the guard would be vacuous"
    fac = [d for d in competitive_intel._resolved_differentiators()
           if d.get("key") == "facilities"]
    assert fac, "the facilities differentiator disappeared"
    assert val in fac[0]["value"], (
        f"the facilities edge does not carry the resolver value {val!r}: "
        f"{fac[0]['value']!r}")


def test_no_differentiator_states_a_facility_count_other_than_canon():
    # Catches the original defect and any future hardcode, without caring which
    # number canon currently holds.
    val = _canon_facilities()
    bad = []
    for d in competitive_intel._resolved_differentiators():
        for m in re.finditer(r"[\d,]+\+(?=\s+(?:physical\s+|global\s+)?"
                             r"(?:data.?cent(?:er|re)s?|facilities))",
                             str(d.get("value", ""))):
            if m.group(0) != val:
                bad.append((d.get("key"), m.group(0)))
    assert not bad, (
        f"differentiator(s) state a facility count that is not canon ({val}): {bad}")


def test_nothing_reads_the_differentiator_list_around_the_resolver():
    # The structural half. Reading _DCHUB_DIFFERENTIATORS directly is how the
    # frozen value got served in the first place, so the list has exactly one
    # legitimate reader.
    src = open(competitive_intel.__file__, encoding="utf-8").read()
    tree = ast.parse(src)

    resolver = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_resolved_differentiators"), None)
    assert resolver, "_resolved_differentiators() is gone — the single reader vanished"
    allowed = {id(n) for n in ast.walk(resolver)
               if isinstance(n, ast.Name) and n.id == "_DCHUB_DIFFERENTIATORS"}

    stray = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "_DCHUB_DIFFERENTIATORS"
             and isinstance(n.ctx, ast.Load) and id(n) not in allowed]
    assert not stray, (
        "_DCHUB_DIFFERENTIATORS is read outside _resolved_differentiators() at line(s) "
        f"{stray} — that path serves values frozen at import. Use the resolver.")
