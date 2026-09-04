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
