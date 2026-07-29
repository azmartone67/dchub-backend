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
