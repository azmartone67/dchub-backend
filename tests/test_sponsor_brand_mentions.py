"""tests/test_sponsor_brand_mentions.py — the $5,000 tier's only deliverable
must not over-count (P2-4, 2026-08-28).

WHY THIS IS DELICATE. /advertise prices category-exclusive Product 2 at $5,000
against $3,000 rotating, and the whole premium is a report saying "your brand
appeared in N AI answers". N goes on an invoice. Over-counting N is not a bug,
it is billing for something that did not happen, so every guard here is
pointed at over-counting specifically.

MEASURED, NOT ASSUMED. Against production on 2026-08-28, naive
`ILIKE '%vantage%'` matched 103 rows where word-boundary matching matched 1 —
a 103x overstatement, entirely "advantage" and "disadvantage". That single
number is why matching is a regex with \\m...\\M and not a LIKE.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD = ROOT / "routes" / "sponsor_mentions.py"

from routes import sponsor_mentions as sm


class _Cur:
    """Records SQL and replays canned rows in order."""
    def __init__(self, results): self.results, self.sql, self.args = list(results), [], []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=None):
        self.sql.append(sql); self.args.append(args or ())
        self._next = self.results.pop(0) if self.results else []
    def fetchone(self): return self._next[0] if self._next else None
    def fetchall(self): return self._next


class _Conn:
    def __init__(self, results): self.cur = _Cur(results)
    def cursor(self): return self.cur
    def close(self): pass


def _scan(brand, aliases=(), results=None):
    conn = _Conn(results if results is not None else [
        [(908, 11, 0)],                    # totals
        [("claude", 7), ("gpt", 3), ("chatgpt", 1)],   # by engine
        [(622, 4)],                        # prior window
        [],                                # samples
    ])
    return sm.brand_mentions(brand, aliases=aliases, conn=conn), conn


# ── matching cannot over-count ───────────────────────────────────────
def test_match_is_word_bounded_not_substring():
    """★ The 103-vs-1 case. LIKE cannot express a word boundary."""
    rx = sm._regex_for("Vantage")
    assert rx.startswith("\\m") and rx.endswith("\\M"), (
        "the brand pattern has no word boundaries — 'Vantage' will match "
        "'advantage'. Measured against prod: 103 rows vs 1."
    )
    _, conn = _scan("Vantage")
    assert not any("LIKE" in s.upper() for s in conn.cur.sql), (
        "a LIKE crept back into the scan; it cannot express a word boundary"
    )


def test_regex_metacharacters_in_a_brand_are_escaped():
    """'A+ Data' must not become a wildcard that matches 'AAAA Data'."""
    rx = sm._regex_for("A+ Data")
    assert "A\\+" in rx or "A\\\\+" in rx, (
        "the '+' in the brand is unescaped — the pattern is now a quantifier "
        "and over-counts"
    )
    assert sm._regex_for("dc.one").count("\\.") == 1, "unescaped '.' matches any char"


def test_aliases_are_one_alternation_not_a_sum():
    """An answer naming both the brand and an alias must count ONCE."""
    _, conn = _scan("Digital Realty", aliases=("DLR",))
    pat = conn.cur.args[0][0]
    assert "|" in pat, "aliases are not alternated into one pattern"
    # Exactly one boundary-open per term, and both terms present: alternation,
    # not addition. A row naming the brand AND the alias matches this pattern
    # once, so count(*) FILTER counts it once.
    assert pat.count("\\m") == 2, "expected 2 alternatives, got %r" % pat
    assert "Realty" in pat and "DLR" in pat
    # And the count must come from ONE filtered aggregate, not two summed.
    totals = [s for s in conn.cur.sql if "count(*) FILTER" in s]
    assert totals, "no filtered aggregate — counts may be summed per term"
    assert totals[0].count("FROM ai_citations") == 1


# ── the report cannot be quoted without its caveats ──────────────────
def test_a_successful_scan_always_carries_limits():
    out, _ = _scan("Equinix")
    assert out["ok"] is True
    assert out["limits"], (
        "a successful scan returned no limits; the count would be printed as "
        "if it measured the open internet"
    )
    joined = " ".join(out["limits"]).lower()
    assert "sample" in joined, "the limits never say the denominator is our own sample"
    assert "truncat" in joined, "the limits never disclose the 2000-char ceiling"


def test_the_denominator_is_always_reported():
    out, _ = _scan("Equinix")
    assert out["sampled_answers"] == 908, (
        "the numerator has no denominator; 11 mentions is unreadable without "
        "the 908 answers it came from"
    )
    assert out["prior_window"]["mentions"] == 4, "no baseline to read the count against"


def test_two_collectors_for_one_vendor_roll_up():
    """engine='gpt' (scraper) and 'chatgpt' (tracker) are one vendor.

    Reported separately, a sponsor reads one vendor as two confirmations.
    """
    out, _ = _scan("Equinix")
    assert out["by_engine"].get("openai") == 4, (
        "gpt and chatgpt did not roll up: %r" % out["by_engine"]
    )
    assert "gpt" not in out["by_engine"] and "chatgpt" not in out["by_engine"]


# ── ambiguity is surfaced, never silently counted ────────────────────
@pytest.mark.parametrize("brand", ["Switch", "Stack", "Vantage", "Aligned"])
def test_ordinary_english_brand_names_are_flagged(brand):
    """Found in production: 'STACK' matched 3 DC Hub-citing answers, all of
    them the English word. Unflagged, that is 3 invoiced citations that do
    not exist."""
    out, _ = _scan(brand)
    assert out["ambiguous"], f"{brand!r} was counted with no ambiguity warning"


def test_short_terms_are_flagged():
    out, _ = _scan("Digital Realty", aliases=("DLR",))
    assert any("DLR" in f for f in out["ambiguous"])


def test_a_distinctive_brand_is_not_flagged():
    """The flag must discriminate, or it is noise everyone learns to ignore."""
    out, _ = _scan("Equinix")
    assert out["ambiguous"] == []


# ── failure is reported, never rendered as zero ──────────────────────
def test_database_unavailable_is_not_reported_as_zero_mentions():
    """★ ok=False, not 'your brand appeared 0 times'."""
    out = sm.brand_mentions("Equinix", conn=None)
    assert out["ok"] is False
    assert out["limits"], "a failed scan produced no explanation"


def test_a_scan_error_does_not_become_a_clean_zero():
    class _Boom:
        def cursor(self): raise RuntimeError("pool gone")
        def close(self): pass
    out = sm.brand_mentions("Equinix", conn=_Boom())
    assert out["ok"] is False and out["mentions"] == 0
    assert any("scan failed" in l for l in out["limits"])


def test_empty_brand_does_not_scan():
    out = sm.brand_mentions("", conn=_Conn([]))
    assert out["ok"] is False and out["mentions"] == 0


# ── structural: the truncation ceiling is a named constant ───────────
def test_truncation_ceiling_is_declared_not_guessed():
    tree = ast.parse(MOD.read_text(encoding="utf-8"))
    val = [n.value.value for n in ast.walk(tree)
           if isinstance(n, ast.Assign)
           and getattr(n.targets[0], "id", "") == "_RESPONSE_TEXT_CEILING"
           and isinstance(n.value, ast.Constant)]
    assert val == [2000], (
        "the response_text ceiling is not declared as 2000; the undercount "
        "disclosure would quote a number nothing measured"
    )
