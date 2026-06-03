"""Honest-numbers + perf regression guard (r48, 2026-06-03).

FENCES the cleanup that turned DC Hub from inflated-and-fragile to honest-and-
stable. After a session that swept the unverified $324B M&A aggregate, inflated
DCPI market counts, and inflated "cited-by N platforms" claims out of ~115 files
across 3 repos, this test makes the gains STICK: it greps the live backend
source for the exact patterns we deliberately removed and FAILS pre-merge if any
creep back — whether re-introduced by autopilot or a human. It also locks two
cache settings whose drift previously stalled the gunicorn worker pool.

Runs automatically in pre-merge.yml (`pytest tests/`). Pure file-reading — no DB,
no network — so it's deterministic and fast.

VERIFIED CANONICAL NUMBERS (checked at the Railway origin, 2026-06-03):
  deals        = 2,032  -> say "2,000+"   (NEVER "$324B": uncomputable, value_usd sparse,
                                           the one live-computing route falls back to $85B)
  countries    = 178    -> say "170+"
  DCPI markets = 233    -> say "232"       (NEVER 280+/285/286/289: SPP-clone inflation, deduped in fix #43)
  MCP tools    = 31     (manifest incl. the Worker-only semantic_search)
  active MCP clients = Claude + Cursor     (NEVER "96+ AI platforms" / long "cited by ChatGPT,
                                           Claude, Gemini, Perplexity, Groq" lists)
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Excluded from the scan: vcs/build, stale worktrees + version archives, the test
# suite itself, and the few files that legitimately MENTION an old value in an
# explanatory (historical) comment/docstring.
_EXCLUDE_DIRS = ("/.git/", "/.claude/", "/node_modules/", "/dchub-mcp-v2.1/",
                 "/PATCHES/", "/tests/", "/__pycache__/", "/.venv/")
_EXCLUDE_FILES = (
    "frontend_stat_normalizer.py",     # dormant; docstring explains the legacy $-stat -> 2,000+ normalization
    "mcp_bug_fixes_and_new_tools.py",  # historical one-time migration script ($185B->… refs)
    "marketing_engine.py",             # docstring documents the OLD hardcoded "280+ markets" it swapped out
)


def _live_py_files():
    for dp, _dn, fn in os.walk(ROOT):
        if any(x in (dp + "/") for x in _EXCLUDE_DIRS):
            continue
        for f in fn:
            if f.endswith(".py") and f not in _EXCLUDE_FILES:
                yield os.path.join(dp, f)


def _is_comment(line: str) -> bool:
    """Skip comment-only lines — a comment that REFERENCES an old value (e.g.
    'dropped the obsolete 276 MARKETS pattern' or '// replace 280+ MARKETS with
    live count') is documentation of the fix, not a live inflated claim."""
    s = line.lstrip()
    return s.startswith(("#", "//", "*"))


def _scan(patterns):
    """Return [(relpath, lineno, line)] for any non-comment line matching a
    forbidden regex."""
    hits = []
    for path in _live_py_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if _is_comment(line):
                        continue
                    if any(p.search(line) for p in patterns):
                        hits.append((os.path.relpath(path, ROOT), i, line.strip()[:120]))
        except Exception:
            pass
    return hits


def _fmt(hits):
    return "\n".join(f"  {f}:{n}  {l}" for f, n, l in hits)


def test_no_324b_inflation():
    """The $324B M&A aggregate is unverified + uncomputable from live data — stay gone."""
    pats = [re.compile(r"\$324B"), re.compile(r"324B\+"), re.compile(r"\$324\s*billion", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced the unverified $324B M&A aggregate — replace with "
                      "'2,000+ tracked deals' (live COUNT(*)=2,032):\n" + _fmt(hits))


def test_no_inflated_platform_counts():
    """We're used by Claude + Cursor — not '96+ / 90+ AI platforms'."""
    pats = [re.compile(r"9[0-9]\+\s*(other\s+)?(AI\s+)?platforms", re.I),
            re.compile(r"\b2[0-9]\+\s*other\s+AI\s+platforms", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated AI-platform count — the verified active "
                      "clients are Claude and Cursor:\n" + _fmt(hits))


def test_no_inflated_market_counts():
    """DCPI universe is 232/233 — not 276/280+/285/286/289 (SPP-clone inflation)."""
    pats = [re.compile(r"\b(27[6-9]|28[0-9])\+?\s+(power\s+|US\s+power\s+|DCPI\s+)?markets", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated DCPI market count — the verified universe "
                      "is 232 (live /api/v1/dcpi/scores=233):\n" + _fmt(hits))


def test_perf_cache_floors():
    """Lock the two cache settings whose drift previously stalled the worker pool."""
    fails = []
    sb_path = os.path.join(ROOT, "routes/surface_brain.py")
    if os.path.exists(sb_path):
        sb = open(sb_path, encoding="utf-8").read()
        m = re.search(r"_SURFACES_TTL_S\s*=\s*([0-9.]+)", sb)
        if not m or float(m.group(1)) < 300:
            fails.append("routes/surface_brain.py _SURFACES_TTL_S must be >= 300 "
                         f"(cold recompute is ~5s; lower TTL stalls a worker); got "
                         f"{m.group(1) if m else 'MISSING'}")
    fp_path = os.path.join(ROOT, "routes/facility_profile_page.py")
    if os.path.exists(fp_path):
        fp = open(fp_path, encoding="utf-8").read()
        if "max-age=3600" not in fp:
            fails.append("routes/facility_profile_page.py must keep Cache-Control "
                         "max-age=3600 so Googlebot crawling ~12.8K enriched facility "
                         "pages hits the CF edge, not the origin.")
    assert not fails, "\n".join(fails)
