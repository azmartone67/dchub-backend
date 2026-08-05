"""The paywall A/B funnel was counting our own admin 403s (2026-08-04).

House rule: tests NEVER import main. This one reads the leaf module's source.
Nothing runs at module scope.

WHY THIS EXISTS
===============
`_enrich_4xx_with_hint` fires on any `/api/*` 401/403/429. 698 routes live under
`/api/v1/admin/`, and every one of them 403s on a wrong or absent X-Admin-Key —
an operator mistyping a key, a health probe, a scheduled tick whose key rotated.

Two things followed:

  1. the 403 came back carrying Stripe checkout links, the tier table and the
     enterprise pitch, so anyone probing an admin path was handed the price
     list;
  2. worse, `_log_ab_event()` ran BEFORE the response was returned, so every
     internal auth failure landed in `ab_funnel_log` as a blocked prospect.
     `/api/v1/admin/funnel-ab-stats` scores the A/B variants off that table —
     the conversion experiment has been counting our own probes in its
     denominator.

★ A conversion metric contaminated by internal traffic is worse than no metric:
it moves, so it looks alive. The 0.04% conversion rate this middleware was
built to improve is measured against a denominator nobody has audited.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = ("routes", "paywall_hint_middleware.py")


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _middleware_body(src):
    """Everything the middleware DOES before it logs, comments removed.

    ★ Comments stripped first on purpose: the block explaining this fix names
    `_log_ab_event()` in prose, so slicing on raw text cuts at the explanation
    instead of the call — the same match-the-docstring trap regression_lint
    documents on itself.
    """
    body = src[src.index("def _enrich_4xx_with_hint"):]
    code = "\n".join(l for l in body.splitlines()
                     if not l.lstrip().startswith("#"))
    return code[:code.index("_log_ab_event(")]


# ── write side ────────────────────────────────────────────────────────

def test_admin_paths_bail_out_before_anything_is_logged():
    """★ The ordering IS the fix. Skipping the enrichment but still logging
    would leave the metric contaminated while looking solved."""
    head = _middleware_body(_src(*MOD))
    assert "_ADMIN_PREFIXES" in head, (
        "the admin bail-out must come before _log_ab_event — otherwise the "
        "funnel keeps counting internal 403s")


def test_both_internal_prefixes_are_covered():
    from routes.paywall_hint_middleware import _ADMIN_PREFIXES
    assert "/api/v1/admin/".startswith(tuple(p.rstrip("/") for p in _ADMIN_PREFIXES)) \
        or "/api/v1/admin/" in _ADMIN_PREFIXES
    assert any(p.startswith("/api/v1/internal") for p in _ADMIN_PREFIXES)
    # a real admin path matches; a customer-facing one does not
    assert "/api/v1/admin/ddl-audit".startswith(_ADMIN_PREFIXES)
    assert not "/api/v1/facilities".startswith(_ADMIN_PREFIXES)


def test_a_genuine_paywall_hit_is_still_enriched():
    """The middleware's whole job. A guard that silenced the real case too
    would trade a contaminated metric for no metric."""
    from routes.paywall_hint_middleware import _ADMIN_PREFIXES
    for path in ("/api/v1/fiber/intel", "/api/v1/mcp/tools/analyze_site",
                 "/api/v1/grid/intelligence"):
        assert not path.startswith(_ADMIN_PREFIXES), path


# ── read side ─────────────────────────────────────────────────────────

def test_every_stats_query_excludes_internal_traffic():
    """★ The write-side fix cannot repair rows already in the table. Without
    this, a stats page opened tomorrow still reports the contaminated 30-day
    window and nothing says so."""
    src = _src(*MOD)
    reads = src.count("FROM ab_funnel_log")
    excludes = src.count("_ADMIN_EXCLUDE")
    # one definition + one use per read query
    assert reads >= 3, "expected the three stats reads"
    assert excludes >= reads, (
        f"{reads} reads of ab_funnel_log but only {excludes - 1} filtered — "
        f"an unfiltered read reports the contaminated history")


def test_the_exclusion_is_defined_once():
    """★ Two copies of 'what counts as internal' would drift, and the
    direction it drifts is toward flattering numbers."""
    src = _src(*MOD)
    assert src.count("_ADMIN_EXCLUDE = ") == 1
    assert src.count("_ADMIN_PREFIXES = ") == 1


def test_the_filter_is_applied_not_the_rows_deleted():
    """Correct the reading, keep the data. A DELETE would destroy the only
    evidence of how large the contamination was."""
    src = _src(*MOD)
    assert "DELETE FROM ab_funnel_log" not in src.upper()
    assert "NOT LIKE" in src
