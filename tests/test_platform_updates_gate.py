"""Guard the /whats-new platform-announcement store and its approval gate.

WHAT WENT WRONG (measured 2026-07-29). The "New platform capabilities" cards on
https://dchub.cloud/whats-new were hardcoded HTML. They said "ranks 36 grids"
while the live scoreboard ranked 46, and "tool #73" / "All 73 tools" while
/api/v1/mcp/tools reported 81. Nothing in either repo referenced that page, so
nothing could ever have caught it.

The replacement is data/platform_updates.json rendered through
routes/platform_updates.py. Two properties have to hold forever:

  1. NOTHING AUTO-PUBLISHES. An entry is invisible unless its status is exactly
     "published" — approval is the owner merging the PR that sets it.
  2. THE STORE HOLDS NO FIGURES. A card that wants a number declares a metric
     TOKEN and the client binds the value live. An unmeasurable token stays
     null with a reason — never 0, never a literal carried over from the old
     card. That is what makes "36 grids" structurally unrepeatable.

routes/platform_updates.py imports Flask at module load and the CI unit-tests
job installs only pytest, so the pure helpers are AST-extracted and exec'd in an
isolated namespace — the house pattern. Nothing here imports main.py, and no
statement runs at module scope.
"""
import ast
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "platform_updates.py")
STORE = os.path.join(ROOT, "data", "platform_updates.json")

_WANT_FN = ("_is_published", "_looks_like_bare_figure", "_metric_spec",
            "_card", "_read_store")
_WANT_CONST = ("METRIC_TOKENS", "METRIC_SOURCE_URL", "MAX_CARDS")


def _load():
    """AST-extract the pure helpers + the token registry into one namespace.

    ★ The parse itself is asserted. An empty or mis-parsed tree yields zero
    matches and every downstream loop iterates nothing, which would let this
    whole file pass while checking absolutely nothing.
    ★ The extracted functions reference each other (_card calls the other
    three), so they share ONE namespace and each name is asserted resolvable —
    a free variable that does not resolve is a NameError at call time, or worse,
    silently untested code.
    """
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    assert tree.body, "routes/platform_updates.py parsed to an EMPTY tree"

    ns = {"json": json}
    found_fn, found_const = set(), set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FN:
            exec(compile(ast.get_source_segment(src, node), SRC, "exec"), ns)
            found_fn.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _WANT_CONST:
                    exec(compile(ast.get_source_segment(src, node), SRC, "exec"), ns)
                    found_const.add(t.id)

    missing = set(_WANT_FN) - found_fn
    assert not missing, "not extracted from platform_updates.py: %s" % sorted(missing)
    missing_c = set(_WANT_CONST) - found_const
    assert not missing_c, "constants not extracted: %s" % sorted(missing_c)
    for name in _WANT_FN:
        assert callable(ns.get(name)), "%s did not exec into a callable" % name
    return ns


# ── 1. the approval gate ─────────────────────────────────────────────────

def test_gate_is_default_invisible():
    """Anything that is not explicitly "published" is withheld."""
    ns = _load()
    is_pub = ns["_is_published"]
    for entry in ({}, {"status": None}, {"status": ""}, {"status": "draft"},
                  {"status": "ready"}, {"status": "approved"}, {"status": "publish"},
                  None, [], "published", 0):
        assert is_pub(entry) is False, "gate leaked: %r" % (entry,)


def test_gate_admits_only_the_literal_published_status():
    ns = _load()
    assert ns["_is_published"]({"status": "published"}) is True
    # whitespace/case tolerated so a hand-edited PR is not silently withheld
    assert ns["_is_published"]({"status": " Published "}) is True


def test_unapproved_entry_never_renders_even_if_perfect():
    """A complete, figure-free, well-formed card is STILL withheld without the
    merged approval. The gate runs before any content inspection."""
    ns = _load()
    entry = {"id": "x", "title": "A fine title", "body": "Prose with no figures.",
             "status": "draft"}
    card, why = ns["_card"](entry, ns["METRIC_TOKENS"])
    assert card is None
    assert "not approved" in (why or "")


# ── 2. no figures may be stored ──────────────────────────────────────────

def test_the_exact_stale_strings_are_rejected():
    """Every literal that actually went stale on the live page."""
    ns = _load()
    bare = ns["_looks_like_bare_figure"]
    for s in ("get_grid_scoreboard now ranks 36 grids",
              "All 73 tools",
              "73 live tools",
              "4,922 verified inside a 21,957 tracked frontier",
              "4,800+ verified data centers",
              "12,650+ tracked facility frontier",
              "cluster_sites_by_latency — tool #73",
              "spanning 170+ countries"):
        assert bare(s) is True, "figure slipped through: %r" % s


def test_ordinary_prose_and_identifiers_are_not_rejected():
    """Precision matters — a fence that rejects everything gets deleted."""
    ns = _load()
    bare = ns["_looks_like_bare_figure"]
    for s in ("filed US generator retirements (EIA-860M)",
              "SMF-28 fibre floors",
              "Every error now carries error_version:1",
              "a CC-BY-4.0 license and a citation-URL template",
              "ranks national and regional grids on one real-time scale",
              ""):
        assert bare(s) is False, "false positive: %r" % s


def test_a_card_with_a_figure_in_prose_is_withheld():
    ns = _load()
    entry = {"id": "x", "status": "published", "title": "Now ranks 36 grids",
             "body": "Fine body."}
    card, why = ns["_card"](entry, ns["METRIC_TOKENS"])
    assert card is None
    assert "hardcoded figure" in (why or "")


# ── 3. UNMEASURED is null + a reason, never 0 ────────────────────────────

def test_unknown_token_is_null_with_a_reason_not_zero():
    ns = _load()
    m = ns["_metric_spec"]({"token": "grid_zones_ranked", "label": "grids ranked"},
                           ns["METRIC_TOKENS"])
    assert m is not None
    assert m["value"] is None
    assert m["value"] != 0 and not isinstance(m["value"], int)
    assert m["basis"] is None
    assert "UNMEASURED" in (m["unmeasured_reason"] or "")


def test_known_token_carries_a_basis_and_a_live_source_but_no_value():
    """★ The store must never hold the number itself — only where to read it."""
    ns = _load()
    for token in ("tools", "facilities"):
        m = ns["_metric_spec"]({"token": token, "label": token}, ns["METRIC_TOKENS"])
        assert m["value"] is None, "%s: the store must not carry a value" % token
        assert m["basis"], "%s: a figure without its basis" % token
        assert m["source_url"] == ns["METRIC_SOURCE_URL"]
        assert m["unmeasured_reason"] is None


def test_grid_zones_ranked_has_no_live_source_registered():
    """Regression pin: /api/v1/grid/scoreboard answers plan_required to an
    anonymous caller, and the in-process EU list is the CONFIGURED count, not
    the returned one. Registering a resolver for this token means someone found
    a keyless returned count — re-verify it before deleting this test."""
    ns = _load()
    assert "grid_zones_ranked" not in ns["METRIC_TOKENS"]


# ── 4. the shipped store itself ──────────────────────────────────────────

def test_store_parses_and_every_published_card_renders():
    ns = _load()
    ups, err = ns["_read_store"](STORE)
    assert err is None, "store unreadable: %s" % err
    assert isinstance(ups, list) and ups, "store has no updates"
    ids = set()
    published = 0
    for e in ups:
        if not ns["_is_published"](e):
            continue
        published += 1
        card, why = ns["_card"](e, ns["METRIC_TOKENS"])
        assert card, "published entry %r withheld: %s" % (e.get("id"), why)
        assert card["id"] not in ids, "duplicate card id %r" % card["id"]
        ids.add(card["id"])
    assert published, "no approved cards — /whats-new would render empty"
    assert published <= ns["MAX_CARDS"], "more approved cards than MAX_CARDS"


def test_store_prose_carries_no_figures_at_all():
    """The whole point. If this fails, a number is about to go stale in public."""
    ns = _load()
    ups, err = ns["_read_store"](STORE)
    assert err is None
    for e in ups:
        for field in ("title", "body"):
            val = (e or {}).get(field)
            assert not ns["_looks_like_bare_figure"](val), (
                "hardcoded figure in %s.%s: %r" % (e.get("id"), field, val))


def test_missing_store_degrades_to_empty_not_a_crash():
    """Fail soft: this block is spliced into the public /api/v1/whats-new feed
    and must never be able to 500 it."""
    ns = _load()
    ups, err = ns["_read_store"](os.path.join(ROOT, "data", "no_such_store.json"))
    assert ups == []
    assert err and "not found" in err


def test_garbage_entries_are_withheld_not_rendered():
    ns = _load()
    for junk in (None, [], "published", 7, {"status": "published"},
                 {"status": "published", "id": "a"},
                 {"status": "published", "id": "a", "title": "t"}):
        card, why = ns["_card"](junk, ns["METRIC_TOKENS"])
        assert card is None, "junk rendered: %r" % (junk,)
        assert why


# ── 5. the 2026-07-29 capability cards ───────────────────────────────────
#
# Three cards staged for the owner to approve by merge: the published DCPI
# methodology, cross-layer site discovery, and the zones-vs-feeds split on the
# grid scoreboard. The tests above already hold for the store as a whole; these
# pin the specific trap each new card was one careless edit away from.
#
# ★ WHY THE GRID CARD CARRIES NO METRIC AT ALL. get_grid_scoreboard returned
#   zones_ranked=47 earlier on 2026-07-29 and 43 by 19:06 UTC the same day
#   (measured, keyless). A literal in that card would have been wrong within
#   hours — twice. There is no token to bind either, so the card ships the
#   SPLIT (zones vs independent feeds) as prose and no figure whatsoever.
#
# EXPECTED COUNTS for this section
#   patched (this change):        4 passed
#   unpatched (the three entries absent from data/platform_updates.json):
#                                 3 failed, 1 passed —
#     test_new_capability_cards_are_present_and_render          FAILS (KeyError-free
#                                   assert: id missing from the store)
#     test_new_capability_cards_declare_only_known_tokens       FAILS (same missing ids)
#     test_new_capability_card_bodies_carry_no_bare_figure      FAILS (same missing ids)
#     test_fence_still_catches_the_figures_these_cards_avoided  PASSES — it tests the
#                                   fence, not the store, and is the MUST-FAIL control
#                                   proving the other three are not vacuous.
#   whole file: 17 passed patched · 3 failed / 14 passed with the store reverted.
#
# ★ THE CONTROL EARNED ITS KEEP ON FIRST RUN. Against the fence as it shipped,
#   "126,840 substations" was NOT rejected — the noun list had grids, zones and
#   tools but never substations, feeds or sources, the exact vocabulary these
#   three cards introduce. Measured: 1 failed / 16 passed before the noun list
#   was widened in routes/platform_updates.py. A guard whose blind spot covers
#   the new copy is worse than none — it reports green while the literal ships.

_NEW_CARD_IDS = ("dcpi-methodology-machine-readable",
                 "cross-layer-site-discovery",
                 "grid-scoreboard-honest-counts")


def _new_entries(ns):
    """The three staged entries, keyed by id. Asserts each one is actually
    present — a lookup that quietly finds nothing would make every assertion
    below iterate an empty set and report green."""
    ups, err = ns["_read_store"](STORE)
    assert err is None, "store unreadable: %s" % err
    by_id = {e.get("id"): e for e in ups if isinstance(e, dict)}
    missing = [i for i in _NEW_CARD_IDS if i not in by_id]
    assert not missing, "staged capability cards absent from the store: %s" % missing
    return {i: by_id[i] for i in _NEW_CARD_IDS}


def test_new_capability_cards_are_present_and_render():
    """Approved by merge, and each one survives the loader end to end."""
    ns = _new_entries(_load())
    for cid, entry in ns.items():
        assert entry.get("announced") == "2026-07-29", cid
        assert entry.get("link", {}).get("href"), "%s: no link to the proof" % cid


def test_new_capability_cards_declare_only_known_tokens():
    """★ A card may show a number ONLY through a token with a live source.

    An unknown token is not an error — it degrades to UNMEASURED — but these
    three were written to avoid that entirely: the one figure any of them shows
    is `markets`, which /api/v1/canon/phrases publishes. Adding a token here
    that is not in METRIC_TOKENS means someone tried to show a number DC Hub
    cannot bind, and the card would render a hole where a figure was intended.
    """
    ld = _load()
    for cid, entry in _new_entries(ld).items():
        spec = entry.get("metric")
        if spec is None:
            continue                      # no figure claimed — always allowed
        token = (spec or {}).get("token")
        assert token in ld["METRIC_TOKENS"], (
            "%s declares token %r, which has no live source — either bind it in "
            "METRIC_TOKENS with a real resolver or drop the metric and publish "
            "the claim without a number" % (cid, token))
        m = ld["_metric_spec"](spec, ld["METRIC_TOKENS"])
        assert m["value"] is None, "%s: the store must not carry the number" % cid
        assert m["basis"] and m["source_url"], "%s: a figure without its basis" % cid


def test_new_capability_card_bodies_carry_no_bare_figure():
    """The whole reason this store exists, applied to the new copy."""
    ld = _load()
    for cid, entry in _new_entries(ld).items():
        for field in ("title", "body"):
            assert not ld["_looks_like_bare_figure"](entry.get(field)), (
                "hardcoded figure in %s.%s — bind it to a metric token or drop "
                "it: %r" % (cid, field, entry.get(field)))
        card, why = ld["_card"](entry, ld["METRIC_TOKENS"])
        assert card, "%s withheld: %s" % (cid, why)


def test_fence_still_catches_the_figures_these_cards_avoided():
    """MUST-FAIL CONTROL for the three tests above.

    Those tests are only meaningful if the fence they lean on actually rejects
    something. These are the sentences the new cards were rewritten to avoid —
    every one of them a figure that already moved today or would freeze.
    """
    bare = _load()["_looks_like_bare_figure"]
    for s in ("the scoreboard now ranks 46 zones",
              "47 zones ranked from 7 independent feeds",
              "ranks 43 grids right now",
              "cross-layer search across 126,840 substations",
              "scored across 317 markets",
              "81 tools live now"):
        assert bare(s) is True, "the fence would have let this ship: %r" % s
