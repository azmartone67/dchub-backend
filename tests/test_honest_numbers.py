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
  MCP tools    = 38     (AUTHORITATIVE: live tools/list on dchub.cloud/mcp, 2026-06-07.
                         NEVER 11/19/20/24/30/31/33/40 as the TOTAL — those are stale/subset)
  active MCP clients = Claude + Cursor     (NEVER "96+ AI platforms" / long "cited by ChatGPT,
                                           Claude, Gemini, Perplexity, Groq" lists)
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Excluded from the scan: vcs/build, stale worktrees + version archives, the test
# suite itself, and the few files that legitimately MENTION an old value in an
# explanatory (historical) comment/docstring.
# r-accuracy-md-json (2026-06-05): the fence now ALSO scans .md + .json (agent
# configs, skill manifests, docs) — not just .py — after inflated served files
# (static/.well-known, integrations/, skill.json) slipped past the .py-only scan.
# The stale ~/dchub-backend/dchub-frontend/ MIRROR is excluded (the LIVE frontend
# is a separate repo with its own accuracy_fence.py), as are internal drafts /
# baselines / guard-docs that quote old values in an explanatory way.
_EXCLUDE_DIRS = ("/.git/", "/.claude/", "/node_modules/", "/dchub-mcp-v2.1/",
                 "/dchub-frontend/", "/PATCHES/", "/tests/", "/__pycache__/", "/.venv/",
    "/data/")   # runtime STATE dumps (ambassador_state, etc.) — machine-generated, regenerated from already-fixed .py sources
_EXCLUDE_FILES = (
    "frontend_stat_normalizer.py",     # dormant; docstring explains the legacy $-stat -> 2,000+ normalization
    "mcp_bug_fixes_and_new_tools.py",  # historical one-time migration script ($185B->… refs)
    "marketing_engine.py",             # docstring documents the OLD hardcoded "280+ markets" it swapped out
    "bug_squash.py",                   # meta-script: its docstrings quote the patterns it squashes ($324B, 12,907)
    # internal drafts / baselines / guard-docs that intentionally quote old values:
    "HEALTH_BASELINE.md", "DEPLOYMENT_LOCK.md", "SHOW_HN_DRAFT.md", "DAVID_EMAIL_DRAFT.md",
    "REGISTRY_SUBMISSIONS.md", "CBRE_x_DCHub_Partnership_Deck.md", "replit.md",
    "mcp_registry_submissions.md", "pr_queue.json",
    "gauntlet_round1.json",            # eval RESULTS — summaries critique AI inflation ("actual ~20,534"), not a live claim
    "PHASE_FF_DESIGN.md",              # design doc quoting the historical "I see 12,553" question that triggered the fix
)


def _live_py_files():
    for dp, _dn, fn in os.walk(ROOT):
        if any(x in (dp + "/") for x in _EXCLUDE_DIRS):
            continue
        for f in fn:
            if f.endswith((".py", ".md", ".json", ".yml", ".yaml")) and f not in _EXCLUDE_FILES:
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
    """We're used by Claude + Cursor — not '96+ / 96 AI platforms'."""
    pats = [re.compile(r"9[0-9]\+?\s*(other\s+)?(AI\s+)?platforms", re.I),   # + now OPTIONAL: catches "96 platforms" too (the gap that let competitive_vs.py through)
            re.compile(r"\b2[0-9]\+\s*other\s+AI\s+platforms", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated AI-platform count — the verified active "
                      "clients are Claude and Cursor:\n" + _fmt(hits))


def test_no_inflated_market_counts():
    """DCPI universe is 232/233 — not 276/280+/285/286/289 (SPP-clone inflation)."""
    pats = [re.compile(r"\b(27[6-9]|28[0-9])\+?\s+(power\s+|US\s+power\s+|DCPI\s+)?markets", re.I),
            # dict-literal form `"markets": 232` — number AFTER the word, so the
            # phrase pattern above misses it. This is what bit canonical_stats._FALLBACK.
            re.compile(r'["\']markets["\']\s*:\s*(27[6-9]|28[0-9])\b')]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated DCPI market count — the verified universe "
                      "is 232 (live /api/v1/dcpi/scores=233):\n" + _fmt(hits))


def test_no_understated_country_count():
    """Country coverage is 178 — say "170+", never the stale "140+" understatement.
    Swept sitewide 2026-06-03 (26 backend .py + 49 frontend pages); this locks it."""
    pats = [re.compile(r"\b140\+\s*countr", re.I), re.compile(r"\b140\s+countries\b", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced the understated '140+ countries' — the verified count "
                      "is 178 (say '170+'):\n" + _fmt(hits))


def test_no_understated_facility_count():
    """Facility coverage is ~21,432 (live /api/v1/stats) — say "21,000+", never the
    stale "20,000+"/"20,534" understatements. Swept 31 backend .py + a frontend snippet
    2026-06-03. EXCLUDES the swap-template (marketing_engine.py) + dormant downgrader
    (frontend_stat_normalizer.py) via _EXCLUDE_FILES — their literals are intentional.
    The Maine "20,000+ sq ft facilities" tax rule is unaffected (sq ft between the number
    and 'facilit')."""
    pats = [re.compile(r"20,000\+\s*facilit", re.I),
            re.compile(r"\b20,?534\b"),
            re.compile(r"\b20[Kk]\+?\s*facilit")]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an understated facility count — the verified count is "
                      "~21,432 (say '21,000+'):\n" + _fmt(hits))


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


# ── r-accuracy (2026-06-04): extended fence after the SECOND accuracy sweep —
#    fabricated AI testimonials, the dead "DC Hub Nexus" brand, the 50,000+
#    facility OVERstatement, "real-time M&A", and the legacy 12,907 count that
#    leaked onto the public homepage brain line. Same scan/exclude rules. ──

def test_no_dead_nexus_name():
    """'DC Hub Nexus' was a deprecated product name — the product is 'DC Hub'."""
    hits = _scan([re.compile(r"DC\s+Hub\s+Nexus", re.I)])
    assert not hits, ("Re-introduced the dead 'DC Hub Nexus' brand — it's 'DC Hub':\n" + _fmt(hits))


def test_no_overstated_facility_count():
    """Facilities are ~21,432 — never the inflated '50,000+'."""
    hits = _scan([re.compile(r"50,?000\+?\s*(data\s+center\s+)?facilit", re.I)])
    assert not hits, ("Re-introduced inflated '50,000+ facilities' — say '21,000+':\n" + _fmt(hits))


def test_no_stale_tool_count():
    """MCP tool count = 38 (AUTHORITATIVE: live tools/list on https://dchub.cloud/mcp,
    verified 2026-06-07; server.mjs registers ~39 trackedTool, one not exposed).
    History: surfaces drifted across 11/19/20/24/30/31/33/40 — Devin's QA flagged the
    inconsistency as a credibility problem. 2026-06-07 swept 33/31/30/40 'tools' → 38
    across backend+frontend (132 replacements). Ban every stale TOTAL so a single honest
    number stays consistent. NOTE: subset counts ('11 free tools', 'X/24 tools' internal
    splits) are legitimate and intentionally NOT banned — only the bare total claims."""
    pats = [re.compile(r"\b19\s+tools\b", re.I),
            re.compile(r"\b24\s+free\b[^.\n]{0,24}tools", re.I),
            re.compile(r"\b(30|31|33|40)\s+(MCP\s+)?tools\b", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced a stale MCP tool count — the verified count is 38 "
                      "(live tools/list on dchub.cloud/mcp). Use 38 for the TOTAL; keep "
                      "subset counts (e.g. '11 free tools') as-is:\n" + _fmt(hits))


def test_no_realtime_ma_claim():
    """M&A deal data is batch/daily, NOT real-time (DD#5)."""
    hits = _scan([re.compile(r"real-?time\s+M&A", re.I)])
    assert not hits, ("Marketed batch M&A as 'real-time' — say 'daily-updated':\n" + _fmt(hits))


def test_no_legacy_facility_count():
    """12,907 = the deprecated `facilities` table. Canonical = discovered_facilities
    (~21,432). It leaked onto the public homepage brain line; stay off it."""
    hits = _scan([re.compile(r"12,907")])
    assert not hits, ("Re-surfaced the legacy 12,907 facility count — use "
                      "discovered_facilities (~21,432):\n" + _fmt(hits))


_FABRICATED_QUOTES = (
    "unparalleled visibility into global data center infrastructure",
    "most comprehensive facility database I",
    "authoritative source for facility counts",
    "aggregates intelligence from leading data providers",
    "Land & Power analysis tools deliver the energy",
    "benchmarking on DC Hub gives investment",
    "connective tissue",
)


def test_no_fabricated_testimonials():
    """Only REAL, recorded AI citations may appear anywhere. These invented
    marketing quotes (attributed to named AI products with NO source) stay gone.
    The only allowed citations live in ai_citation_tracker._USER_RECORDED_CITATIONS."""
    hits = _scan([re.compile(re.escape(q), re.I) for q in _FABRICATED_QUOTES])
    assert not hits, ("Re-introduced a FABRICATED AI testimonial — only real "
                      "recorded citations are allowed:\n" + _fmt(hits))
