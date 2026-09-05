#!/usr/bin/env python3
"""tests/test_slug_freeze_short_names.py — a one- or two-character facility
name is a real name, and the pending counter must see every row the worker does.

NO NETWORK, NO DB.

TWO DEFECTS, ONE STUCK SET (measured 2026-09-05, discovered_facilities):

  1. build_canonical_slug() rejected on `len(name_slug) < 3` — the length of the
     NAME FRAGMENT. The fragment is never the slug: the return value always
     carries an 8-char stable hash and usually a provider prefix, so "SC" at
     Equinix is `equinix-sc-de6ac1f8`, 19 characters and unique. The guard
     measured a part and rejected the whole.

  2. The `pending` counter read `canonical_slug IS NULL` while the worker
     selects `canonical_slug IS NULL OR canonical_slug = ''`. A rejected row
     lands on the '' sentinel, which the worker keeps re-selecting and the
     counter cannot see — so the 6-hourly workflow printed "pending=0" on every
     tick while the rows sat there.

Together: 28 Operational facilities, first_seen 2026-03-18 to 2026-04-10, with
no URL at all for five months, and a green loop saying so. Names in the stuck
set: SC (US), L7 (UA), RZ (DE), Oi (BR), A/B/C (AT), 1A/1B/2/3/4 (CN, HK),
B4 (FR), IT, O.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routes.facility_slug_freeze import (  # noqa: E402
    build_canonical_slug,
    build_id_scheme_slug,
)

# The real stuck rows, provider/name verbatim from discovered_facilities.
STUCK = [
    ("SC", "SC"), ("Digital Telecom IX LLC", "L7"), ("", "A"), ("1A", "1A"),
    ("", "RZ"), ("中国电信", "4"), ("2", "2"), ("Air France", "B4"),
    ("1B", "1B"), ("", "B"), ("3", "3"), ("", "C"), ("O", "O"),
    ("IT", "IT"), ("Oi", "Oi"),
]


def test_every_stuck_row_now_gets_a_slug():
    for provider, name in STUCK:
        slug = build_canonical_slug(provider, name)
        assert slug, (
            "%r/%r still returns %r — this row has had no URL since March"
            % (provider, name, slug))


def test_those_slugs_are_long_unique_and_url_safe():
    """The rejection was justified by shortness. Show the result is not short."""
    slugs = set()
    for provider, name in STUCK:
        slug = build_canonical_slug(provider, name)
        assert len(slug) >= 10, (
            "%r is shorter than the 8-char hash it must contain" % slug)
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), (
            "%r is not a safe URL segment" % slug)
        assert re.search(r"-[0-9a-f]{8}$", slug), (
            "%r has lost its stable hash suffix — identity is no longer "
            "keyed on provider|name" % slug)
        slugs.add(slug)
    # 中国电信/4 folds to zhong-guo-dian-xin-4-…: a non-Latin provider must
    # still produce a readable segment rather than collapsing to the hash.
    cn = build_canonical_slug("中国电信", "4")
    assert cn.startswith("zhong-guo-dian-xin-"), cn


def test_a_nameless_row_is_still_rejected():
    """The real guard survives: a name that folds to nothing gives the URL no
    human-readable identity, and `-<hash8>` alone is not a citable page."""
    for provider, name in [("Equinix", ""), ("Equinix", None), ("", "  "),
                           ("Equinix", "!!!"), ("Equinix", "—")]:
        assert build_canonical_slug(provider, name) is None, (
            "%r/%r produced a slug with no readable name part" % (provider, name))


def test_the_change_is_a_strict_widening_no_existing_slug_moves():
    """★ canonical_slug is SET-ONCE and live URLs must never move. Every input
    that produced a slug before must produce the IDENTICAL slug now — only
    previously-rejected inputs may change, and only from None to a value.

    The old rule is reimplemented here rather than imported: importing the
    thing under test to check itself proves nothing."""
    def old_rule(provider, name):
        from routes.facility_slug_freeze import _slugify
        ns = _slugify(name) or ""
        if not ns or len(ns) < 3:
            return None
        return build_canonical_slug(provider, name)

    corpus = STUCK + [
        ("Equinix", "Equinix HK1"), ("NTT", "NTT Frankfurt"),
        ("Digital Realty", "43830 Devin Shafron Drive"),
        ("", "Spark Data Centre"), ("Unknown", "OSM DC 324441696"),
        ("CoreSite", "CoreSite LA2"), ("Data4", "Data4 Italia Campus MIL01"),
        ("Amazon Web Services", "AWS Susquehanna Nuclear Campus"),
    ]
    widened = 0
    for provider, name in corpus:
        before, after = old_rule(provider, name), build_canonical_slug(provider, name)
        if before is None:
            widened += after is not None
            continue
        assert before == after, (
            "%r/%r MOVED: %r -> %r. canonical_slug is set-once; a moved slug "
            "mints a redirect for an already-indexed URL" % (provider, name, before, after))
    assert widened >= len(STUCK), (
        "the change rescued %d rows, expected at least %d — it is not doing "
        "the job it exists for" % (widened, len(STUCK)))


def test_the_historical_id_scheme_slug_is_left_alone():
    """build_id_scheme_slug reconstructs the URL Google indexed BEFORE the
    stable-slug swap, so it may only ever describe URLs that really existed. A
    row that never had a canonical slug never had an old URL either — widening
    it too would mint alias rows for URLs nobody ever served."""
    for provider, name in STUCK:
        assert build_id_scheme_slug(provider, name, 12345) is None, (
            "%r/%r now claims a historical URL that was never served"
            % (provider, name))


def test_pending_counter_reads_the_same_rows_the_worker_selects():
    """★ The checker must not read a strict subset of the population it
    publishes a verdict on. Both statements live in the same function; compare
    them as source rather than trusting the comment above them."""
    # ★ The SQL STRING, bound by the AST — not `body[j:j + 260]`.
    # A fixed slice measures the length of what it reads, not its content: on
    # 2026-09-05 a sibling guard went red because a string it checks GREW past
    # its window while staying entirely correct. Widening the number only moves
    # the next failure out. Here the subject is a f-string built by implicit
    # concatenation, so take the whole JoinedStr/Constant and there is no window
    # to outgrow — and no need to strip comments, because a comment can never
    # be part of a string literal in the first place.
    import ast as _ast
    _p = os.path.join(ROOT, "routes", "facility_slug_freeze.py")
    _src_txt = open(_p, encoding="utf-8").read()
    counter = ""
    for _n in _ast.walk(_ast.parse(_src_txt)):
        if isinstance(_n, (_ast.JoinedStr, _ast.Constant)):
            _seg = _ast.get_source_segment(_src_txt, _n) or ""
            if "SELECT COUNT(*) FROM {table}" in _seg:
                counter = _seg
                break
    assert counter, "the pending-counter SQL literal was not found"
    assert "canonical_slug = ''" in counter, (
        "the pending counter still ignores the '' sentinel — a row the worker "
        "re-selects every run would be reported as pending=0 forever, which is "
        "exactly what happened between March and September 2026")
    assert "canonical_slug IS NULL" in counter


def test_the_sitemap_emitter_lets_a_frozen_short_slug_through():
    """★ The mechanism this whole fix depends on reaching the sitemap.

    main.py carries a SECOND copy of the short-name rejection in the sitemap
    emitter. It is harmless ONLY because it is guarded by `not _stored_first`
    — once a row has a frozen canonical_slug the guard is skipped. If that
    guard is ever tightened, the 28 rows get slugs from the freeze and are
    still dropped from the sitemap, and nothing else would catch it.

    (The same file's comment records the last time this bit: 210 live
    facilities with CJK/Cyrillic names computed an empty name_slug and were
    `continue`d out of the sitemap despite holding a good frozen URL.)
    """
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.index("len(name_slug) < 3")
    line = src[src.rindex("\n", 0, i) + 1:src.index("\n", i)]
    assert "_stored_first" in line, (
        "the sitemap emitter's short-name guard no longer checks for a stored "
        "slug first (%r) — a frozen short slug would be dropped from the "
        "sitemap even though the freeze assigned it" % line.strip())
