#!/usr/bin/env python3
"""tests/test_sitemap_thin_gate.py — submit only pages that can rank, and never
submit none.

NO NETWORK, NO DB.

GSC 2026-08-06: 1,281 indexed against 19,323 submitted (6.6%), with "Duplicate
without user-selected canonical" at 2,822 and "Duplicate, Google chose different
canonical" at 1,033.

None of the usual suspects were at fault — measured 2026-08-14. Sampled sitemap
URLs return 200 with no redirect; facility pages are SERVER-RENDERED for
Googlebot, self-canonical, index,follow. Google can read them. It is declining
them, and the duplicate buckets are it saying why.

The discriminator, 4/4 on sampled pages:

    Perseus Pittsburgh   power_mw 99    3,347 chars   68% unique
    Equinix NJ Campus    power_mw 80    2,999 chars   65% unique
    County Mayo Campus   power_mw NULL  1,298 chars   23% unique
    Frontier Lakeland 2  power_mw NULL  1,369 chars   22% unique

Without a capacity the page is a name, a slug and shared chrome.

★ THIS REVERSES THE 2026-07-01 CHANGE, on evidence. That one unioned the legacy
table in because "their absence = the bulk of GSC's ~30k not indexed". Absence
was never the cause — the pages were crawled and rejected — so widening the
sitemap added thin pages and the indexed count did not follow.

★ AND IT IS THE ONE CHANGE HERE THAT CAN ONLY REMOVE URLS. A bad query or an
emptied power_mw column would shrink the sitemap toward nothing and de-index the
site, and that failure looks exactly like success: a valid sitemap, served 200,
with fewer entries. Hence the floor.

Live-fired against production 2026-08-14, both ways, comparing <loc> sets:

    gate off (today)   19,656 URLs   18,064 facilities
    gate on            9,207 URLs     7,615 facilities
    removed 10,449 (57.8%), ADDED 0 — a strict subset, as a narrowing
    predicate must be. Floor 2,000, so the collapse guard does not trip.

19,656 corroborates the diagnosis independently: GSC reports 19,323 submitted.

One second-order effect, measured: comparing whole <url> blocks showed "+2".
Both were the SAME loc with an earlier lastmod. Equinix PA2/PA3 each have two
live rows sharing one canonical_slug — a real one (22 MW / 30 MW, Paris) and an
empty twin (0 MW, Saint-Denis) — and the facility queries carry no ORDER BY, so
which row wins the provider|name collision is decided by scan order. Adding the
predicate drops the empty twin and the real row wins. That is an improvement,
but the nondeterminism is pre-existing and unrelated; it is not fixed here.

Run standalone:   python3 tests/test_sitemap_thin_gate.py
Run under pytest: pytest tests/test_sitemap_thin_gate.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")


def _builder():
    """Source of _build_sitemap_sections, comments stripped.

    The comments quote the old ungated SQL on purpose; matching prose would
    read the history as the current behaviour. Five guards in this repo have
    already made that mistake."""
    s = open(SRC, encoding="utf-8").read()
    i = s.index("def _build_sitemap_sections(")
    j = s.index("\ndef ", i + 100)
    return "\n".join(l for l in s[i:j].splitlines()
                     if not l.lstrip().startswith("#"))


def _full():
    return open(SRC, encoding="utf-8").read()


def test_the_gate_exists_and_is_capacity_based():
    b = _builder()
    assert "_thin_excl" in b, "the capacity gate is gone"
    assert re.search(r'_thin_excl\s*=\s*"AND COALESCE\(power_mw, 0\) > 0"', b), (
        "the gate must select on power_mw — sqft/operational_year/investment/"
        "acreage together add only 2 rows and are not discriminators"
    )


def test_it_gates_every_query_that_emits_facility_urls():
    """★ FOUR queries feed fac_rows: discovered_facilities primary, its
    minimal-column fallback, the legacy union, and the legacy fallback. Each
    fallback fires on a schema error, so an ungated one silently restores all
    ~17.9k thin URLs and the change looks applied while doing nothing.

    The builder ALSO runs discovered_facilities queries that emit no facility
    URL — the duplicate-slug sets, the country/state list, the market join.
    Those must NOT be gated: excluding thin rows there would drop markets and
    locations that have facilities. So this selects on the emitting queries
    specifically, which are the ones bounded by LIMIT 50000.

    ★★ AMENDED 2026-08-19 (r-proven-exempt). There are now TWO further queries
    that add rows to fac_rows and are deliberately NOT gated: the proven-
    rankable readmission, which reads slugs GSC has served impressions for and
    pulls them back regardless of capacity. They are excluded from this check
    on purpose, and they are pinned by their own tests below rather than left
    to pass here on the technicality that they do not say LIMIT 50000.
    Bounding this assertion to LIMIT 50000 is what keeps it meaningful: the
    four gated emitters are the unbounded population scans, and an ungated
    fifth ONE OF THOSE would still restore the whole thin set."""
    b = _builder()
    emitters = [m.start() for m in re.finditer(r"LIMIT 50000", b)]
    assert len(emitters) == 4, (
        f"expected 4 facility-emitting population queries, found "
        f"{len(emitters)} — if a fifth was added it needs the gate too"
    )
    for pos in emitters:
        stmt = b.rfind("c.execute", 0, pos)
        assert stmt != -1 and "_thin_excl" in b[stmt:pos], (
            "a facility-emitting query is not gated — on a schema error this "
            "branch would quietly restore the full thin sitemap"
        )


def test_the_gate_can_only_remove_urls():
    """An AND narrows; an OR would widen and could re-admit the thin pages."""
    b = _builder()
    m = re.search(r'_thin_excl\s*=\s*"([^"]+)"', b)
    assert m, "gate expression not found"
    expr = m.group(1)
    assert expr.strip().upper().startswith("AND "), (
        f"the gate must be an AND clause; got {expr!r}"
    )
    assert " OR " not in expr.upper(), "an OR would widen the set, not narrow it"


def test_it_has_a_kill_switch():
    b = _builder()
    assert "SITEMAP_THIN_GATE_DISABLE" in b, (
        "a change that removes 14,000 URLs must be revertible without a deploy"
    )


def test_a_collapse_falls_back_instead_of_de_indexing():
    """★ The failure that looks like success: a valid, smaller sitemap."""
    b = _builder()
    assert "_SITEMAP_THIN_GATE_FLOOR" in b, "no collapse floor"
    assert "_build_sitemap_facilities_ungated" in b, (
        "a collapse must fall back to the ungated set, not serve the collapsed one"
    )
    f = _full()
    m = re.search(r"_SITEMAP_THIN_GATE_FLOOR\s*=\s*(\d+)", f)
    assert m, "floor constant not defined"
    floor = int(m.group(1))
    # 7,971 URLs pass the gate today across both sources. The floor must sit
    # well under that (so ordinary movement never trips it) and well over zero.
    assert 100 < floor < 7971, f"floor {floor} is not between 0 and the live count"


def test_the_fallback_cannot_recurse():
    """The fallback re-enters the builder. It is safe ONLY because the floor
    check is conditioned on the gate being on, and the fallback turns it off."""
    b = _builder()
    m = re.search(r"if _thin_gate_on and len\(_fac\) < _SITEMAP_THIN_GATE_FLOOR", b)
    assert m, (
        "the floor check must be guarded by `_thin_gate_on` — without it the "
        "ungated fallback re-enters the builder, trips the floor again, and "
        "recurses forever"
    )
    f = _full()
    i = f.index("def _build_sitemap_facilities_ungated(")
    body = f[i: i + 900]
    assert "SITEMAP_THIN_GATE_DISABLE" in body and "'1'" in body, (
        "the fallback must disable the gate before re-entering"
    )
    assert "finally" in body, "the fallback must restore the previous env value"


def test_the_gate_is_emission_only():
    """SITEMAP EMISSION ONLY. Slugs stay frozen and every page keeps serving
    200 — a page qualifies again the moment its record gets a capacity, with no
    redeploy. The builder has separate machinery that DOES noindex pages
    (r-ner-noindex, _is_junk_facility); a thin page is not junk, it is
    unfinished, and it must not be fed into that. So: every use of the gate is
    a SQL string concatenation and nothing else."""
    b = _builder()
    uses = [m.start() for m in re.finditer(r"_thin_excl", b)]
    assert len(uses) == 5, f"expected 1 definition + 4 uses, found {len(uses)}"
    for pos in uses[1:]:
        line = b[b.rfind("\n", 0, pos) + 1: b.find("\n", pos)]
        assert re.match(r'^\s*"""\s*\+\s*_thin_excl\s*\+\s*"""\s*$', line), (
            f"the gate must only ever be concatenated into SQL; found: {line.strip()!r}"
        )
    # Capacity must reach SQL only through the gate. A query that hardcoded
    # power_mw would be a second, ungoverned copy of this policy — invisible to
    # the kill switch and to the collapse floor.
    for m in re.finditer(r"c\.execute\(", b):
        depth, k = 0, m.end() - 1
        while k < len(b):
            depth += (b[k] == "(") - (b[k] == ")")
            if depth == 0:
                break
            k += 1
        sql = b[m.start(): k]
        assert "power_mw" not in sql.replace("_thin_excl", ""), (
            "a query references power_mw directly instead of through the gate — "
            "the kill switch and the collapse floor would not govern it"
        )


# ---------------------------------------------------------------------------
# r-proven-exempt (2026-08-19) — ground truth overrides the proxy.
#
# The gate above uses power_mw as a stand-in for "can this page rank".
# Measured against GSC 2026-08-19, cross-referencing the top-100 pages (28d)
# against the live sitemap:
#
#     in sitemap  33 pages  94 clicks  median pos 8.2  33/33 have capacity
#     DROPPED     32 pages  79 clicks  median pos 8.0   2/32 have capacity
#
# The 100%/6% split proves the gate decides membership; the medians prove the
# dropped pages rank NO WORSE. 13 already-page-1 pages were removed, including
# microsoft-cph07 (position 2.9, CTR 13.79%, power_mw 0.0).
#
# The gate stays — the 08-14 evidence for it (1,281 indexed / 19,323 submitted)
# is real. What changes is that a page GOOGLE ALREADY RANKS outranks the proxy.
# ---------------------------------------------------------------------------


def test_proven_readmission_exists_and_is_gsc_sourced():
    """The exemption must be evidence-based, not a widened predicate."""
    b = _builder()
    assert "seo_proven_pages" in b, (
        "the proven-rankable readmission is gone — the capacity gate is back "
        "to dropping pages Google ranks at position 2.9"
    )
    assert "_SITEMAP_PROVEN_MIN_IMPRESSIONS" in b, "no impression threshold"


def test_proven_readmission_does_not_widen_the_gate():
    """★ The exemption must NOT be folded into _thin_excl. Keeping the gate a
    pure narrowing AND-clause is what keeps the kill switch and the collapse
    floor governing exactly one policy."""
    b = _builder()
    m = re.search(r'_thin_excl\s*=\s*"([^"]+)"', b)
    assert m and " OR " not in m.group(1).upper(), (
        "the readmission leaked into the gate expression"
    )
    assert "seo_proven_pages" not in m.group(1), "gate must not read the table"


def test_proven_readmission_respects_the_kill_switch():
    """SITEMAP_THIN_GATE_DISABLE=1 must restore the pre-08-14 sitemap exactly.
    With the gate off nothing was ever dropped, so readmitting is at best a
    no-op and at worst a second, unreviewed source of facility URLs.

    ★ Matches the guard as a STATEMENT on its own line. The first draft used
    rfind("if _thin_gate_on") and was vacuous: it matched the ternary in
    `_thin_excl = "..." if _thin_gate_on else ""` further up, so it passed
    whether or not the readmission was guarded at all. Mutation testing caught
    it — deleting the guard left the test green."""
    b = _builder()
    start = b.index("_proven_readmitted = 0")
    end = b.index("seo_proven_pages", start)
    between = [l.strip() for l in b[start:end].splitlines()]
    assert "if _thin_gate_on:" in between, (
        "the readmission is not conditioned on _thin_gate_on as a statement — "
        "it would run even with the kill switch set"
    )


def test_proven_readmission_fails_closed():
    """No table, no rows, or a query error must degrade to CURRENT behaviour
    (gate alone), never to the full thin set. The set starts empty and is only
    ever filled by a successful read."""
    b = _builder()
    i = b.index("seo_proven_pages")
    pre = b[max(0, i - 400):i]
    assert re.search(r"_proven_slugs\s*=\s*set\(\)", pre), (
        "the proven set must be initialised empty before the query, so a "
        "failure leaves it empty rather than undefined"
    )
    post = b[i:i + 1200]
    assert "except Exception" in post and "rollback" in post, (
        "a failed proven read must roll back and continue, not abort the build"
    )
    assert re.search(r"if\s+_proven_slugs\s*:", post), (
        "an empty proven set must short-circuit — `= ANY(%s)` on an empty "
        "list is both pointless and a psycopg2 trap"
    )


def test_proven_readmission_is_bounded():
    """It must not be able to readmit the whole thin set, however corrupted
    the refresh."""
    b, f = _builder(), _full()
    assert "_SITEMAP_PROVEN_CAP" in b, "the readmission is unbounded"
    m = re.search(r"_SITEMAP_PROVEN_CAP\s*=\s*_env_int\([^,]+,\s*(\d+)\)", f)
    assert m, "cap constant not defined"
    cap = int(m.group(1))
    assert 0 < cap < 13279, (
        f"cap {cap} must sit above the qualifying set (6,874 at threshold 10) "
        f"and below the 13,279-slug capacity-less pool, or a corrupted refresh "
        f"readmits the thin set wholesale"
    )


def test_the_threshold_governs_not_the_cap():
    """★★★ THE 2026-08-19 LESSON, encoded.

    Shipped as (threshold=1, cap=5000) on an estimate that the proven set was
    "~800 URLs". The real 90-day pull is 21,199, so threshold 1 selected 100%
    of the set and the CAP silently became the policy: ORDER BY impressions
    DESC LIMIT 5000 cut at 14 impressions. microsoft-mel11 — position 5.5, 3
    clicks, 10 impressions — was excluded by a number chosen as a
    corrupted-refresh backstop, not as an SEO decision.

    A threshold of 1 is not a threshold. Measured distribution: p50=5, p75=13,
    p90=38, so anything <= 2 re-admits ~82% of the set."""
    f = _full()
    m = re.search(r"_SITEMAP_PROVEN_MIN_IMPRESSIONS\s*=\s*_env_int\([^,]+,\s*(\d+)\)", f)
    assert m, "threshold constant not defined"
    thr = int(m.group(1))
    assert thr > 2, (
        f"threshold {thr} selects ~{'100' if thr <= 1 else '82'}% of the 21,199 "
        f"proven URLs — that is not a threshold, and the cap becomes the real "
        f"policy (this is exactly what shipped and had to be corrected)"
    )
    cm = re.search(r"_SITEMAP_PROVEN_CAP\s*=\s*_env_int\([^,]+,\s*(\d+)\)", f)
    assert cm and int(cm.group(1)) > 6874, (
        "the cap must sit ABOVE the qualifying set so the threshold decides "
        "readmission; a cap below it silently becomes the policy again"
    )


def test_the_two_threshold_defaults_cannot_drift():
    """main.py enforces the threshold; google_search_console.py reports it.
    A status endpoint quoting a different number than the sitemap applies is
    worse than one quoting none."""
    f = _full()
    m = re.search(r"_SITEMAP_PROVEN_MIN_IMPRESSIONS\s*=\s*_env_int\([^,]+,\s*(\d+)\)", f)
    gsc_path = os.path.join(ROOT, "google_search_console.py")
    g = open(gsc_path, encoding="utf-8").read()
    gm = re.search(r"PROVEN_MIN_IMPRESSIONS_DEFAULT\s*=\s*(\d+)", g)
    assert m and gm, "one of the two threshold defaults is missing"
    assert int(m.group(1)) == int(gm.group(1)), (
        f"threshold defaults disagree: main.py={m.group(1)} vs "
        f"google_search_console.py={gm.group(1)}"
    )


def test_env_int_cannot_zero_a_knob():
    """A misspelled or empty env value must fall back, never yield 0 — a 0
    threshold would readmit everything, a 0 cap would readmit nothing."""
    f = _full()
    i = f.index("def _env_int(")
    body = f[i:i + 700]
    assert "v > 0" in body and "default" in body, (
        "_env_int must reject non-positive values and fall back to the default"
    )
    assert "except Exception" in body, "_env_int must not raise on bad input"


def test_proven_rows_still_face_the_correctness_filters():
    """★ Readmission is from the CAPACITY gate ONLY. Junk slugs, NER/headline
    names and alternate-canonical URLs must still be excluded — a page that
    ranks but declares a different canonical is a GSC "Alternate page with
    proper canonical", which is what r-selfcanon exists to stop emitting.

    Structural proof: the readmitted rows are appended to fac_rows, so they
    flow through the same emit loop as everything else. If they were appended
    to sections['facilities'] directly they would bypass every filter.

    ★ Anchored on the block's own start and end markers, NOT a fixed character
    window. A window is a guard that silently changes meaning when unrelated
    code is inserted near it — tests/test_duplicate_facility_seo.py already
    costs this repo CI failures for exactly that reason, and the first draft of
    this test failed at 1,600 chars against a 2,226-char block."""
    b = _builder()
    start = b.index("_proven_readmitted = 0")
    end = b.index("GSC-proven facility URLs readmitted", start)
    block = b[start:end]
    assert re.search(r"fac_rows\s*=\s*list\(fac_rows\)\s*\+\s*_proven_rows", block), (
        "proven rows must be unioned into fac_rows so the junk/NER/noncanon "
        "filters in the emit loop still apply to them"
    )
    assert "sections['facilities']" not in block, (
        "proven rows are being emitted directly, bypassing the filters"
    )


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
