"""The public feed may not publish a negative verdict on a named market.

★ THE STATE THIS EXISTS FOR, measured live 2026-09-02 on
GET /api/v1/media/feed (cf-cache-status: MISS, served-by railway-primary):

    total items: 119   AVOID: 17
      'Westmont DCPI AVOID'    source=DCPI Engine  url=/dcpi#westmont
      'Osasco DCPI AVOID'      source=DCPI Engine  url=/dcpi#osasco
      'Elk Grove DCPI AVOID'   source=DCPI Engine  url=/dcpi#elk-grove

Six were published that evening at 21:08. The 2026-07-02 directive makes this
rail positive-only: DC Hub may publish that a named market is worth building
in; it does not publish a named, real place as one to avoid.

★ WHY THE PREVIOUS FIX DIDN'T HOLD. The rule was written into the SQL, but the
alert query exists in FOUR places — v1's fallback body, v2, v3, and the hub's
dcpi_alerts rail — and `aggregate_announcements` DELEGATES to
`aggregate_announcements_v3` (its own docstring: "the original v1 body is
preserved below for reference/rollback but unreachable"). So the copy a reader
naturally finds and fixes is NOT the one serving traffic, and the copy that
serves the rollback path keeps leaking. The live titles proved it: they carried
v3's plain `market_name || ' DCPI ' || verdict` form, not v1's emoji form,
while main.py imports v1.

★ THE INVARIANT. The rule lives at ONE choke point downstream of every query,
and each aggregator returns through it. A new alert query added later cannot
reintroduce the leak without deleting that call.

WHAT IS TESTED HOW — stated plainly rather than implied:
  · drop_unpublishable_verdicts  — BEHAVIOURAL. Real inputs, real return value.
  · "every aggregator returns through it" — STRUCTURAL (AST). These functions
    open a live psycopg2 connection and introspect information_schema, so
    driving them here would test a stub, not the code. The AST assertion is
    admissible only because the structure IS the invariant: a terminal
    `return items` is exactly an unfiltered feed. Both halves are
    mutation-proved in the PR.
"""
import ast
import io
import os

from dchub_media import drop_unpublishable_verdicts, VERDICTS_NOT_PUBLISHED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(ROOT, "dchub_media.py")


# ── behavioural: the rule itself ──────────────────────────────────────
def test_it_drops_the_plain_form_that_was_live():
    """v3's form — the one actually served on 2026-09-02."""
    items = [{"title": "Westmont DCPI AVOID", "source": "DCPI Engine"}]
    assert drop_unpublishable_verdicts(items) == []


def test_it_drops_the_emoji_form_from_the_fallback_body():
    """v1's form. A fix matching only the plain string leaks on rollback."""
    items = [{"title": "Osasco DCPI 🚨 AVOID"}]
    assert drop_unpublishable_verdicts(items) == []


def test_it_keeps_build_verdicts():
    """★ THE GREEN DIRECTION. Without this, a filter that returns [] always
    would satisfy every other assertion here — and silently empty the feed."""
    items = [{"title": "Ashburn DCPI BUILD"}]
    assert drop_unpublishable_verdicts(items) == items


def test_it_leaves_non_alert_items_alone():
    """The rail is one source among five; news and press must survive."""
    items = [{"title": "Fervo brings 396 MW online", "source": "DCD"},
             {"title": "Claude", "source": "testimonial"}]
    assert drop_unpublishable_verdicts(items) == items


def test_it_filters_a_mixed_feed_to_exactly_the_publishable_ones():
    items = [{"title": "Ashburn DCPI BUILD"},
             {"title": "Westmont DCPI AVOID"},
             {"title": "Tape still isn't dead"},
             {"title": "Elk Grove DCPI 🚨 AVOID"}]
    kept = [i["title"] for i in drop_unpublishable_verdicts(items)]
    assert kept == ["Ashburn DCPI BUILD", "Tape still isn't dead"]


def test_empty_and_missing_titles_do_not_raise():
    assert drop_unpublishable_verdicts([]) == []
    assert drop_unpublishable_verdicts(None) is None
    assert drop_unpublishable_verdicts([{"source": "x"}]) == [{"source": "x"}]


def test_the_rule_names_avoid():
    """If the constant is emptied the filter silently stops filtering."""
    assert "AVOID" in VERDICTS_NOT_PUBLISHED


# ── structural: no aggregator may return an unfiltered list ───────────
def _aggregator_defs():
    tree = ast.parse(io.open(MODULE, encoding="utf-8").read())
    return [n for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name.startswith("aggregate_announcements")]


def test_the_aggregators_are_actually_present():
    """Guards the guard: if a rename empties this list every assertion below
    passes vacuously. This repo has shipped exactly that failure before."""
    names = [f.name for f in _aggregator_defs()]
    assert len(names) >= 3, f"expected >=3 aggregators, found {names}"


def test_no_aggregator_returns_an_unfiltered_items_list():
    """★ THE STRUCTURAL INVARIANT. A bare `return items` is an unfiltered feed.
    Every terminal return must pass through the choke point."""
    offenders = []
    for fn in _aggregator_defs():
        for node in ast.walk(fn):
            if (isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "items"):
                offenders.append(f"{fn.name}:{node.lineno}")
    assert not offenders, (
        "these returns bypass drop_unpublishable_verdicts and ship the feed "
        f"unfiltered: {offenders}")
