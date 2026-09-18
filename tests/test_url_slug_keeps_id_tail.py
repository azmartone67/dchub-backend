"""slugify() truncated at 70 chars and cut off the id the resolver keys on.

★ MEASURED LIVE 2026-09-17, cache-busted, redirects followed. 662 of the
19,147 distinct published facility slugs are longer than 70 chars, and the
blind `s[:max_len]` cut destroyed the trailing 8-hex id on ALL 662:

    published  /facilities/metanet-...-staten-island-038bf517   78c   200
    builder    /facility/metanet-...-staten-island-             70c   404

★ THE ID IS THE KEY, THE NAME IS COSMETIC. Proven with a control rather than
assumed -- same shortened name, three different tails:

    /facility/vantage-...-stargate-expan-26a8567d   200  "Vantage Lighthouse Campus"
    /facility/vantage-...-stargate-expan-deadbeef   404  "DC Hub - Not found"
    /facility/vantage-...-stargate-expan            404  "DC Hub - Not found"

So the fix truncates the NAME and re-appends the id rather than raising
max_len. Raising it was the other option and is the riskier one: a longer cap
would move already-published news/press URLs that were minted under the 70
cap. Measured across all 7 sitemaps (27,697 locs), facility slugs are the ONLY
ones that exceed 70 chars -- so id-preserving truncation changes exactly the
broken class and leaves every other kind byte-identical.

Verified live after the fix -- the builder's own output now resolves:

    /facility/metanet-communications-inc-...-state-038bf517  70c  200
      "Metanet New York Data Center Staten Island - NYISO | DC Hub"
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UR = os.path.join(ROOT, "routes", "url_registry.py")
DCPI = os.path.join(ROOT, "routes", "dcpi.py")


def _load(*entry):
    """AST-extract `entry` plus every module-level name they transitively read.

    The sibling test pins an explicit _WANT set, which silently rots: adding
    _ID_TAIL to slugify left that harness NameError-ing on any input over
    max_len while all its short-slug cases still passed. Resolving free names
    from the source removes that whole failure mode.
    """
    src = open(UR, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assigns = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigns.setdefault(t.id, n)
    seen, need, queue = set(), [], list(entry)
    while queue:
        nm = queue.pop()
        if nm in seen or nm not in funcs:
            continue
        seen.add(nm)
        for sub in ast.walk(funcs[nm]):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in funcs:
                    queue.append(sub.id)
                elif sub.id in assigns and sub.id not in need:
                    need.append(sub.id)
    pieces = [ast.get_source_segment(src, assigns[a]) for a in need]
    pieces += [ast.get_source_segment(src, funcs[f]) for f in seen]
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), UR, "exec"), ns)
    return ns


_NS = _load("slugify", "build_public_url")
slugify = _NS["slugify"]
build_public_url = _NS["build_public_url"]

# Real slugs lifted from sitemap-facilities-1 / sitemap-ai-facilities-*, each
# verified live at its published /facilities/<slug> URL. Regression anchors.
PUBLISHED = [
    ("metanet-communications-inc-metanet-new-york-data-center-staten-island-038bf517", "038bf517"),
    ("faculty-of-computer-science-and-engineering-ss-cyril-and-methodius-university-in-skopje-ss-cyril-and-methodius-university-rectorate-15d4050b", "15d4050b"),
    ("johannes-gutenberg-universitt-mainz-johannes-gutenberg-universitt-mainz-a8f38937", "a8f38937"),
    ("amazon-web-services-td-cowen-aws-data-center-leasing-returning-company-scores-1gw-customer-3dd5b952", "3dd5b952"),
    ("vantage-data-centers-vantage-lighthouse-campus-stargate-expansion-26a8567d", "26a8567d"),
]


@pytest.mark.parametrize("raw,ident", PUBLISHED)
def test_over_length_slug_keeps_the_id_the_resolver_looks_up(raw, ident):
    assert len(raw) > 70, "fixture must exercise the truncation path"
    out = slugify(raw)
    assert out.endswith("-" + ident), (
        f"id {ident} lost: {out!r} -- this is the 404, the name part is cosmetic"
    )
    assert len(out) <= 70, f"{len(out)} chars > max_len: {out!r}"
    # The name must actually be spent down, not silently dropped entirely.
    assert len(out) > len(ident) + 1, f"name part vanished: {out!r}"


@pytest.mark.parametrize("raw,ident", PUBLISHED)
def test_builder_emits_the_id_end_to_end(raw, ident):
    url = build_public_url("facility", raw)
    slug = url.rsplit("/", 1)[-1]
    assert url.startswith("https://dchub.cloud/facility/")
    assert slug.endswith("-" + ident) and len(slug) <= 70, url


def test_slug_without_an_id_tail_is_byte_identical_to_the_old_cut():
    """No published news/press/market slug exceeds 70, and none may start to.

    This is the blast-radius guard: if the id branch ever leaks into ordinary
    text slugs it would move URLs that are already indexed.
    """
    raw = ("schneider-electric-and-nvidia-announce-a-joint-reference-design-for-"
           "liquid-cooled-ai-factories-in-europe")
    assert len(raw) > 70
    assert slugify(raw) == raw[:70]


def test_a_hexlike_word_shorter_than_eight_chars_is_not_treated_as_an_id():
    raw = "a" * 80 + "-faced"
    assert slugify(raw) == raw[:70]


@pytest.mark.parametrize("max_len", [4, 8, 9, 10, 12, 30, 70, 140])
def test_result_never_exceeds_max_len(max_len):
    """A max_len too small to hold an id must fall back, not overflow.

    Without the `budget > 0` guard this is where it breaks: the negative slice
    keeps most of the name and returns a slug LONGER than max_len.
    """
    for raw, _ in PUBLISHED:
        out = slugify(raw, max_len=max_len)
        assert len(out) <= max_len, f"max_len={max_len} produced {len(out)}: {out!r}"
        assert out, "slugify must never return empty"


def test_short_and_clean_slugs_still_round_trip_unchanged():
    for s in ["santa-clara", "phoenix", "acme-dc-038bf517", ""]:
        expect = s or "update"
        assert slugify(s) == expect


# --- /dcpi punctuation divergence -------------------------------------------
# /dcpi/coeur-d'alene is published with a LITERAL apostrophe and answers 200;
# it is the market_power_scores key and the only apostrophe slug among the 336
# dcpi locs, so it is canonical. slugify() cannot emit it -- it folds every
# non-alphanumeric run to "-" -- so the builder emits /dcpi/coeur-d-alene,
# which answered 404 (hard 404, "<h1>Market not found">, not a soft one).
# The route now folds the STORED slug the same way on its would-be-404 path
# and 301s to the canonical. This guards that the two folds cannot drift apart.
_SQL_FOLD = re.compile(
    r"regexp_replace\(lower\(market_slug\),\s*'([^']+)',\s*'([^']*)',\s*'g'\)", re.S)


def test_the_dcpi_fallback_folds_exactly_the_way_slugify_does():
    src = open(DCPI, encoding="utf-8").read()
    m = _SQL_FOLD.search(src)
    assert m, "the punctuation-insensitive /dcpi fallback query is gone"
    cls, repl = m.group(1), m.group(2)
    stored = "coeur-d'alene"
    folded = re.sub(cls, repl, stored.lower()).strip(repl or "-")
    assert folded == slugify(stored), (
        f"SQL folds {stored!r} to {folded!r} but slugify gives {slugify(stored)!r} -- "
        "a builder-emitted /dcpi link would 404"
    )
    assert folded == "coeur-d-alene"


def test_the_dcpi_fallback_runs_before_the_404_is_returned():
    """Ordering is the whole point: after the Response it is dead code."""
    src = open(DCPI, encoding="utf-8").read()
    fallback = _SQL_FOLD.search(src)
    not_found = src.index('f"<h1>Market not found: {slug}</h1>"')
    assert fallback.start() < not_found, "fallback must precede the 404 response"
    # and it must be reachable only once the exact lookup missed
    branch = src.rindex("if not s:", 0, fallback.start())
    assert branch < fallback.start()
