"""tests/test_brief_seed_lists_single_source.py — the sitemap may not advertise
a brief the router will not serve (2026-09-04).

WHAT HAPPENED. Of the 43 /brief URLs in the published sitemap, 41 returned 200
and two returned 404:

    /hyperscalers/softbank/brief        404
    /operators/core-scientific/brief    404

softbank was REMOVED from hyperscaler_brief.SEED_HYPERSCALERS on 2026-06-06,
with a good reason recorded right above the tuple ("an INVESTOR in
hyperscalers, not an operator"). The route stopped serving it that day. Three
OTHER copies of the same list did not move: main.py's sitemap builder and the
two exception-fallbacks in crawler_scheduler.py. Google was handed the dead URL
for three months, and the pre-warm cron would have warmed a 404.

main.py's own comment asked for exactly the right thing — "Seed lists kept in
lock-step with ... operator_brief.SEED_OPERATORS / hyperscaler_brief
.SEED_HYPERSCALERS" — but asking is not enforcing. The sitemap now IMPORTS
those tuples, and this test fails if a fourth copy appears or the fallbacks
drift again.

★ WHY A TEST AND NOT JUST THE IMPORT. The import fixes today's drift. It does
not stop someone re-adding a literal list next to it, which is how three copies
appeared in the first place.

Run:  python3 -m pytest tests/test_brief_seed_lists_single_source.py -v
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in text.split("\n"))


def _seed_tuple(rel: str, name: str) -> tuple:
    """Read a module-level tuple of string literals without importing the
    module (these route modules pull in Flask, DB pools and blueprints).

    ★ Comments are stripped FIRST. The first version cut the segment at the
    next ")" after the tuple opened, and the prose documenting why a slug was
    retired contains parentheses — so it truncated the tuple mid-way and
    reported airtrunk and iron-mountain as drift. A parser that stops at the
    first bracket in a comment invents defects."""
    src = _strip_comments(open(os.path.join(REPO, rel), encoding="utf-8").read())
    i = src.index(f"{name} = (")
    seg = src[i:src.index(")", i)]
    return tuple(re.findall(r'"([a-z0-9-]+)"', seg))


def test_sitemap_does_not_hardcode_brief_seed_slugs():
    """main.py must import the tuples, not restate them."""
    src = open(os.path.join(REPO, "main.py"), encoding="utf-8").read()
    # Strip comments — this fix documents the retired slugs in prose, and the
    # write-up must not read as a re-introduction.
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))

    assert "from routes.operator_brief import SEED_OPERATORS" in code, (
        "main.py's sitemap must import operator_brief.SEED_OPERATORS rather "
        "than restate it — a hand-kept second copy is how "
        "/operators/core-scientific/brief stayed in the sitemap."
    )
    assert "from routes.hyperscaler_brief import SEED_HYPERSCALERS" in code, (
        "main.py's sitemap must import hyperscaler_brief.SEED_HYPERSCALERS."
    )
    # The retired slugs must not reappear as sitemap literals.
    for dead in ("softbank", "core-scientific"):
        for m in re.finditer(re.escape(dead), code):
            line = code[:m.start()].count("\n") + 1
            ctx = code.split("\n")[line - 1]
            assert "brief" not in ctx.lower(), (
                f"main.py:{line} re-introduces the retired brief slug "
                f"'{dead}': {ctx.strip()[:80]}"
            )


def test_prewarm_fallbacks_match_the_seed_tuples():
    """crawler_scheduler's exception-fallbacks are the copies that drifted."""
    ops = _seed_tuple("routes/operator_brief.py", "SEED_OPERATORS")
    hyp = _seed_tuple("routes/hyperscaler_brief.py", "SEED_HYPERSCALERS")

    src = open(os.path.join(REPO, "crawler_scheduler.py"), encoding="utf-8").read()
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))

    for name, truth in (("SEED_OPERATORS", ops), ("SEED_HYPERSCALERS", hyp)):
        for m in re.finditer(rf"{name} = \(", code):
            seg = code[m.start():code.index(")", m.start())]
            fallback = tuple(re.findall(r'"([a-z0-9-]+)"', seg))
            if not fallback:
                continue  # not a literal assignment
            extra = set(fallback) - set(truth)
            assert not extra, (
                f"crawler_scheduler.py's {name} fallback carries "
                f"{sorted(extra)}, which routes/*_brief.py no longer serves. "
                f"The pre-warm cron would spend a request caching a 404."
            )


def _load_market_brief():
    """routes/market_brief.py loaded from disk. It is imported for real here,
    because the claim under test is about what its OWN canonicaliser decides —
    and a module some other test parked in sys.modules must not answer for it."""
    import importlib.util
    path = os.path.join(REPO, "routes", "market_brief.py")
    spec = importlib.util.spec_from_file_location("_brief_seed_market_brief", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_canonical") and hasattr(mod, "SEED_MARKETS"), (
        "loaded something that is not routes/market_brief.py")
    return mod


def _literal_slugs(code: str, assign: str) -> tuple:
    """The quoted slugs of `<assign> = ( ... )` in comment-stripped source."""
    i = code.index(assign)
    seg = code[i:code.index(")", i)]
    return tuple(re.findall(r"""['"]([a-z0-9-]+)['"]""", seg))


def test_sitemap_market_briefs_iterate_the_seed_tuple():
    """★2026-09-11 — the MARKET brief list in main.py was still a literal, and
    three of its fifteen slugs were aliases the brief route 301s
    (northern-virginia -> ashburn, silicon-valley -> santa-clara, portland ->
    portland-or, measured live). The loop that builds /markets/<slug>/brief
    must iterate the name imported from routes.market_brief, not a tuple typed
    beside it. Read from the AST, so a comment quoting the old list can neither
    satisfy nor fail this."""
    import ast
    tree = ast.parse(open(os.path.join(REPO, "main.py"), encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_build_sitemap_sections")

    def _is_market_brief_path(node):
        if not isinstance(node, ast.JoinedStr):
            return False
        text = "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return text.startswith("/markets/") and text.endswith("/brief")

    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)
             and any(_is_market_brief_path(s) for s in ast.walk(n))]
    assert len(loops) == 1, (
        f"expected exactly one loop emitting /markets/<slug>/brief in "
        f"_build_sitemap_sections, found {len(loops)}")
    it = loops[0].iter
    assert isinstance(it, ast.Name), (
        f"main.py:{loops[0].lineno} iterates a {type(it).__name__}, not the "
        "imported seed tuple — a typed list is how three 301s reached the sitemap")
    imported = [a for n in ast.walk(fn)
                if isinstance(n, ast.ImportFrom) and n.module == "routes.market_brief"
                for a in n.names
                if a.name == "SEED_MARKETS" and (a.asname or a.name) == it.id]
    assert imported, (
        f"main.py:{loops[0].lineno} iterates `{it.id}`, which is not bound by "
        "`from routes.market_brief import SEED_MARKETS`")


def test_every_sitemapped_market_brief_is_served_not_redirected():
    """Each slug the sitemap, its fallback, and the pre-warm fallback can emit
    must be one the brief route SERVES: _build_brief 301s whenever
    _canonical(slug) differs from the normalised request (market_brief.py,
    `redirect_to = canonical if canonical != requested_slug`)."""
    mb = _load_market_brief()
    seeds = tuple(mb.SEED_MARKETS)
    assert len(seeds) >= 10, f"SEED_MARKETS shrank to {len(seeds)} — the scan rotted"

    main_code = _strip_comments(open(os.path.join(REPO, "main.py"), encoding="utf-8").read())
    fallback = _literal_slugs(main_code, "_mb_slugs = (")
    cron_code = _strip_comments(open(os.path.join(REPO, "crawler_scheduler.py"),
                                     encoding="utf-8").read())
    prewarm = _literal_slugs(cron_code, "SEED_MARKETS = (")
    assert fallback and prewarm, "could not read a fallback tuple — the parser rotted"

    for where, slugs in (("routes.market_brief.SEED_MARKETS", seeds),
                         ("main.py _mb_slugs fallback", fallback),
                         ("crawler_scheduler.py SEED_MARKETS fallback", prewarm)):
        redirected = {s: mb._canonical(s) for s in slugs
                      if mb._canonical(s) != mb._norm_slug(s)}
        assert not redirected, (
            f"{where} carries brief slugs the route 301s: {redirected}")
    assert not set(fallback) - set(seeds), (
        f"main.py's fallback carries {sorted(set(fallback) - set(seeds))}, "
        "which SEED_MARKETS does not")
    assert not set(prewarm) - set(seeds), (
        f"crawler_scheduler's fallback carries {sorted(set(prewarm) - set(seeds))}, "
        "which SEED_MARKETS does not")


def test_retired_slugs_are_gone_from_the_seed_tuples():
    """The two slugs whose briefs 404'd must not be back."""
    ops = _seed_tuple("routes/operator_brief.py", "SEED_OPERATORS")
    hyp = _seed_tuple("routes/hyperscaler_brief.py", "SEED_HYPERSCALERS")
    assert "core-scientific" not in ops, (
        "core-scientific is back in SEED_OPERATORS. It belongs there only once "
        "the operator data carries the provider — the brief 404s otherwise, and "
        "a seed slug is documented as hand-QA'd and pre-warmed."
    )
    assert "softbank" not in hyp, (
        "softbank is back in SEED_HYPERSCALERS. It was removed 2026-06-06 as an "
        "investor rather than an operator; the route does not serve it."
    )
