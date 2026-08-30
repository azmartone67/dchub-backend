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

VERIFIED CANONICAL NUMBERS — REFRESHED 2026-06-21 (growth-audit live pull from
/api/v1/stats + tools/list on dchub.cloud/mcp). The 2026-06-03 values below several
metrics had DRIFTED UP; the enforced regexes still only ban GROSS over/under-claims,
so they remain green, but treat THESE as the say-numbers going forward:
  deals        = ~1,420 -> say "1,400+"   (was 2,032/"2,000+", 3,079/"3,000+", ~4,255/"4,000+";
                                           the "4,000+" curated buyer+seller subset was ITSELF an
                                           over-claim: the AUTO id embeds the ingest date, so the
                                           4,275 rows collapse to ~1,420 DISTINCT deals (2026-07-17).
                                           Source of truth = canonical_stats.deals_phrase(), which
                                           floors the ~1,420 distinct count DOWN to "1,400+".
                                           STILL never "$324B": uncomputable, value_usd sparse)
  countries    = 178    -> say "170+"      (unchanged)
  facilities   = 21,808 -> say "21,000+"   (was 21,432; floor phrase unchanged)
  DCPI markets = ~311    -> say "300+"      (was 232→300; live /api/v1/stats markets=311,
                                           capabilities.json=317; still NEVER 340+)
  MCP tools    = SoT     (live tools/list on dchub.cloud/mcp == ai_surface_canon.PINNED
                         ["tools_advertised"] (79 as of 2026-07-20). Tool-count drift is now
                         OWNED by tests/test_canonical_counts_drift.py, derived from the SoT;
                         THIS fence no longer enforces or hard-codes a tool count (the old
                         "38"/"47" AUTHORITATIVE literals were exactly the hand-maintained
                         rot the SoT-derived drift-fence replaced).)
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
# r-accuracy-txt (2026-06-25): + .txt — served static/ai.txt carried stale JSON
# metadata (50,000 facilities / 140 countries) the .py/.md/.json scan never saw,
# and the ChatGPT integration instructions.txt understated facilities. The legacy
# Replit DEPLOY-CHECKLIST.txt (dead "DC Hub Nexus" brand) is excluded below.
# The stale ~/dchub-backend/dchub-frontend/ MIRROR is excluded (the LIVE frontend
# is a separate repo with its own accuracy_fence.py), as are internal drafts /
# baselines / guard-docs that quote old values in an explanatory way.
_EXCLUDE_DIRS = ("/.git/", "/.claude/", "/node_modules/", "/dchub-mcp-v2.1/",
                 "/dchub-frontend/", "/PATCHES/", "/tests/", "/__pycache__/", "/.venv/",
    "/data/")   # runtime STATE dumps (ambassador_state, etc.) — machine-generated, regenerated from already-fixed .py sources
_EXCLUDE_FILES = (
    "frontend_stat_normalizer.py",     # dormant; docstring explains the legacy $-stat -> 2,000+ normalization
    "marketing_engine.py",             # docstring documents the OLD hardcoded "280+ markets" it swapped out
    "bug_squash.py",                   # meta-script: its docstrings quote the patterns it squashes ($324B, 12,907)
    "wins_poster.py",                  # guard file: its _BANNED fence-self-check regexes literally quote $324B / DC Hub Nexus to BLOCK them
    "brain_investigator.py",           # guard file: its fabrication denylist + LLM prompt literally quote $324B/50,000/340+ markets/96+ platforms to FLAG them in investigation output
    "upgrade_outreach.py",             # guard file: its draft-generator LLM prompt quotes the banned figures to instruct the model to NEVER cite them in outreach
    # internal drafts / baselines / guard-docs that intentionally quote old values:
    "HEALTH_BASELINE.md", "DEPLOYMENT_LOCK.md", "SHOW_HN_DRAFT.md", "DAVID_EMAIL_DRAFT.md",
    "REGISTRY_SUBMISSIONS.md", "CBRE_x_DCHub_Partnership_Deck.md", "replit.md",
    "mcp_registry_submissions.md", "pr_queue.json",
    "gauntlet_round1.json",            # eval RESULTS — summaries critique AI inflation ("actual ~20,534"), not a live claim
    "PHASE_FF_DESIGN.md",              # design doc quoting the historical "I see 12,553" question that triggered the fix
    "DEPLOY-CHECKLIST.txt",            # legacy Replit deploy checklist — references the dead "DC Hub Nexus" brand in defunct setup steps (live deploy is Railway+CF)
)


def _live_py_files():
    # ★ The exclusions are matched against the directory's path RELATIVE to
    # ROOT, never its absolute path. They are written with surrounding slashes
    # ("/data/" must not match "metadata"), so the relative path is re-wrapped
    # in them rather than the tuple being respelled.
    #
    # Matching the ABSOLUTE path also matched the checkout's own ancestors, and
    # Claude Code puts its worktrees at ~/dchub-backend/.claude/worktrees/<name>/
    # — from there every directory matched "/.claude/" and this scan yielded 0
    # files instead of 2,046. It did not fail: with nothing to scan there is
    # nothing to flag, so the whole drift-fence went green while guarding
    # NOTHING. test_the_scan_is_not_vacuous below is what makes that loud.
    for dp, _dn, fn in os.walk(ROOT):
        rel = os.path.relpath(dp, ROOT).replace(os.sep, "/")
        probe = "/" if rel == "." else "/" + rel + "/"
        if any(x in probe for x in _EXCLUDE_DIRS):
            continue
        for f in fn:
            if f.endswith((".py", ".md", ".json", ".yml", ".yaml", ".txt")) and f not in _EXCLUDE_FILES:
                yield os.path.join(dp, f)


def _is_comment(line: str) -> bool:
    """Skip comment-only lines — a comment that REFERENCES an old value (e.g.
    'dropped the obsolete 276 MARKETS pattern' or '// replace 280+ MARKETS with
    live count') is documentation of the fix, not a live inflated claim."""
    s = line.lstrip()
    return s.startswith(("#", "//", "*"))


_SCAN_ERRORS = []   # files the scan could not read; see _scan()


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
        except Exception as e:
            # ★2026-08-30 — WAS `except Exception: pass`. An unreadable file was
            # skipped in silence, so a fence that could not read half the tree
            # still reported clean. Identical defect to the frontend's
            # accuracy_fence.py, which passed over 22 live over-claims the same
            # way. A partial scan is now recorded and asserted on below; it is
            # never a pass.
            _SCAN_ERRORS.append(f"{os.path.relpath(path, ROOT)}: {type(e).__name__}: {e}")
    return hits


def _fmt(hits):
    return "\n".join(f"  {f}:{n}  {l}" for f, n, l in hits)


def test_the_scan_is_not_vacuous():
    """★ Non-vacuity floor. Every assertion in this file is of the form "no
    forbidden pattern was found", so a scan that walks NOTHING satisfies all of
    them — the fence reports green precisely when it has stopped guarding.

    That is not hypothetical: this file's exclusions were matched against the
    ABSOLUTE path until 2026-08-19, so running the suite from a checkout under
    ~/dchub-backend/.claude/worktrees/<name>/ excluded the entire repo and every
    test below passed against 0 files. Nothing in the output said so.

    This repo already knows the shape — scripts/check_ddl_through_pool.py calls
    its own floor "refusing to report a vacuous pass" — but the drift-fence that
    OWNS the canonical counts had none.
    """
    n = sum(1 for _ in _live_py_files())
    assert n >= 500, (
        f"the honest-numbers scan sees only {n} files — it is not measuring the "
        "repo, so every assertion below is passing for the wrong reason. Check "
        "_EXCLUDE_DIRS is being matched against the path RELATIVE to ROOT."
    )


def test_no_324b_inflation():
    """The $324B M&A aggregate is unverified + uncomputable from live data — stay gone."""
    pats = [re.compile(r"\$324B"), re.compile(r"324B\+"), re.compile(r"\$324\s*billion", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced the unverified $324B M&A aggregate — replace with "
                      "'1,400+ tracked deals' (DISTINCT deduped floor ~1,420; NOT the raw "
                      "COUNT(*)~11.5K nor the stale '4,000+' row count):\n" + _fmt(hits))


def test_no_inflated_platform_counts():
    """We're used by Claude + Cursor — not '96+ / 96 AI platforms'."""
    pats = [re.compile(r"9[0-9]\+?\s*(other\s+)?(AI\s+)?platforms", re.I),   # + now OPTIONAL: catches "96 platforms" too (the gap that let competitive_vs.py through)
            re.compile(r"\b2[0-9]\+\s*other\s+AI\s+platforms", re.I)]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated AI-platform count — the verified active "
                      "clients are Claude and Cursor:\n" + _fmt(hits))


def test_no_inflated_market_counts():
    """DCPI universe is ~300 (Neon-verified 2026-06-08: COUNT(DISTINCT market_name)
    minus 3 aggregates = 300; grew from 232 via international expansion). Catch
    GROSS over-claims (340+) — the real ~300 (+ a growth buffer to 339) is fine."""
    # r73 (2026-06-08): retuned. The window was 276-399 back when 232 was the
    # believed canonical; the Neon dedup proved the real count is 300, so
    # 276-339 is now LEGITIMATE. Catch 340+ as inflation.
    # NOTE: dict-literal pattern is DOUBLE-quote only ("markets": N) — DC Hub's own
    # stats/config use JSON-style double quotes; single-quoted Python data records
    # ('markets': 400) are third-party provider attributes (e.g. Zayo's coverage),
    # NOT DC Hub's DCPI count, so they're legitimately exempt.
    pats = [re.compile(r"\b(3[4-9][0-9]|[4-9][0-9]{2})\+?\s+(power\s+|US\s+power\s+|DCPI\s+)?markets", re.I),
            re.compile(r'"markets"\s*:\s*(3[4-9][0-9]|[4-9][0-9]{2})\b')]
    hits = _scan(pats)
    assert not hits, ("Re-introduced an inflated DCPI market count — the verified universe "
                      "is ~311 (live /api/v1/stats markets=311, 2026-06-21; say '300+'):\n" + _fmt(hits))


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


# test_no_stale_tool_count REMOVED (2026-07-20): the MCP tool count is now OWNED by
# tests/test_canonical_counts_drift.py, which DERIVES it from the source of truth
# (ai_surface_canon.PINNED["tools_advertised"] == live tools/list, currently 79) rather
# than a hand-maintained literal. This fence banned a hardcoded (30|31|33|40|42|46) list
# that had ITSELF gone stale — it never caught the 72/73→79 drift — the exact
# hardcoded-count rot the SoT-derived drift-fence exists to end. Do NOT re-add a literal
# tool-count ban here; extend the drift-fence (AGENT_CODE_SURFACES / SURFACES) instead.


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


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-FABRICATION INVARIANTS (2026-06-16) — upgrade this fence from a denylist
# of PAST specific mistakes into checks that catch the CLASS. These exist because
# the freshness/breakage/placeholder detectors are blind to plausible-but-fake
# data (it returns 200, looks fresh, isn't empty), and because a regex can't tell
# a real per-facility `capacity_mw=4500` from a fabricated aggregate — only the
# patterns below (NULL→0 coercion, ISO-scale literals, dedup-blind canonical
# counts) are precise enough to gate in CI. The DYNAMIC half (cross-surface value
# consistency + floor≤live-reality) runs in the brain, which has DB access.
# ─────────────────────────────────────────────────────────────────────────────

def test_no_null_to_zero_metric_coercion():
    """`float(<metric_col> or 0)` silently turns a NULL ('not tracked') into a
    fabricated 0.0 — this is exactly how the ISO snapshot advertised a fake
    '0.0 GW data-center load' for all 7 ISOs. Metric columns that can be NULL
    must use `... if x is not None else None`, not truthiness coercion."""
    cols = ("queued_load_data_center_gw", "queued_load_dc_share_pct")
    pats = [re.compile(r"float\([^)]*" + re.escape(c) + r"[^)]*\bor\s+0\s*\)") for c in cols]
    hits = _scan(pats)
    assert not hits, ("NULL→0 coercion on a metric column fabricates a '0' where "
                      "the honest value is 'not tracked' (null). Use "
                      "`float(x) if x is not None else None`:\n" + _fmt(hits))


def test_no_hardcoded_iso_scale_aggregates():
    """An aggregate metric (total_mw/total_gw/queue total) assigned to a literal
    >= 100,000 MW (100 GW) is an ISO/national-scale number that must come from
    the live DB, never a hardcoded constant. A real per-facility capacity_mw is
    <10,000, so this threshold cleanly separates fabricated aggregates (the
    deleted /api/v1/queue/interconnection: PJM 298000, ERCOT 215000…) from
    legitimate per-record values."""
    pat = re.compile(r"['\"]?(total_mw|total_gw|queued_load_total_\w+|queue_mw)['\"]?\s*[:=]\s*([1-9]\d{5,})\b")
    hits = []
    for path in _live_py_files():
        if not path.endswith(".py"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if _is_comment(line):
                        continue
                    m = pat.search(line)
                    if m and int(m.group(2)) >= 100000:
                        hits.append((os.path.relpath(path, ROOT), i, line.strip()[:120]))
        except Exception:
            pass
    assert not hits, ("Hardcoded ISO-scale MW aggregate — must be queried live, "
                      "not fabricated as a constant:\n" + _fmt(hits))


def test_canonical_stats_is_dedup_aware():
    """The single source of truth (canonical_stats.py) must distinguish the raw
    'tracked' discovery pile from the deduped 'verified' active set, and its
    verified floor must never exceed the tracked floor. This is what prevents the
    marketed count from drifting 7x above reality again (21,461 raw vs 3,141
    active). The dynamic 'floor <= live count' check runs in the brain."""
    src = open(os.path.join(ROOT, "canonical_stats.py"), encoding="utf-8").read()
    assert "facilities_verified" in src, "canonical_stats lost the verified/active count"
    assert "COALESCE(is_duplicate,0)=0" in src, (
        "the verified count must filter the deduped fleet "
        "(COALESCE(is_duplicate,0)=0)")
    assert "COALESCE(is_duplicate,0)=0 AND merged_at IS NULL" not in src, (
        "issue #1539 regression: pairing merged_at IS NULL with the dup filter "
        "counts the drained pending queue (reads ~0), not the verified fleet — "
        "the merge pipeline stamps merged_at on every promoted fleet row")
    # 2026-07-16: the brain GATHER breakdowns (routes/brain_data_gatherer.py)
    # carried the same stale predicate and reported "verified 0 / tracked
    # 21,992" per country/provider/ISO while canonical said ~4,958. Guard the
    # SQL specs with the same #1539 check (docstrings may mention the old
    # predicate historically, so only scan the _SQL constants).
    gatherer_path = os.path.join(ROOT, "routes", "brain_data_gatherer.py")
    if os.path.exists(gatherer_path):
        gsrc = open(gatherer_path, encoding="utf-8").read()
        assert "COALESCE(is_duplicate,0)=0" in gsrc, (
            "brain_data_gatherer breakdowns must use the canonical fleet filter")
        import re as _re
        sql_blocks = _re.findall(r'_SQL = """(.*?)"""', gsrc, _re.S)
        assert sql_blocks, "brain_data_gatherer lost its pre-written _SQL specs"
        for blk in sql_blocks:
            assert "merged_at IS NULL" not in blk, (
                "issue #1539 regression in brain_data_gatherer.py: the verified "
                "predicate must NOT pair merged_at IS NULL with the dup filter — "
                "it counts the drained pending queue (verified reads 0)")
    import importlib, sys
    sys.path.insert(0, ROOT)
    cs = importlib.import_module("canonical_stats")
    fb = cs._FALLBACK
    assert fb["facilities_verified"] <= fb["facilities"], (
        f"verified floor {fb['facilities_verified']} must be <= tracked floor {fb['facilities']}")
    assert fb["countries_verified"] <= fb["countries"], (
        f"verified-country floor {fb['countries_verified']} must be <= tracked {fb['countries']}")


def test_no_field_form_or_iso_fabrication_drift():
    """JSON-FIELD-FORM drift the phrase patterns above miss — served metadata as
    "key": value (e.g. "countries": 140). Same blind spot that hid stale values in
    static/ai.txt + the backend's served dicts (handle_well_known, the ai-agents.json
    route). Also bans the "7 US + Hydro-Quebec, AESO, Nord Pool" framing: HQ/AESO/Nord
    Pool are MODELED baselines, not live ISOs. (2026-06-25; the "tools_count" field-form
    check was ceded to tests/test_canonical_counts_drift.py on 2026-07-20 — that
    SoT-derived guard owns the tool count now.)"""
    pats = [
        re.compile(r'"countries"\s*:\s*"?1[0-6][0-9]\b'),                   # understated countries <170
        re.compile(r'7 US \+ Hydro-?Qu', re.I),                             # HQ/AESO/Nord-Pool framed as live ISOs
    ]
    hits = _scan(pats)
    assert not hits, ("Field-form drift or HQ/AESO/Nord-Pool 'live ISO' fabrication — "
                      "canonical: countries 170+; HQ/AESO/Nord Pool are modeled "
                      "baselines, not live ISOs:\n" + _fmt(hits))




# ═══════════════════════════════════════════════════════════════════════════
# issue #1539, THIRD occurrence — a repo-wide RATCHET, not a per-file guard
# ═══════════════════════════════════════════════════════════════════════════
# `merged_at IS NULL AND is_duplicate = 0` on discovered_facilities is the
# PENDING-REVIEW QUEUE, not the fleet, and matches ~zero rows:
# merge_discovered_v3 stamps `merged_at = NOW()` on every clean row it promotes
# into canonical `facilities`, so clean rows always have merged_at SET and the
# intersection is empty. The fleet filter is `COALESCE(is_duplicate, 0) = 0`.
#
# The two guards above are scoped to the two FILES where this had been caught.
# It then appeared a THIRD time in routes/operators.py and zeroed a whole public
# surface: /operators served "0 tracked" under index,follow with a meta
# description reading "Live directory of 0 data center operators", while
# /api/v1/stats reported 6,432 providers. Same predicate and same symptom as the
# hyperscaler briefs (PR #1546). It survived because every previous fix was
# applied to a FILE instead of to the PREDICATE.
#
# ★ WHY A RATCHET AND NOT A BAN. 39 occurrences remain across 13 files and they
# are NOT all wrong — merge_discovered*.py and discovery_auto_approve.py use
# this predicate CORRECTLY, because finding un-promoted rows is exactly their
# job. Auditing which of the rest are fleet reads is real work and is not done
# here. So this test freezes the known set and fails on anything NEW. It proves
# "no new instances", never "the remaining ones are fine" — and saying so is the
# point, because a guard that overstates what it checked is the bug this file
# exists to prevent.
_FLEET_PREDICATE = re.compile(r"merged_at IS NULL\s+AND\s+is_duplicate\s*=\s*0")

# ★ AUDITED BASELINE (2026-08-05). Every remaining occurrence was read in
# context and classified; all 11 are CORRECT usage or narration, not bugs. That
# upgrades this test from "ratchet over unknowns" to "ratchet over a set someone
# actually checked" — so any increase now means a NEW bug, not more unknowns.
#
# The 28 that WERE bugs are fixed: operator_brief (13), market_brief (7),
# state_brief (4), lp_alerts_cron (2), brain_consistency_radar (1),
# tenant_directory (1). /api/v1/operator-brief/equinix returned
# "operator_not_found" while /api/v1/operators/equinix reported 543 facilities,
# in the same second.
#
# Why each survivor is legitimate:
#   discovery_auto_approve / merge_discovered / merge_discovered_v2 — the merge
#     pipeline itself. Finding un-promoted rows IS its job.
#   main.py — two auto-approval loop gates ("is there anything pending?"), one
#     dedup backlog selector, one docstring describing that selector.
#   discovery_routes.py — /discovery/status, where the value is literally named
#     `pending` beside total_staged and total_merged.
#   flywheel_master_shell.py — NEGATED: `NOT (merged_at IS NULL AND
#     is_duplicate=0)` counts ALREADY-PROCESSED rows, deliberately.
#   facilities_delta.py — a SQL `--` comment INSIDE a DDL string. ★ _strip_narration
#     removes Python `#` comments and docstrings but cannot see SQL comments
#     nested in string literals, so narration inside SQL still scans as code.
#     Same trap that re-armed the #2058 backfill scanner.
#
# Lower these as anything changes; never raise one to make a test pass.
_PENDING_QUEUE_BASELINE = {
    "merge_discovered.py": 3,            # merge pipeline — correct
    "main.py": 3,                        # approval-loop gates + backlog selector
    "discovery_auto_approve.py": 1,      # approval queue — correct
    "merge_discovered_v2.py": 1,         # merge pipeline — correct
    "routes/discovery_routes.py": 1,     # /discovery/status `pending` counter
    "routes/facilities_delta.py": 1,     # SQL comment inside a DDL string
    "routes/flywheel_master_shell.py": 1,  # NEGATED — counts processed rows
}


def _strip_narration(src: str) -> str:
    """Blank out `#` comments and docstrings.

    Required, not cosmetic: this very file quotes the bad predicate to describe
    it, and routes/operators.py documents the fix in its module docstring. A
    scanner that reads narration convicts the documentation.
    """
    import ast as _ast
    lines = src.splitlines(True)
    keep = [True] * (len(lines) + 2)
    try:
        for node in _ast.walk(_ast.parse(src)):
            if not isinstance(node, (_ast.Module, _ast.FunctionDef,
                                     _ast.AsyncFunctionDef, _ast.ClassDef)):
                continue
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                for i in range(body[0].lineno, (body[0].end_lineno or 0) + 1):
                    if i < len(keep):
                        keep[i] = False
    except Exception:
        pass
    return "".join("\n" if not keep[i] else re.sub(r"#.*$", "", ln)
                   for i, ln in enumerate(lines, 1))


def _pending_queue_counts() -> dict:
    counts = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "node_modules", "venv", ".venv",
                                    "__pycache__", "dchub-frontend"}]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
            if rel.startswith("tests" + os.sep):
                continue
            try:
                src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
            except Exception:
                continue
            if "discovered_facilities" not in src:
                continue
            n = len(_FLEET_PREDICATE.findall(_strip_narration(src)))
            if n:
                counts[rel.replace(os.sep, "/")] = n
    return counts


def test_no_new_pending_queue_predicate_anywhere():
    """Ratchet: no file may ADD `merged_at IS NULL AND is_duplicate = 0`.

    Proves only that the count has not grown. It does NOT certify the
    39 baselined occurrences as correct.
    """
    counts = _pending_queue_counts()
    new_files = sorted(set(counts) - set(_PENDING_QUEUE_BASELINE))
    assert not new_files, (
        "issue #1539: these files newly pair merged_at IS NULL with the dup "
        "filter, which reads the DRAINED PENDING QUEUE (~zero rows), not the "
        "fleet. Use COALESCE(is_duplicate, 0) = 0 alone:\n  "
        + "\n  ".join(new_files))
    grew = {f: (counts[f], _PENDING_QUEUE_BASELINE[f])
            for f in counts if counts[f] > _PENDING_QUEUE_BASELINE[f]}
    assert not grew, (
        "issue #1539: occurrences increased (file: now vs baseline) — "
        f"{grew}. The fleet filter is COALESCE(is_duplicate, 0) = 0 alone.")


def test_operators_surface_uses_the_fleet_filter():
    """The specific surface this fixed: /operators served '0 tracked' live.

    MUTATION: restore `merged_at IS NULL AND is_duplicate = 0` in
    routes/operators.py -> this fails.
    """
    src = open(os.path.join(ROOT, "routes", "operators.py"), encoding="utf-8").read()
    code = _strip_narration(src)
    assert not _FLEET_PREDICATE.search(code), (
        "routes/operators.py is back on the pending-queue predicate — "
        "/operators renders '0 tracked' and gets indexed that way")
    assert "COALESCE(is_duplicate, 0) = 0" in code, (
        "routes/operators.py must read the fleet with COALESCE(is_duplicate,0)=0")


# ═══════════════════════════════════════════════════════════════════════════
# ai-agents.json — the manifest agents read FIRST (2026-08-20)
# ═══════════════════════════════════════════════════════════════════════════
# The defect this fences is NOT a literal, which is exactly why every regex
# above missed it for months: `"facilities": f"{_live_counts['facilities']:,}"`
# is a RUNTIME value. No amount of pattern-matching over source text can say
# which BASIS an expression evaluates to. So this guard is STRUCTURAL — it
# asserts the field is wired to the canon accessor, which is the one property
# that stays true across the next basis change as well.
#
# What shipped until 2026-08-20, measured live on BOTH /api/v1/ai-agents.json
# and /.well-known/ai-agents.json:
#     served  "facilities": "26,334"  = COUNT(*) discovered_facilities (ROWS)
#     canon                  18,400+  = DISTINCT canonical_slug   (BUILDINGS)
# ~1.4x over our OWN published floor — the canonical_floor_above_live_reality
# failure canon exists to prevent. The tell was that the conditional's FALLBACK
# was already canon: the manifest was honest only while the DB was DOWN.
_AI_AGENTS_COVERAGE_KEYS = {
    "facilities", "countries", "news_articles",
    "deals_tracked", "capacity_pipeline_gw", "update_frequency",
}

# field -> the canon placeholder it must resolve through.
# news_articles is deliberately ABSENT: it counts `announcements`, and canon
# carries no news key, so there is nothing to bind it to. Adding one is a basis
# decision, not a wiring fix — do not "fix" that by inventing a placeholder.
_CANON_BOUND_FIELDS = {
    "facilities":    "{canon_facilities}",
    "countries":     "{canon_countries}",
    "deals_tracked": "{canon_deals}",
}


def _ai_agents_data_coverage_node():
    """main.py's ai-agents.json data_coverage dict, via AST.

    Tests never import main.py (it opens DB pools and registers ~200
    blueprints), so the dict is parsed out of the source instead — the same
    convention the rest of this suite uses for shipped code."""
    import ast
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Dict):
            keys = {k.value for k in n.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if keys == _AI_AGENTS_COVERAGE_KEYS:
                return n
    return None


def test_ai_agents_manifest_coverage_binds_to_canon():
    """data_coverage must DERIVE its headline counts, never re-query them.

    MUTATION: restore `"facilities": f"{_live_counts['facilities']:,}" if ...`
    in main.py's ai-agents.json route -> this fails.
    """
    import ast
    node = _ai_agents_data_coverage_node()
    # An extraction that finds nothing would make every assertion below vacuous
    # (§the guard-is-vacuous-because-of-its-surface-list trap). Fail loudly.
    assert node is not None, (
        "main.py's ai-agents.json data_coverage dict was not found by key set "
        f"{sorted(_AI_AGENTS_COVERAGE_KEYS)}. If a field was renamed, update "
        "_AI_AGENTS_COVERAGE_KEYS — do NOT delete this guard, which would "
        "silently un-fence the manifest agents read first.")

    fields = {k.value: v for k, v in zip(node.keys, node.values)
              if isinstance(k, ast.Constant)}

    for name, placeholder in _CANON_BOUND_FIELDS.items():
        expr = fields.get(name)
        assert expr is not None, f"data_coverage lost its {name!r} field"
        src = ast.unparse(expr)
        # Must be a bare _canon_text("{canon_*}") call — not a conditional whose
        # live branch re-queries. A live-vs-fallback conditional is precisely how
        # the two paths came to disagree: the degraded one was the honest one.
        assert "_live_counts" not in src, (
            f"data_coverage[{name!r}] is back on a live re-query: {src!r}\n"
            "Bind to the canon accessor instead — a re-implemented COUNT is how "
            "this basis drifted the last three times (capabilities.json, "
            "content_enqueue, and this manifest).")
        assert placeholder in src, (
            f"data_coverage[{name!r}] must resolve through {placeholder} "
            f"(canon), got: {src!r}")

    # The raw pile must not be re-queried anywhere in the manifest route.
    src_all = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    route_i = src_all.find("if path == '/.well-known/ai-agents.json'")
    assert route_i > 0, "the ai-agents.json route moved — re-anchor this guard"
    route_src = src_all[route_i:route_i + 20000]
    assert "COUNT(*) FROM discovered_facilities" not in route_src, (
        "the ai-agents.json route re-introduced a raw COUNT(*) over "
        "discovered_facilities. That is the ROW count (~1.5 rows/site), not the "
        "building count. canon already owns this number.")


def test_ai_agents_manifest_renders_floors_not_raw_counts():
    """Render the block — never infer the served value from the source.

    Guards the direction of the error too: a canon binding that resolved to
    canonical_stats' 2026-06-30 cold-start seed would publish "400+" with no
    DATABASE_URL — a 46x UNDER-claim, which is NOT the safe direction of a 1.4x
    over-claim. {canon_*} reads PINNED (canon_nums reads it directly, and
    resolve_canon deep-copies rather than mutating), so it stays a static
    conservative floor in every DB state.

    MUTATION: point data_coverage at facilities_verified_phrase() -> this fails.
    """
    import ast
    from ai_surface_canon import canon_text

    node = _ai_agents_data_coverage_node()
    assert node is not None, "data_coverage dict not found — see the guard above"

    # The namespace deliberately carries the OTHER canon helpers a surface
    # author might plausibly reach for, not just _canon_text. Without them a
    # re-binding to facilities_verified_phrase() would die here on NameError —
    # failing, but for the wrong reason, and leaving the floor assertions below
    # unable to fail from any realistic mutation. A guard whose checks cannot
    # fire is decoration; these must exercise the value, not the import.
    import canonical_stats as _cs
    ns = {"_canon_text": canon_text, "_live_counts": {}}
    ns.update({n: getattr(_cs, n) for n in dir(_cs) if n.endswith("_phrase")})

    # DB-down is the interesting state: _live_counts is empty there.
    rendered = eval(ast.unparse(node), ns)                  # noqa: S307 - our own AST

    for name in _CANON_BOUND_FIELDS:
        val = rendered[name]
        assert val and val.endswith("+"), (
            f"data_coverage[{name!r}] rendered {val!r}. Canon publishes FLOOR "
            "phrases ('18,400+'); an exact integer is not a floor and cannot "
            "carry the round-DOWN guarantee that keeps us from over-claiming.")

    fac = int(rendered["facilities"].rstrip("+").replace(",", ""))
    assert fac >= 15000, (
        f"facilities rendered {rendered['facilities']!r} — below the floor DC Hub "
        "already publishes. That is the cold-start-seed trap: a canon binding "
        "that under-claims by an order of magnitude is not 'safe because floors "
        "round down'. Omit the field rather than publish a seed.")
    assert fac <= 26000, (
        f"facilities rendered {rendered['facilities']!r} — that is the raw "
        "discovery pile (ROWS), not distinct buildings.")


# ═══════════════════════════════════════════════════════════════════════════
# the SECOND ai-agents manifest generator (2026-08-23)
# ═══════════════════════════════════════════════════════════════════════════
# The guard above fences main.py's data_coverage — the dict that is actually
# served. mcp_gateway.MCPGateway._generate_ai_agents_json() builds a SECOND
# one, and nothing fenced it, so it kept its own hand-typed copy of the deals
# floor (`else "1,800+"`) while its two siblings in the same dict already fell
# back through canon_text(). It has no caller today, which is exactly why it
# rotted quietly: dead code that renders a headline number is a landmine, not
# a nullity — the next author to wire it up ships a floor last touched by
# hand on 2026-08-16.
#
# That copy also sat OUTSIDE the one instrument that would have caught it. The
# claim ledger registers `canon:public.deals` against ai_surface_canon.PINNED
# and nothing else, so on 2026-08-23 claim 100974 refuted the pin (1,800+ vs a
# live 1,900+) and could not have said one word about this line.
#
# Same structural shape as the guard above, for the same reason: the defect is
# not a literal you can regex for once the expression becomes a conditional.
_GATEWAY_COVERAGE_KEYS = {
    "facilities", "countries", "capacity_tracked_mw", "news_articles",
    "deals_tracked", "news_sources", "update_frequency",
}


def _gateway_data_coverage_node():
    """mcp_gateway's data_coverage dict, via AST (see the note above on why
    this suite parses shipped source instead of importing it)."""
    import ast
    src = open(os.path.join(ROOT, "mcp_gateway.py"), encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Dict):
            keys = {k.value for k in n.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if keys == _GATEWAY_COVERAGE_KEYS:
                return n
    return None


def test_gateway_manifest_deals_floor_is_not_a_second_typed_copy():
    """mcp_gateway's deals fallback must resolve through the canon.

    MUTATION: restore `else "1,800+"` in mcp_gateway.py -> this fails.
    """
    import ast
    node = _gateway_data_coverage_node()
    # Extraction that finds nothing makes every assertion below vacuous.
    assert node is not None, (
        "mcp_gateway's data_coverage dict was not found by key set "
        f"{sorted(_GATEWAY_COVERAGE_KEYS)}. If a field was renamed, update "
        "_GATEWAY_COVERAGE_KEYS — do NOT delete this guard.")

    fields = {k.value: v for k, v in zip(node.keys, node.values)
              if isinstance(k, ast.Constant)}
    expr = fields.get("deals_tracked")
    assert expr is not None, "data_coverage lost its 'deals_tracked' field"
    src = ast.unparse(expr)
    assert "{canon_deals}" in src, (
        f"mcp_gateway data_coverage['deals_tracked'] does not resolve through "
        f"{{canon_deals}}: {src!r}\nA hand-typed floor here is invisible to the "
        "claim ledger, which only ever watches ai_surface_canon's canon key.")


def test_gateway_manifest_deals_floor_follows_the_pin_when_it_moves(monkeypatch):
    """Pin the COUPLING, not the number.

    A test asserting the rendered value equals "1,900+" passes just as happily
    against a hardcoded literal — that is the shape of guard that let this copy
    live through three pin bumps. So move the canon to a value no literal in
    the tree carries and require the render to follow.
    """
    import ast
    import ai_surface_canon
    from ai_surface_canon import canon_text

    sentinel = "4,242+"
    monkeypatch.setitem(ai_surface_canon.PINNED["public"], "deals", sentinel)

    node = _gateway_data_coverage_node()
    assert node is not None, "data_coverage dict not found — see the guard above"

    # DB-down is the interesting state: live_counts is empty there. It is also
    # the ONLY state this generator has ever had — `from db import db_query`
    # names a module that is not in this repo, so the fallback is the whole
    # behaviour, not a rare degraded path.
    ns = {"canon_text": canon_text, "live_counts": {},
          "DISCOVERY_FILES": {}, "self": None}
    rendered = eval(ast.unparse(node), ns)                  # noqa: S307 - our own AST

    assert rendered["deals_tracked"] == sentinel, (
        f"data_coverage['deals_tracked'] rendered "
        f"{rendered['deals_tracked']!r} while the canon pin said {sentinel!r} "
        "— the floor is still typed a second time here.")


def test_gateway_manifest_deals_floor_never_renders_empty(monkeypatch):
    """The fail-open trap the two guards above cannot see.

    canon_text() is fail-open: an unwired placeholder resolves to ''. Swapping
    the literal for a MISSPELLED token ({canon_deal}) would satisfy a
    'no literal here' guard and publish a blank count instead of a stale one —
    a different failure, equally quiet.
    """
    import ast
    from ai_surface_canon import canon_text

    node = _gateway_data_coverage_node()
    assert node is not None, "data_coverage dict not found — see the guard above"
    ns = {"canon_text": canon_text, "live_counts": {},
          "DISCOVERY_FILES": {}, "self": None}
    rendered = eval(ast.unparse(node), ns)                  # noqa: S307 - our own AST
    val = rendered["deals_tracked"]
    assert val and val.strip(), (
        "data_coverage['deals_tracked'] rendered empty — the {canon_deals} "
        "placeholder is not wired in canon_nums(), and canon_text() fails open "
        "to ''.")
    assert val.endswith("+"), (
        f"data_coverage['deals_tracked'] rendered {val!r}; canon publishes "
        "FLOOR phrases, and an exact integer cannot carry the round-DOWN "
        "guarantee that keeps the manifest from over-claiming.")


def test_gateway_manifest_still_prefers_a_live_count_over_the_floor():
    """CONTROL — must stay GREEN before and after the canon rewiring.

    The canon is the FALLBACK, not the answer. The cheapest way to satisfy
    every guard above is to collapse the expression to a bare
    canon_text("{canon_deals}") and delete the live branch — which would
    publish a hand-maintained floor in place of a real count the moment this
    generator is ever wired up. That over-broad "fix" passes A, B and C and
    fails only here.
    """
    import ast
    from ai_surface_canon import canon_text

    node = _gateway_data_coverage_node()
    assert node is not None, "data_coverage dict not found — see the guard above"
    ns = {"canon_text": canon_text, "live_counts": {"deals": 1931},
          "DISCOVERY_FILES": {}, "self": None}
    rendered = eval(ast.unparse(node), ns)                  # noqa: S307 - our own AST
    assert rendered["deals_tracked"] == "1,931", (
        f"expected the live count '1,931', got {rendered['deals_tracked']!r} — "
        "the canon is the floor of last resort, not the served value")


def test_the_exclusion_ledger_has_not_rotted():
    """An exclusion that covers nothing is worse than no exclusion: it reads as
    a considered decision while silently protecting a file that may since have
    grown a real violation.

    ★2026-08-30 audit. Four of 22 entries were dead. Three named test files
    already covered by _EXCLUDE_DIRS "/tests/" — they had never done anything.
    The fourth, mcp_bug_fixes_and_new_tools.py, was genuine paid debt: the
    fence passes without it, so the exclusion was holding a real root-level
    file out of coverage for no reason. Both classes are checked here, cheaply
    and without re-running the fence 22 times.
    """
    dead, shadowed = [], []
    present = set()
    for root, _dirs, files in os.walk(ROOT):
        rel = root[len(ROOT):].replace(os.sep, "/") + "/"
        if any(x in rel for x in _EXCLUDE_DIRS):
            continue
        present.update(files)
    for name in _EXCLUDE_FILES:
        hits = [d for d in _EXCLUDE_DIRS if name.startswith(d.strip("/"))]
        if name not in present:
            # the file is gone, or lives only under an already-excluded dir
            (shadowed if _basename_only_under_excluded_dir(name) else dead).append(name)
        del hits
    assert not dead and not shadowed, (
        "_EXCLUDE_FILES names entries that no longer exclude anything — drop "
        "them so the files they name are covered again:\n"
        + "".join(f"  gone/unscanned: {n}\n" for n in dead)
        + "".join(f"  shadowed by _EXCLUDE_DIRS: {n}\n" for n in shadowed))


def _basename_only_under_excluded_dir(name):
    """True when every copy of `name` in the tree sits under an _EXCLUDE_DIRS
    path — i.e. the file-level entry is redundant with the directory rule."""
    found_anywhere = False
    for root, _dirs, files in os.walk(ROOT):
        if name not in files:
            continue
        found_anywhere = True
        rel = root[len(ROOT):].replace(os.sep, "/") + "/"
        if not any(x in rel for x in _EXCLUDE_DIRS):
            return False
    return found_anywhere


def test_the_scan_could_actually_read_every_file_it_claims_to_check():
    """A fence that swallows read errors reports clean on a partial scan."""
    _SCAN_ERRORS.clear()
    _scan([re.compile(r"\bzzz_no_such_token_zzz\b")])
    assert not _SCAN_ERRORS, (
        "the honest-numbers scan could not read these files, so every result "
        "above covers less than it claims. Refusing to report clean on a "
        "partial scan:\n" + "\n".join("  " + e for e in _SCAN_ERRORS))
