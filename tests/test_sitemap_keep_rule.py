"""The Step 2 keep rule must never remove a URL it could not measure.

main.py's r-keep-rule narrows the ungated AI family to the gated shard plus the
URLs GSC has actually surfaced. The dangerous direction is obvious and total: a
missing, empty or STALE seo_proven_pages looks exactly like "no facility URL
earned an impression", and read that way one rebuild retires 12,091 live URLs
from the family AI crawlers discover us through.

So every test here is a refusal, and each one is mutation-checked.

★ THE SHIPPED CODE IS EXECUTED, NEVER RETYPED. The functions are sliced out of
  main.py's AST and exec'd in a controlled namespace — main.py itself opens
  database connections at import time. A retyped copy would test a program
  nobody deploys. See [[feedback_test_the_code_not_a_mirror]].
"""
import ast
import datetime
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "main.py")
_TEXT = open(SRC, encoding="utf-8").read()
_TREE = ast.parse(_TEXT)

_WANT_FN = ("_env_int", "_sitemap_entry_locs", "_proven_recent_slugs",
            "_apply_keep_rule")
_WANT_ASSIGN = ("_SITEMAP_LOC_RE", "_SITEMAP_KEEP_MIN_IMPRESSIONS",
                "_SITEMAP_KEEP_PROVEN_MAX_AGE_DAYS", "_SITEMAP_KEEP_MIN_SHARE")

TODAY = datetime.date.today()


class _Log:
    def __init__(self):
        self.lines = []

    def _add(self, level, msg, *a):
        try:
            self.lines.append(f"{level}:{msg % a if a else msg}")
        except Exception:
            self.lines.append(f"{level}:{msg}")

    def info(self, m, *a):
        self._add("info", m, *a)

    def warning(self, m, *a):
        self._add("warning", m, *a)

    def error(self, m, *a):
        self._add("error", m, *a)


class _Cur:
    """Answers the two statements the reader issues, and RAISES on anything
    else. Narrower than psycopg2, never wider: a fake that answers whatever it
    is asked is how a suite goes green on a query Postgres would reject."""

    def __init__(self, table=True, rows=(), boom=None):
        self.table, self.rows, self.boom = table, list(rows), boom
        self._out = []

    def execute(self, sql, args=None):
        if self.boom:
            raise self.boom
        s = " ".join(str(sql).split()).lower()
        if "to_regclass" in s:
            self._out = [("public.seo_proven_pages",)] if self.table else [(None,)]
        elif "from seo_proven_pages" in s:
            assert "impressions >=" in s, "the reader stopped gating impressions"
            self._out = list(self.rows)
        else:
            raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return list(self._out)


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def _load(cur=None):
    """Exec the SHIPPED keep-rule code with a controlled namespace."""
    parts, seen = [], set()
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FN:
            parts.append(ast.get_source_segment(_TEXT, node))
            seen.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in _WANT_ASSIGN:
                    parts.append(ast.get_source_segment(_TEXT, node))
                    seen.add(t.id)
    missing = (set(_WANT_FN) | set(_WANT_ASSIGN)) - seen
    assert not missing, f"main.py no longer defines {sorted(missing)}"

    conn = _Conn(cur) if cur is not None else None
    ns = {"os": os, "re": re, "logger": _Log(), "_dt": datetime.datetime,
          "get_read_db": (lambda: conn), "__name__": "main_stub"}
    exec(compile("\n\n".join(parts), SRC, "exec"), ns)
    ns["_conn"] = conn
    return ns


def _entry(slug):
    return (f'  <url><loc>https://dchub.cloud/facilities/{slug}</loc>'
            f'<lastmod>2026-09-15</lastmod></url>')


GATED = [_entry(f"g{i}-aabbccdd") for i in range(5)]
AI_ONLY = [_entry(f"a{i}-11223344") for i in range(15)]
AI = GATED + AI_ONLY
PROVEN_10 = {f"a{i}-11223344" for i in range(10)}


# ── reading the impression side ─────────────────────────────────────────

def test_a_missing_proven_table_reads_as_unusable_not_as_zero():
    # ★ rows present ON PURPOSE. With an empty fixture the EMPTY branch below
    #   returns None as well, so this test passed with the existence check
    #   deleted. Mutation-checked exactly there: the log line is what separates
    #   the two refusals. See [[feedback_mutation_masked_by_earlier_guard]].
    ns = _load(_Cur(table=False, rows=[("a0-11223344", TODAY)]))
    assert ns["_proven_recent_slugs"]() is None
    assert any("does not exist" in line for line in ns["logger"].lines)


def test_an_empty_proven_table_reads_as_unusable_not_as_zero():
    ns = _load(_Cur(rows=[]))
    assert ns["_proven_recent_slugs"]() is None
    # ★ Without the EMPTY branch, max() over no rows raises and the generic
    #   except returns None too — so the return value alone proves nothing.
    assert any("is EMPTY" in line for line in ns["logger"].lines)


def test_a_stale_proven_table_reads_as_unusable():
    """The live shape: the daily GSC refresh stops, the rows stay, and every
    slug that has fallen out of the window silently reads as zero."""
    ns = _load(_Cur(rows=[("a0-11223344", TODAY - datetime.timedelta(days=9))]))
    assert ns["_proven_recent_slugs"]() is None
    assert any("days old" in line for line in ns["logger"].lines)


def test_a_read_error_reads_as_unusable():
    ns = _load(_Cur(boom=RuntimeError("connection reset")))
    assert ns["_proven_recent_slugs"]() is None


def test_only_the_newest_refresh_counts_as_in_window():
    old = TODAY - datetime.timedelta(days=40)
    ns = _load(_Cur(rows=[("fresh-aabbccdd", TODAY),
                          ("dropped-out-aabbccdd", old)]))
    assert ns["_proven_recent_slugs"]() == {"fresh-aabbccdd"}


def test_the_connection_is_closed_on_every_path():
    ns = _load(_Cur(table=False))
    ns["_proven_recent_slugs"]()
    assert ns["_conn"].closed is True


# ── the rule ────────────────────────────────────────────────────────────

def test_capacity_thin_urls_with_no_impression_are_dropped():
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: set(PROVEN_10)
    kept, stats = ns["_apply_keep_rule"](AI, GATED)
    assert stats["applied"] is True
    assert stats["dropped"] == 5
    assert kept == GATED + AI_ONLY[:10], "order or content changed"


def test_a_gated_url_is_never_dropped_whatever_its_impressions():
    """The gated shard is what GSC and Bing read, and the ungated family must
    stay a superset of it."""
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: set(PROVEN_10)
    kept, _ = ns["_apply_keep_rule"](AI, GATED)
    assert set(GATED) <= set(kept)


def test_an_unusable_proven_set_publishes_todays_artefact():
    """★★★ The one that matters. None must never mean "nothing is proven"."""
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: None
    kept, stats = ns["_apply_keep_rule"](AI, GATED)
    assert kept == AI
    assert stats["applied"] is False and stats["dropped"] == 0
    # ★ Name the refusal. Treating None as an empty set keeps only the gated
    #   shard, which the share floor then rejects anyway — so the artefact is
    #   right and the reason is wrong, and the next reader believes the rule
    #   ran. Mutation-checked: without this the substitution passes.
    assert stats["reason"] == "proven set unusable"


def test_the_kill_switch_publishes_todays_artefact(monkeypatch):
    monkeypatch.setenv("SITEMAP_KEEP_RULE_DISABLE", "1")
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: set(PROVEN_10)
    kept, stats = ns["_apply_keep_rule"](AI, GATED)
    assert kept == AI and stats["applied"] is False


def test_dropping_most_of_the_family_is_a_lost_input_not_a_collapse():
    """An empty-ish proven read would retire nearly the whole family. That is
    an input failure and it must publish today's artefact, loudly."""
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: {"nothing-matching-11223344"}
    kept, stats = ns["_apply_keep_rule"](AI, GATED)
    assert kept == AI
    assert stats["applied"] is False
    assert any("::" not in line and "below the" in line
               for line in ns["logger"].lines)


def test_an_empty_family_is_not_a_division_by_zero():
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: set(PROVEN_10)
    kept, stats = ns["_apply_keep_rule"]([], GATED)
    assert kept == [] and stats["before"] == 0


@pytest.mark.parametrize("bad", ["", "  <url><loc></loc></url>"])
def test_an_unparseable_entry_is_dropped_rather_than_published_blind(bad):
    """A rendered entry with no <loc> cannot be matched against either set. It
    is not gated and not proven, so it goes — the alternative is publishing a
    URL the rule never evaluated."""
    ns = _load()
    ns["_proven_recent_slugs"] = lambda: set(PROVEN_10)
    kept, _ = ns["_apply_keep_rule"](AI + [bad], GATED)
    assert bad not in kept
