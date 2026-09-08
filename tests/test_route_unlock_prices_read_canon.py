"""No route may RESTATE a tier price; every one must READ tier_registry.

WHY THIS EXISTS. routes/grid_transition_radar.py, routes/deal_autopsy.py and
routes/site_selection_canvas.py each carried a literal `"pro_usd_month": 199`
inside an `unlock` block. That number survived TWO repricings — 199 -> 299
(r-reprice, 2026-06-19) and 299 -> 99 (r-price-collapse, 2026-09-05) — and was
still being served live on 2026-09-08:

    $ curl -sX POST https://dchub.cloud/mcp -d '{...tools/call grid_transition_radar}'
      "developer_usd_month":49,"pro_usd_month":199

Meanwhile /api/v1/mcp/manifest served the correct $99 from tier_registry. The
existing price guards (test_agent_surfaces_one_canon, test_canonical_counts_drift)
pin main.py's _canonical_pricing() and the manifest — they never looked inside
routes/, which is exactly where the drift lived.

WHAT IT PINS. Any dict key ending `_usd_month` anywhere under routes/ must have
a COMPUTED value (a call such as _canon_price("pro")), never a numeric literal.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ROUTES = os.path.join(os.path.dirname(__file__), "..", "routes")

# ★ FLOOR. A repo-glob assertion that matches nothing passes vacuously — it
# would go green the day someone renames the field, which is the same day the
# guard stops protecting anything. This is the number of `*_usd_month` keys
# the scan must still be able to SEE. Raise it when a surface is added; a drop
# below it fails loudly rather than silently protecting nothing.
_MIN_FIELDS_SCANNED = 6


def _usd_month_values():
    """(file, lineno, key, value-node) for every `*_usd_month` dict key."""
    found = []
    for name in sorted(os.listdir(_ROUTES)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_ROUTES, name)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and k.value.endswith("_usd_month")):
                    found.append((name, getattr(k, "lineno", 0), k.value, v))
    return found


def test_the_scan_can_still_see_the_price_fields():
    """The floor. Without this the next test is green on an empty match set."""
    found = _usd_month_values()
    assert len(found) >= _MIN_FIELDS_SCANNED, (
        "only %d `*_usd_month` keys found under routes/ (floor %d). Either the "
        "field was renamed — in which case this guard now protects nothing and "
        "must be updated — or the unlock blocks were deleted."
        % (len(found), _MIN_FIELDS_SCANNED)
    )


def test_no_route_restates_a_tier_price_as_a_literal():
    offenders = [
        "%s:%d  %s = %r" % (f, ln, key, node.value)
        for f, ln, key, node in _usd_month_values()
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
    ]
    assert not offenders, (
        "a tier price is RESTATED as a literal instead of read from "
        "tier_registry.price():\n  " + "\n  ".join(offenders) +
        "\nA restated price is a second source of truth. routes/ carried "
        "pro_usd_month: 199 through two repricings this way and served it "
        "live for three days after the price became $99."
    )


def test_the_unlock_blocks_actually_resolve_to_canon():
    """Behaviour, not shape: the values these routes emit must equal canon."""
    import tier_registry
    from routes.grid_transition_radar import _canon_price as gtr_price
    from routes.deal_autopsy import _canon_price as da_price
    from routes.site_selection_canvas import _canon_price as ssc_price

    for fn in (gtr_price, da_price, ssc_price):
        assert fn("pro") == tier_registry.price("pro")
        assert fn("developer") == tier_registry.price("developer")
