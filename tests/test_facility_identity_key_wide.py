#!/usr/bin/env python3
"""identity_key_wide() — the CANDIDATE wider identity. NO NETWORK, NO DB.

★★★ THIS KEY IS NOT WIRED UP, AND ONE OF THESE TESTS IS WHAT KEEPS IT THAT WAY.

util.facility_headline.identity_key() folds case and whitespace and then
compares exact strings. routes/facility_dedup_v4.plan_group groups published
URLs on it and scripts/check_sitemap_selfcanon.py counts the groups, so
widening it RE-PARTITIONS every published facility URL and changes which pages
get a rel=canonical pointed at a twin. That is a dedup decision with an apply
window, not a refactor — see [[feedback_display_string_doubles_as_a_grouping_key]].
So the wider key ships MEASURED, TESTED and UNUSED, and
test_identity_key_wide_has_no_consumers_outside_this_test fails the moment
somebody points a consumer at it without doing the rest of the work.

WHAT IT WOULD DO (measured 2026-09-11 against the live artefact: every
/facilities/<slug> in the published sitemap, 18,809 URLs, the five identity
fields taken from the public /api/v1/exports/facilities snapshot generated
09:05Z plus /api/v1/facilities/<slug> for the 1,858 slugs that snapshot does
not cover):

    identity_key       18,701 keys · 102 groups of >=2 · 108 surplus URLs
    identity_key_wide  18,663 keys · 139 groups of >=2 · 146 surplus URLs
    38 groups fuse 76 previously-distinct keys, over 77 URLs
    all 38 have >=2 SELF-canonical members today — none is already merged
    routes.facility_dedup_v4.designators_disagree vetoes 0 of the 38
    1 of the 38 has members 2.53 km apart; 30 have no usable coordinates

THE CASES THE NARROW KEY EXISTS TO KEEP APART are asserted here directly,
because a widening is only as good as what it still refuses to merge:
the ", ST, CC" collision matrix, the five NTT exchange buildings from
[[reference_dchub_undetected_facility_dupes_0907]], AirTrunk's ２Ａ/２Ｂ/２Ｃ
halls, the SecureIT DCB1.1/DCB1.2 class, and Amazon's generically-named
IAD85/IAD75/IAD96 at Manassas.
"""
import pathlib
import re
import subprocess
import sys
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.facility_headline import (  # noqa: E402
    facility_headline, identity_key, identity_key_wide)


# ── the oracle: an INDEPENDENT re-implementation, not a call-through ───────
#
# ★ It must not call _fold_identity/_dedouble_leading. A mirror of the
#   function under test proves only that the function equals itself; see
#   [[feedback_test_the_code_not_a_mirror]]. This is written from the SPEC —
#   "NFKC, casefold, non-alphanumerics to spaces, collapse whitespace, then
#   drop one repeated leading run of at most four tokens" — using different
#   mechanics (a regex + an explicit scan) from the implementation's
#   character loop.

_NOT_ALNUM = re.compile(r"[^^\w]|_", re.UNICODE)


def oracle_fold(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    chars = []
    for ch in text:
        # \w would admit "_" and exclude some marks; spell the rule out.
        chars.append(ch if (ch.isalpha() or ch.isdigit()) else " ")
    return " ".join("".join(chars).split())


def oracle_wide(name, provider, city, state, country):
    hl = facility_headline(name, provider, city, state, country)
    out = []
    for field in ("h1", "dedup_title"):
        toks = oracle_fold(hl[field]).split()
        for width in (4, 3, 2, 1):
            if len(toks) >= 2 * width and toks[:width] == toks[width:2 * width]:
                toks = toks[width:]
                break
        out.append(" ".join(toks))
    return tuple(out)


def oracle_narrow(name, provider, city, state, country):
    """An independent re-implementation of the CURRENT identity_key()."""
    hl = facility_headline(name, provider, city, state, country)
    return tuple(re.sub(r"\s+", " ", hl[f]).strip().lower()
                 for f in ("h1", "dedup_title"))


# ── corpora ───────────────────────────────────────────────────────────────

# Pairs the wider key MUST fuse. Every one is two live, self-canonical URLs on
# dchub.cloud on 2026-09-11 (slugs in the comments).
MUST_MERGE = [
    # doubled one-token operator — telehouse-telehouse-frankfurt-{4e1e3e11,e8954610}
    (("Telehouse Telehouse Frankfurt", None, "Frankfurt", None, "DE"),
     ("Telehouse - Frankfurt", "Telehouse", "Frankfurt", None, "DE")),
    # doubled TWO-token operator — global-switch-global-switch-madrid-{29b3b29f,e1e3e3bb}
    (("Global Switch Madrid", "Global Switch", "Madrid", None, "ES"),
     ("Global Switch Global Switch Madrid", "Global Switch", "Madrid",
      None, "ES")),
    # punctuation only — flexential-{atlanta-ga-3ae10fe1,flexential-atlanta-ga-06163c7d}
    (("Flexential Atlanta, GA", "Flexential", "Atlanta", "GA", "US"),
     ("Flexential Atlanta GA", "Flexential", "Atlanta", "GA", "US")),
    # parenthesis only — microsoft-azure-uk-south-london-{b6ff87f5,9192a46a}
    (("Microsoft Azure UK South (London)", "Microsoft", "London", None, "GB"),
     ("Microsoft Azure UK South London", "Microsoft", "London", None, "GB")),
    # a bare separator — keppel-data-centres*-keppel-dc-almere-1-{3ea854b7,a59ee4eb}
    (("Keppel Data Centres | Keppel DC Almere 1",
      "Keppel Data Centres Holding Pte Ltd", "Almere", None, "NL"),
     ("Keppel Data Centres Keppel DC Almere 1", "Keppel Data Centres",
      "Almere", None, "NL")),
    # hyphen-vs-space inside the operator — 1-net-*-east
    (("1 Net 1 Net East", None, "Singapore", None, "SG"),
     ("1-Net East", "1-Net Singapore Pte Ltd", "Singapore", None, "SG")),
]

# Rows the wider key MUST keep apart. Anything that fuses two of these is a
# false merge, and each entry names a real building.
MUST_SPLIT = [
    # ── the ", ST, CC" collision matrix identity_key already separates ──
    ("Metro Data Center", "Acme", "Springfield", "IL", "US"),
    ("Metro Data Center", "Acme", "Springfield", "MO", "US"),
    ("Metro Data Center", "Acme", "Springfield", None, "CA"),
    # ── ntt-ntt-*: five GENUINELY distinct Japanese exchange buildings ──
    #    (reference_dchub_undetected_facility_dupes_0907 calls this out by
    #     name as the case the slug name-key heuristic over-counts)
    ("NTTドコモ", "NTT", "Tokyo", None, "JP"),
    ("NTT東日本 小曽木電話交換局", "NTT", "Tokyo", None, "JP"),
    ("NTT武蔵村山電話交換局", "NTT", "Tokyo", None, "JP"),
    ("NTT西日本", "NTT", "Osaka", None, "JP"),
    ("NTT Communications", "NTT", "Tokyo", None, "JP"),
    # ── AirTrunk's three halls, written with FULL-WIDTH characters ──
    ("AirTrunk ２Ａ棟", "AirTrunk", "Tokyo", None, "JP"),
    ("AirTrunk ２Ｂ棟", "AirTrunk", "Tokyo", None, "JP"),
    ("AirTrunk ２Ｃ棟", "AirTrunk", "Tokyo", None, "JP"),
    # ── co-located halls under one operator (facility_dedup_v4's own class) ──
    ("SecureIT DCB1.1", "SecureIT", "Bucharest", None, "RO"),
    ("SecureIT DCB1.2", "SecureIT", "Bucharest", None, "RO"),
    ("noris network AG ING1 ITA", "noris network AG", "Nuremberg", None, "DE"),
    ("noris network AG ING1 ITB", "noris network AG", "Nuremberg", None, "DE"),
    # ── generic operator name, three real buildings (v3's 581-group lesson) ──
    ("IAD85", "Amazon Web Services", "Manassas", "VA", "US"),
    ("IAD75", "Amazon Web Services", "Manassas", "VA", "US"),
    ("IAD96", "Amazon Web Services", "Manassas", "VA", "US"),
    # ── non-Latin names that an ASCII "punctuation" filter erases ──
    ("百度地图顺德数据中心", None, "Foshan", None, "CN"),
    ("万国数据广州南沙数据中心", None, "Guangzhou", None, "CN"),
    ("Битривер Рус", None, "Moscow", None, "RU"),
    ("Даталайн", None, "Moscow", None, "RU"),
    ("شعبة اللجان الطبية", None, "Baghdad", None, "IQ"),
    ("বাংলাদেশ টেকনোসিটি লিমিটেড", None, "Dhaka", None, "BD"),
    # ── the boundary the leading-only rule protects: a row that NAMES its
    #    city must not equal a row that has none (databank-atlanta-*) ──
    ("DataBank Atlanta", "DataBank", None, None, "US"),
    ("Atlanta", "DataBank", "Atlanta", "", "US"),
]


def partition(keyfn, rows):
    out = {}
    for r in rows:
        out.setdefault(keyfn(*r), []).append(r)
    return {k: sorted(v) for k, v in out.items()}


# ── 1. the candidate is not wired up ──────────────────────────────────────

def test_identity_key_wide_has_no_consumers_outside_this_test():
    """★★★ THE RATCHET. Widening the key re-partitions ~19k published URLs and
    re-points rel=canonical; measured 2026-09-11 it fuses 38 groups over 77
    URLs. That is an apply decision with a measurement and a rollback, and
    nothing about this module's tests passing makes it safe.

    So: `identity_key_wide` may be referenced only by the module that defines
    it and by this test. Repointing facility_dedup_v4, check_sitemap_selfcanon
    or anything else fails HERE, in the same PR that does it, with this text.
    """
    # ★ --untracked, not plain `git grep`. Mutation-checked 2026-09-11: a NEW
    #   consumer file is untracked until it is added, so the plain form let a
    #   brand-new routes/*.py importing the wide key pass clean — the exact
    #   shape of the change this guard exists to stop.
    out = subprocess.run(
        ["git", "grep", "-l", "--untracked", "identity_key_wide", "--",
         "*.py", "*.yml", "*.yaml", "*.sh"],
        cwd=str(ROOT), capture_output=True, text=True)
    # rc 1 = no matches at all, which cannot happen (this file matches).
    assert out.returncode in (0, 1), out.stderr
    found = {p for p in out.stdout.split() if p}
    allowed = {"util/facility_headline.py",
               "tests/test_facility_identity_key_wide.py"}
    assert found <= allowed, (
        f"identity_key_wide is referenced by {sorted(found - allowed)}. It is "
        "a CANDIDATE: pointing a consumer at it re-partitions every published "
        "facility URL. Measure the delta on the live corpus, get the merge "
        "sample reviewed, then delete this assertion in the SAME PR.")


def test_identity_key_itself_did_not_move():
    """A wider key added beside the narrow one must not disturb the narrow
    one. Oracle is an independent re-implementation, not a call-through."""
    for row in MUST_SPLIT + [r for pair in MUST_MERGE for r in pair]:
        assert identity_key(*row) == oracle_narrow(*row), row


# ── 2. shape ──────────────────────────────────────────────────────────────

def test_the_wide_key_has_the_same_shape_as_the_narrow_one():
    """A consumer is repointed by changing one call. If the shape drifts, the
    repoint is a silent type change instead of a compile error."""
    for row in MUST_SPLIT[:6]:
        narrow, wide = identity_key(*row), identity_key_wide(*row)
        assert isinstance(wide, tuple) and len(wide) == len(narrow) == 2
        assert all(isinstance(p, str) for p in wide)


def test_the_wide_key_reads_dedup_title_not_the_displayed_title():
    """The SERP <title> moves with copy edits; a grouping key must not. The
    second component must be the frozen `dedup_title`, folded — never the
    displayed `title`, which carries MW, status and the ISO."""
    row = ("Charlotte National Data Center", "Spectrum", "Charlotte", "NC",
           "US")
    hl = facility_headline(*row)
    assert hl["title"] != hl["dedup_title"], "fixture no longer discriminates"
    wide = identity_key_wide(*row)
    assert wide[1] == oracle_fold(hl["dedup_title"])
    assert wide[1] != oracle_fold(hl["title"])


# ── 3. the fold, against an independent oracle ────────────────────────────

@pytest.mark.parametrize(
    "row", MUST_SPLIT + [r for pair in MUST_MERGE for r in pair])
def test_the_wide_key_matches_an_independent_reimplementation(row):
    assert identity_key_wide(*row) == oracle_wide(*row)


def test_a_non_latin_name_never_folds_to_an_empty_key():
    """★★★ `[^a-z0-9]` is a LATIN filter, not a punctuation filter. Measured
    2026-09-11: with an ASCII class, 36 Chinese-, Japanese-, Russian-, Arabic-
    and Bengali-named facilities folded to "" and landed in ONE group — a
    single false merge bigger than every true merge the widening buys."""
    for row in MUST_SPLIT:
        h1, title = identity_key_wide(*row)
        assert h1.strip(), f"{row!r} folded its <h1> to an empty key"
        assert title.strip(), f"{row!r} folded its title to an empty key"


def test_full_width_characters_normalise_but_still_discriminate():
    """NFKC folds ２Ａ onto 2A so the two spellings of one hall agree — and
    the three AirTrunk halls stay three."""
    a = identity_key_wide("AirTrunk ２Ａ棟", "AirTrunk", "Tokyo", None, "JP")
    b = identity_key_wide("AirTrunk 2A棟", "AirTrunk", "Tokyo", None, "JP")
    c = identity_key_wide("AirTrunk ２Ｂ棟", "AirTrunk", "Tokyo", None, "JP")
    assert a == b
    assert a != c


# ── 4. the de-double: what it collapses, and what it must not ─────────────

def test_a_doubled_leading_operator_collapses():
    for a, b in MUST_MERGE:
        assert identity_key_wide(*a) == identity_key_wide(*b), (a, b)
        assert identity_key(*a) != identity_key(*b), (
            f"{a!r} / {b!r} already share a narrow key — this fixture no "
            "longer demonstrates anything the widening buys")


def test_capitalisation_alone_was_never_the_gap():
    """★★★ A CLAIM IN THE 2026-09-12 HANDOFF, REFUTED HERE SO IT STAYS
    REFUTED. It listed "DataBank IAD1 …" vs "Databank IAD1 …" as a residual
    the current identity cannot see. It can: identity_key() lower-cases both
    components, so a pure case difference has NEVER produced two keys, and no
    widening is needed for it. What the residual actually is — measured over
    all 18,809 published URLs — is doubled operator tokens and punctuation.
    Writing the case pair into a merge fixture would have manufactured a
    passing test for a defect that does not exist."""
    a = ("DataBank IAD1", "DataBank", "Ashburn", "VA", "US")
    b = ("Databank IAD1", "Databank", "Ashburn", "VA", "US")
    assert identity_key(*a) == identity_key(*b)
    assert identity_key_wide(*a) == identity_key_wide(*b)


def test_a_repeat_that_is_not_at_the_front_is_left_alone():
    """★★★ THE MEASURED NEAR-MISS. Collapsing a repeat ANYWHERE crosses the
    boundary between the site name and the location slot: "DataBank Atlanta —
    Atlanta, US Data Center" (city Atlanta) folds onto "DataBank Atlanta — US
    Data Center" (city NULL), making a row that names a city equal to a row
    that does not. Only a LEADING run is an operator prefix."""
    with_city = identity_key_wide("Atlanta", "DataBank", "Atlanta", "", "US")
    no_city = identity_key_wide("DataBank Atlanta", "DataBank", None, None,
                                "US")
    assert with_city[0] == no_city[0], "fixture broken: the h1s should agree"
    assert with_city != no_city, (
        "the de-double ate the city — a row with city 'Atlanta' now keys the "
        "same as a row with no city at all")


def test_only_one_repeated_run_is_collapsed():
    """A loop to a fixed point turns a three-token brand into a one-token one.
    "aa aa aa bb" is one collapse -> "aa aa bb", never "aa bb"."""
    from util.facility_headline import _dedouble_leading
    assert _dedouble_leading(["aa", "aa", "aa", "bb"]) == ["aa", "aa", "bb"]
    assert _dedouble_leading(["x", "y", "x", "y", "x", "y"]) == ["x", "y",
                                                                 "x", "y"]


def test_a_genuinely_repeated_place_word_is_still_collapsed_and_that_is_known():
    """A STATED LIMIT, pinned so it cannot regress silently into a surprise.

    The rule cannot tell a doubled brand from a name that really does start
    with a repeated word ("Walla Walla"). Measured 2026-09-11 the collapse
    fires on 471 of 18,809 live URLs and produces ZERO false merges in that
    corpus — but the risk is real for a future row, and it is the reason the
    candidate is not wired."""
    from util.facility_headline import _dedouble_leading, _fold_identity
    toks = _fold_identity("Walla Walla Data Center").split()
    assert _dedouble_leading(toks) == ["walla", "data", "center"]


# ── 5. the partition, not just the rows ───────────────────────────────────

def test_the_wide_partition_is_a_COARSENING_never_a_reshuffle():
    """★★★ THE INVARIANT THAT MAKES A WIDENING REVIEWABLE. Every wide group
    must be a union of whole narrow groups: rows that key together now must
    still key together, and a widening may only ADD members to a group. A key
    that splits an existing group is not wider, it is DIFFERENT, and the
    measured "38 groups merged" would then be hiding an unmeasured number of
    groups torn apart."""
    rows = MUST_SPLIT + [r for pair in MUST_MERGE for r in pair]
    narrow, wide = partition(identity_key, rows), partition(identity_key_wide,
                                                            rows)
    for members in narrow.values():
        keys = {identity_key_wide(*r) for r in members}
        assert len(keys) == 1, (
            f"narrow group {members!r} was SPLIT across {len(keys)} wide keys")
    assert len(wide) <= len(narrow)


def test_the_collision_cases_the_narrow_key_exists_for_survive_widening():
    """Assert the PARTITION over the whole refusal corpus, not row pairs: any
    two of these landing on one key is a false merge, and naming the pair in
    the failure is what makes it diagnosable."""
    groups = partition(identity_key_wide, MUST_SPLIT)
    collisions = {k: v for k, v in groups.items() if len(v) > 1}
    assert not collisions, (
        "the wider key FUSED buildings that are not the same building: "
        + "; ".join(f"{k[0]!r} <- {[r[0] for r in v]}"
                    for k, v in collisions.items()))
    assert len(groups) == len(MUST_SPLIT)


def test_the_ntt_exchange_buildings_stay_five_buildings():
    """The named case from reference_dchub_undetected_facility_dupes_0907:
    `ntt-ntt-*` is five genuinely distinct Japanese exchange buildings with a
    different <h1> each. An identity that folded them together would publish
    one page for five buildings — a far worse defect than the duplicate the
    widening is chasing."""
    ntt = [r for r in MUST_SPLIT if (r[1] or "") == "NTT"]
    assert len(ntt) == 5, "fixture drift"
    assert len({identity_key_wide(*r) for r in ntt}) == 5
