"""ONE announcement store — a brain-staged card can never be stranded again.

THE BUG (2026-08-01). Two announcement systems shipped the same day:

  data/platform_updates.json  -> /api/v1/platform-updates   (brain writes HERE)
  capability_announcements.py -> `platform` on /whats-new   (page read THIS)

The brain staged into the first; the page rendered the second. Four
owner-approved cards were live at one endpoint and invisible on the page they
were written for.

The second failure was quieter and worse. The page's renderer reads
`it.metric`, `it.link_href` and `it.code`. The registry it was being fed emits
`figures[]`/`cta_href`/`cta_label` instead — so even the five cards that DID
render came out with no metric tile and no call-to-action link, and nothing
anywhere was red. A card-count check would have passed; only a SHAPE check
catches it.

So the fences here are:
  1. the feed and the store serve the SAME card set (no stranding), and
  2. every served card carries the exact keys the renderer reads (no silent
     half-render).

Tests never import main.py — the real functions are pulled out of source with
`ast` and executed against stubs.
"""
import ast
import json
import logging
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PU = os.path.join(ROOT, "routes", "platform_updates.py")
GROWTH = os.path.join(ROOT, "routes", "infra_growth.py")
STORE = os.path.join(ROOT, "data", "platform_updates.json")

# Exactly the fields dchub-frontend/whats-new renderPlatform() reads off a card.
RENDERER_KEYS = ("tag", "title", "body", "code", "metric",
                 "link_href", "link_label")


def _name_of(node):
    if isinstance(node, ast.FunctionDef):
        return node.name
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None):
        return node.target.id
    return None


def _load(names):
    """Exec selected top-level nodes of routes/platform_updates.py."""
    src = open(PU, encoding="utf-8").read()
    import datetime as _dt_mod
    ns = {"os": os, "json": json, "time": time, "logging": logging,
          "_dt": _dt_mod,
          "logger": logging.getLogger("test_one_store"),
          "__file__": PU}
    wanted, found = set(names), set()
    for node in ast.parse(src).body:
        nm = _name_of(node)
        if nm in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), PU, "exec"), ns)
            found.add(nm)
    missing = wanted - found
    assert not missing, f"not found in platform_updates.py: {missing}"
    return ns


# ★ This is a WHITELIST exec, so a helper published_updates() starts calling
# must be added here or the call dies with NameError inside the fail-soft
# except — which surfaces as "ok": False rather than as a missing name.
_CORE = ("STORE_PATH", "METRIC_SOURCE_URL", "METRIC_TOKENS", "MAX_CARDS",
         "_TTL", "_cache", "_is_published", "_looks_like_bare_figure",
         "_metric_spec", "_card", "_read_store", "published_updates",
         "resolve_card_metrics", "DECISION_URL", "_age_days",
         "_withheld_entry")


def _block():
    ns = _load(_CORE)
    return ns, ns["published_updates"](force=True)


# ── 1. no card can be stranded ────────────────────────────────────────

def test_every_published_entry_reaches_the_feed():
    """The anti-stranding fence: served set == published set, by id."""
    ns, block = _block()
    doc = json.load(open(STORE, encoding="utf-8"))
    published = {e["id"] for e in doc["updates"]
                 if str(e.get("status", "")).strip().lower() == "published"}
    served = {c["id"] for c in block["cards"]}
    assert served == published, (
        "the /whats-new feed and the store disagree — stranded: "
        f"{sorted(published - served)}; unexpected: {sorted(served - published)}")
    assert len(served) >= 9, (
        f"expected the full published set, got {len(served)} — the four cards "
        "the brain staged must not vanish again")


def test_whats_new_reads_the_one_store_and_not_the_retired_registry():
    src = open(GROWTH, encoding="utf-8").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "whats_new"), None)
    assert fn is not None, "whats_new() not found"
    body = ast.get_source_segment(src, fn) or ""
    assert "published_updates" in body
    assert "capability_announcement_cards" not in body, (
        "whats_new() reads the retired second registry again — that is exactly "
        "how four approved cards went invisible")


# ── 2. the served shape must match what the page renders ──────────────

def test_every_card_carries_the_keys_the_renderer_reads():
    """A card-COUNT check passes while the page renders half a card. This is
    the shape check that catches a store swap dropping metric/link_href."""
    _, block = _block()
    assert block["cards"], "no cards to check"
    for card in block["cards"]:
        for key in RENDERER_KEYS:
            assert key in card, (
                f"card {card.get('id')!r} has no {key!r} — the page renderer "
                f"reads it, so the card would render without it")


def test_cards_expose_a_call_to_action_link():
    """The regression that had every visible card rendering with no link."""
    _, block = _block()
    linked = [c for c in block["cards"] if c.get("link_href")]
    assert len(linked) == len(block["cards"]), (
        "some cards carry no link_href: "
        f"{[c['id'] for c in block['cards'] if not c.get('link_href')]}")


# ── 3. numbers bind live, and never fake a zero ───────────────────────

def test_resolve_binds_live_values_without_poisoning_the_cache():
    ns, block = _block()
    canon = {"tools": 82, "markets": 306, "facilities": 15866,
             "deals": 1843, "countries": 178}
    out = ns["resolve_card_metrics"](block, canon)
    bound = [c for c in out["cards"]
             if (c.get("metric") or {}).get("value") is not None]
    assert bound, "no card bound a live value from canon"
    for card in bound:
        m = card["metric"]
        assert m["value"] == canon[m["token"]]
        assert m.get("source_url"), "a bound figure must ship its source_url"
        assert m.get("basis"), "a bound figure must ship its basis"
    # published_updates() hands back the CACHED block; mutating it in place
    # would serve one request's numbers to every later request.
    assert all((c.get("metric") or {}).get("value") is None
               for c in block["cards"]), "resolve_card_metrics mutated the cache"


def test_unmeasured_token_stays_null_with_a_reason_never_zero():
    ns, block = _block()
    out = ns["resolve_card_metrics"](block, {})   # canon unavailable
    for card in out["cards"]:
        m = card.get("metric") or {}
        if not m.get("token"):
            continue
        assert m.get("value") is None, "a token with no live source must be null"
        assert m.get("unmeasured_reason"), "null value must carry its reason"
        assert m.get("value") != 0, "never 0 — that is a fabricated figure"


def test_resolve_ignores_junk_and_boolean_canon_values():
    ns, block = _block()
    # A bool is an int in Python and would render as "True"; blank/None are
    # not values. Note "306" as a STRING is NOT junk — see the test below.
    for junk in ({"markets": True}, {"markets": None}, {"markets": "   "},
                 {"markets": []}):
        out = ns["resolve_card_metrics"](block, junk)
        for card in out["cards"]:
            m = card.get("metric") or {}
            if m.get("token") == "markets":
                assert m.get("value") is None, f"bound junk canon value: {junk}"


def test_binds_the_real_canon_phrase_shape_not_just_ints():
    """★ The shape /api/v1/canon/phrases ACTUALLY publishes.

    Only `tools` is a bare int. markets/facilities/deals/countries are FLOORED
    PHRASE STRINGS ("300+", "15,500+") — the citation-safe form the page renders
    verbatim. An int-only guard rejected them and then stamped "no live value
    published", which is a false claim: a value IS published. This test uses the
    live payload shape rather than tidy ints, which is exactly why the first
    version of it missed the bug.
    """
    ns, block = _block()
    canon = {"tools": 82, "markets": "300+", "facilities": "15,500+",
             "deals": "1,600+", "countries": "170+"}
    out = ns["resolve_card_metrics"](block, canon)
    bound = {}
    for card in out["cards"]:
        m = card.get("metric") or {}
        if m.get("token") in canon:
            bound[m["token"]] = m.get("value")
            assert m.get("value") is not None, (
                f"token {m['token']!r} has a published canon value "
                f"({canon[m['token']]!r}) but was left null")
            assert not m.get("unmeasured_reason"), (
                f"token {m['token']!r} bound a value yet still claims it is "
                "unmeasured")
    assert bound, "no card bound any canon-published token"
    assert bound.get("markets") == "300+" or "markets" not in bound


def test_resolve_never_raises_on_a_malformed_block():
    ns = _load(_CORE)
    for bad in ({}, {"cards": None}, {"cards": [{"metric": "not-a-dict"}]},
                {"cards": [{}]}):
        ns["resolve_card_metrics"](bad, {"markets": 306})


# ── 4. the approval gate still governs ────────────────────────────────

def test_unpublished_entries_are_still_withheld_by_default():
    ns = _load(_CORE)
    assert ns["_is_published"]({"status": "published"}) is True
    for bad in ({"status": "draft"}, {"status": ""}, {}, {"status": None},
                {"status": "Published "}, None):
        got = ns["_is_published"](bad)
        if bad == {"status": "Published "}:
            assert got is True      # trimmed + lowercased on purpose
        else:
            assert got is False, f"{bad!r} must not reach the public page"
