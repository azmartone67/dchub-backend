"""tests/test_dcpi_unlock_cta_one_wording.py — every DCPI preview names the
same unlock: Developer or Pro (owner, 2026-09-26; revisit 10-01 with
dec-mcp-paid-means-pro).

Measured live 2026-09-26: /api/v1/dcpi/leaderboard said the numeric scores
"are Pro", while /api/v1/dcpi/scores/<slug> said "A key on the Developer plan
or above opens this endpoint". Both surfaces open for Developer and above
(_DCPI_PAID_PLANS), so "Pro" alone was wrong. Prices come from the canon
(routes._stripe_links.TIER_PRICE_LABEL), never typed in routes/dcpi.py.
"""
import ast
import pathlib

from routes._stripe_links import TIER_PRICE_LABEL
from routes.dcpi import _DCPI_PAID_PLANS, dcpi_unlock_cta

SRC = (pathlib.Path(__file__).resolve().parents[1] / "routes" / "dcpi.py").read_text()


def test_names_developer_and_pro_at_canon_prices():
    cta = dcpi_unlock_cta()
    assert f"Developer ({TIER_PRICE_LABEL['developer']})" in cta
    assert f"Pro ({TIER_PRICE_LABEL['pro']})" in cta
    assert "are Pro" not in cta and "pack credits" not in cta
    assert "verdicts and the market list are free" in cta


def test_per_market_adds_the_pack_route_and_no_bare_pricing_link():
    per = dcpi_unlock_cta(pack_opens=True)
    assert f"Developer ({TIER_PRICE_LABEL['developer']}) or Pro ({TIER_PRICE_LABEL['pro']})" in per
    assert "pack credits also opens a single market" in per
    assert "dchub.cloud/pricing" not in per      # the tease's own /go/c ladder carries the link
    assert "dchub.cloud/pricing" in dcpi_unlock_cta()


def test_the_wording_matches_who_actually_unlocks():
    assert "developer" in _DCPI_PAID_PLANS and "pro" in _DCPI_PAID_PLANS


def test_no_dcpi_preview_says_pro_only_and_every_cta_uses_the_helper():
    # Score wording only: an HTML comment about ISO comparison + alerts is a
    # different feature and says nothing about the scores.
    import re
    hits = re.findall(r"(?:scores?|detail) (?:are|is) Pro\b", SRC)
    assert not hits, hits
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        # payload["_upgrade_cta"] = <value>   and   {"_upgrade_cta": <value>}
        vals = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "_upgrade_cta":
                    vals.append(node.value)
        if isinstance(node, ast.Dict):
            vals += [v for k, v in zip(node.keys, node.values)
                     if isinstance(k, ast.Constant) and k.value == "_upgrade_cta"]
        for v in vals:
            assert isinstance(v, ast.Call) and getattr(v.func, "id", "") == "dcpi_unlock_cta", \
                f"line {v.lineno}: a DCPI _upgrade_cta not built by dcpi_unlock_cta()"


def test_leaderboard_rows_say_developer_or_pro():
    assert "Numeric DCPI scores unlock with Developer or Pro" in SRC


def test_no_price_is_typed_in_the_sentence():
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "dcpi_unlock_cta")
    body = ast.get_source_segment(SRC, fn)
    assert "$49" not in body and "$99" not in body


def test_every_per_market_tease_carries_the_dcpi_sentence():
    """A function that builds a DCPI tease envelope for a SINGLE market must also
    set the per-market sentence; otherwise the generic REST wall sentence wins."""
    tree = ast.parse(SRC)
    uses = {}
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        single = [c for c in calls if getattr(c.func, "id", "") == "tease_envelope"
                  and c.args and isinstance(c.args[0], ast.Constant) and c.args[0].value == 1]
        if single:
            uses[fn.name] = any(getattr(c.func, "id", "") == "dcpi_unlock_cta"
                                and any(k.arg == "pack_opens" for k in c.keywords) for c in calls)
    assert len(uses) >= 2, uses          # per-market v1 and v2
    assert all(uses.values()), uses
