"""The offer on market pages: varied by data, priced from the canon.

WHY IT IS ON THIS SURFACE. Measured 2026-09-17: the citation channel carries
13,278 organic-content crawler hits in 7d (/api/v1/ai/crawler-split labels it
`crawler_and_citation`) against ~462 MCP tool calls, and it had no offer in it.
The human never loads the page — they read the answer inside the assistant — so
an offer only travels if it is IN THE TEXT THAT GETS QUOTED.

WHY MARKET PAGES ONLY (~300) and not the 22,100+ facility pages: identical
boilerplate at that scale is what thin_content_master_shell and the sitemap
keep-rule exist to catch, and de-indexing would cost the channel this is meant
to monetise.

WHAT IT REPLACED, on every market page:
    "All 19,000+ facilities ... from $49/mo"
a stale floor (canon says 22,100+) and a price that is not the ladder. BOTH
WERE LITERALS — which is the failure these tests exist to prevent recurring.

WHAT THESE PIN
  * every price is READ from the module that owns it, never typed;
  * the block VARIES on this market's own data, measured as a real number
    rather than asserted;
  * a market with no verdict/score says nothing about its verdict/score — the
    neutral page withholds measured facts on purpose and the offer must not
    smuggle one back in;
  * an unreadable price DROPS its rung and never guesses on a money surface;
  * the stale literals are gone from every market renderer in the file.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "routes" / "market_deep_dive.py").read_text(encoding="utf-8")

from routes.market_deep_dive import _market_offer_html as offer  # noqa: E402


def text(html):
    """Visible text, entities and tags removed — what a crawler quotes."""
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = t.replace("&mdash;", "—").replace("&amp;", "&")
    return re.sub(r"\s+", " ", t).strip()


MARKETS = [
    ("ashburn", "Ashburn", {"verdict": "AVOID", "dcpi_score": 17.2,
                            "facility_count": 317}),
    ("midland-odessa", "Midland-Odessa", {"verdict": "BUILD", "dcpi_score": 83,
                                          "facility_count": 41}),
    ("phoenix", "Phoenix", {"verdict": "CAUTION", "dcpi_score": 61,
                            "facility_count": 128}),
    ("columbus", "Columbus", {"verdict": "BUILD", "dcpi_score": 74,
                              "facility_count": 63}),
]


# ── prices are read, not typed ──────────────────────────────────────────
def test_both_rungs_are_read_from_the_modules_that_own_them():
    """★ The block being replaced said '$49/mo' because it was a literal, and
    it was wrong for months on ~300 pages."""
    from routes.mcp_conversion_plays import PACK10_CREDITS, PACK10_PRICE_CENTS
    import tier_registry as tr
    t = text(offer("ashburn", "Ashburn", MARKETS[0][2]))
    assert f"${PACK10_PRICE_CENTS // 100} one-time" in t
    assert f"{PACK10_CREDITS:,} API calls" in t
    assert f"Pro ${int(tr.price('pro'))}/mo" in t


def test_no_money_literal_is_typed_into_the_builder():
    """★ Scanned over EXECUTABLE CODE ONLY. The docstring and the comments
    above this function quote $49 / 19,000+ on purpose — a note explaining a
    price drift has to be able to name the price. A guard that reads prose as
    code is the same defect one level up, and it fired here first."""
    import ast
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_market_offer_html")
    nodes = fn.body
    if (nodes and isinstance(nodes[0], ast.Expr)
            and isinstance(nodes[0].value, ast.Constant)
            and isinstance(nodes[0].value.value, str)):
        nodes = nodes[1:]
    code = "\n".join(ast.get_source_segment(SRC, n) or "" for n in nodes)
    assert "PACK10_PRICE_CENTS" in code, (
        "the scanned segment is not the function body — the checks below "
        "would pass over an empty string")
    for bad in ("$49", "$99", "$10", "1,000 API calls", "19,000"):
        assert bad not in code, f"{bad!r} is typed into the offer builder"


def test_an_unreadable_price_drops_its_rung_and_never_guesses(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    t = text(offer("ashburn", "Ashburn", MARKETS[0][2]))
    assert "one-time" not in t, "the pack rung survived an unreadable price"
    assert "Pro $" in t, "the readable rung went with it"


def test_with_no_price_readable_it_says_paid_plan_not_a_number(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    monkeypatch.setitem(sys.modules, "tier_registry", None)
    t = text(offer("ashburn", "Ashburn", MARKETS[0][2]))
    assert "paid DC Hub plan" in t
    assert "$" not in t, "a price was guessed on a money surface"


# ── the variation is real, and measured ─────────────────────────────────
def test_no_two_markets_produce_the_same_block():
    seen = {}
    for slug, name, st in MARKETS:
        t = text(offer(slug, name, st))
        assert t not in seen, f"{name} is byte-identical to {seen.get(t)}"
        seen[t] = name


def test_the_lead_sentence_is_mostly_unshared_between_markets():
    """★ MEASURED, NOT ASSERTED. 'Keep variation real' has to be a number, or
    it is a claim about a template. The first sentence is the part a summariser
    is most likely to carry, so it is the part that must differ."""
    leads = [text(offer(s, n, st)).split(". ")[0] for s, n, st in MARKETS]
    for i, a in enumerate(leads):
        for b in leads[i + 1:]:
            wa, wb = set(a.split()), set(b.split())
            shared = len(wa & wb) / max(len(wa | wb), 1)
            assert shared < 0.65, (
                f"lead sentences are {shared:.0%} shared:\n  {a}\n  {b}")


def test_each_block_carries_that_markets_own_numbers():
    for slug, name, st in MARKETS:
        t = text(offer(slug, name, st))
        assert name in t
        assert str(st["dcpi_score"]) in t
        assert f"{st['facility_count']:,}" in t
        assert st["verdict"] in t


def test_the_lead_clause_is_chosen_by_the_verdict():
    """Different verdicts must give different reasons to buy, or the
    'variation' is just a name substitution."""
    by_verdict = {}
    for slug, name, st in MARKETS:
        by_verdict.setdefault(st["verdict"], []).append(
            text(offer(slug, name, st)).split(". ")[1])
    assert len(set(v[0] for v in by_verdict.values())) == len(by_verdict)
    # and two markets sharing a verdict share that clause — it is data-driven,
    # not random
    assert len(set(by_verdict["BUILD"])) == 1


# ── it must not invent facts a page deliberately withholds ──────────────
@pytest.mark.parametrize("st", [None, {}, {"verdict": "", "dcpi_score": None},
                                {"verdict": "AVOID", "dcpi_score": "?"}])
def test_no_verdict_or_score_means_it_claims_neither(st):
    """★ The neutral market page carries NO measured facts BY DESIGN — that is
    the point of it, for a market whose numbers are suspect. The offer must not
    smuggle one back in."""
    t = text(offer("bogota", "Bogota", st))
    assert "Bogota" in t
    # Ban the CLAIM, not the vocabulary: "per-site scores are Pro" names a
    # gated FIELD and is true of every market. "Bogota scores 61/100" is an
    # assertion about this market, and that is what must not appear.
    assert not re.search(r"scores\s+[\d?]", t), f"a score was claimed: {t[:90]}"
    for banned in ("/100", "AVOID", "BUILD", "CAUTION"):
        assert banned not in t, f"{banned!r} appeared with no data to support it"
    # it still sells
    assert "pricing" in t and "connect" in t


def test_a_missing_facility_count_drops_the_clause_rather_than_padding():
    t = text(offer("x", "Xmarket", {"verdict": "BUILD", "dcpi_score": 70}))
    assert "tracked facilities" not in t
    assert "0 tracked" not in t


# ── the surfaces ────────────────────────────────────────────────────────
def test_the_stale_literals_are_gone_from_every_market_renderer():
    """★ The old block lived in THREE renderers in this file. Replacing one and
    leaving two would have left most market pages on the wrong price."""
    assert "from $49/mo" not in SRC
    # the only surviving mention is the comment recording what was replaced
    assert SRC.count("19,000+") == 1
    i = SRC.index("19,000+")
    assert "The old one read" in SRC[i - 260:i], (
        "a live 19,000+ literal is still rendered")


def test_all_three_renderers_call_the_builder():
    assert SRC.count("_market_offer_html(") == 4   # 1 def + 3 call sites


def test_it_points_at_both_doors_with_attribution():
    t_html = offer("ashburn", "Ashburn", MARKETS[0][2])
    assert 'href="/pricing?ref=market-deep-dive&amp;tool=ashburn"' in t_html
    assert 'href="/connect?ref=market-deep-dive"' in t_html


def test_it_names_the_fields_the_gate_actually_gates():
    """The copy and the product must agree — these are the exact fields
    mcp#456 withholds from a free caller."""
    t = text(offer("ashburn", "Ashburn", MARKETS[0][2]))
    for field in ("MW headroom", "months-to-power", "queue IDs",
                  "fiber carriers", "per-site scores"):
        assert field in t, field
