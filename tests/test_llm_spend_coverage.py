"""Every Anthropic call site in the tree must go through the spend ledger.

WHY THIS IS A GUARD AND NOT A ONE-OFF CLEANUP
=============================================
Writing `requests.post(anthropic_messages_url(), ...)` — or the urllib
two-step — is the obvious thing to write, it works, and nothing complains. The
ledger then reports a number that looks like spend and is actually a floor,
and the next person to ask "where are the tokens going?" gets a confident,
wrong answer. So the durable fix is this test, not the edits beside it.

★ WHY THE GUARD IS CALL-SITE LEVEL, NOT FILE LEVEL
==================================================
The first version of this test asked brain_llm_spend's file-level scan
("does this module mention instrumented_post?") and it was measurably
worthless: the mutation that reverted a WIRED call back to `requests.post`
left the `import ... instrumented_post as _llm_post` line in place, so the
module still counted as instrumented and the test stayed GREEN. Two of the
six mutations survived, and they were the two the guard exists for.

The file-level scan was also blind to an entire transport. Its predicate
required `requests.post`, so the ~26 urllib callers never entered the
numerator OR the denominator — they were not a measured gap, they were
invisible. That is why the reported gap was 4 while the real one was 39.

THE RATCHET
===========
39 call sites across 27 modules are still raw, and they are being wired in
reviewable batches rather than one 27-module change that deploys on merge.
Until that finishes this guard RATCHETS: no new module may appear and no
module may gain a call site. When a batch lands, BASELINE must be lowered —
test_ratchet_is_tight fails if it is not, so the debt cannot silently drift
back up behind a stale allowance.

Keyed by MODULE and COUNT, not by line number: line numbers move on any edit
to the file, so a line-keyed baseline would fail on unrelated changes and
train people to regenerate it without reading it.

NOTE on the harness: the CI unit-tests job installs ONLY pytest (not
requirements.txt), so importing routes.brain_llm_spend would crash collection
on Flask. We AST-extract the scan functions and exec them against the REAL
module path, so this tests the shipped scanner rather than a reimplementation
that could drift from the thing it guards.
"""
import os
import ast
import logging
import pathlib
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEND = os.path.join(ROOT, "routes", "brain_llm_spend.py")

_FUNCS = ("_scanned_files", "instrumented_modules", "count_llm_modules",
          "uninstrumented_modules", "uninstrumented_call_sites",
          "_is_anthropic_arg")
_NAMES = ("_instrumented", "_llm_module_count", "_WRAPPER_NAMES", "_RAW_CALLEES")

#: Raw Anthropic call sites per module, as of 2026-09-20. LOWER THIS when a
#: batch is wired; never raise it.
BASELINE = {
    "agentic_master_shell": 3, "ai_citation_tracker": 1, "analyst_note": 1,
    "brain_answer_cache": 1, "brain_feature_proposer": 1, "brain_inspector": 3,
    "brain_investigator": 1, "brain_lane_driver": 1, "brain_models": 1,
    "brain_v2_layer4": 2, "feedback_triage": 1, "geo_autopublish": 1,
    "linkedin_content_engine": 2, "marketing_engine": 3, "media_citation_gap": 1,
    "media_comment_engagement": 1, "media_dm_follow_up": 1,
    "media_journalist_lane": 1, "media_recurring_formats": 1,
    "media_spike_responder": 1, "media_thread_generator": 3,
    "news_entity_extraction": 1, "og_cards": 1, "sales_outreach_automator": 1,
    "testimonial_probe": 3, "weekly_newsletter": 1, "wins_poster": 1,
}

#: Wired 2026-09-20. These must NEVER reappear in the gap.
WIRED = ("media_published_review", "brain_strategic_planner",
         "ai_platform_tool_tuner", "content_publisher", "main",
         "brain_layer23_lifecycle")


def _load_scanner(spend_path=SPEND):
    """Exec the REAL scanner's functions against `spend_path` as their
    __file__. Defaults to the shipped module; a test passes a miniature
    checkout's routes/brain_llm_spend.py to run the same scanner from a path
    built to trip it (tests/test_repo_scans_are_path_independent.py)."""
    src = open(SPEND, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            pieces.append(ast.get_source_segment(src, node))
    # __file__ is what _scanned_files() derives routes/ and the repo root from,
    # so it must point at the real module for the scan to see the real tree.
    ns = {"logger": logging.getLogger("test"), "__file__": str(spend_path)}
    exec(compile("\n\n".join(pieces), SPEND, "exec"), ns)
    missing = [n for n in _FUNCS + _NAMES if n not in ns]
    assert not missing, f"AST extraction missed {missing} — update _FUNCS/_NAMES"
    return ns


NS = _load_scanner()


def _gap_counts():
    sites = NS["uninstrumented_call_sites"]()
    assert sites is not None, "call-site scan failed — unmeasured, not clean"
    return collections.Counter(m for m, _ln in sites), sites


# ── Floors: the scan must be capable of finding things ───────────────────

def test_scan_floor():
    """Every assertion below is vacuous if the scan finds nothing. A renamed
    helper, a moved directory or a glob that stops matching would otherwise
    turn this whole file green while measuring zero."""
    files = NS["_scanned_files"]()
    assert len(files) > 200, f"only {len(files)} files scanned — glob is broken"
    total = NS["count_llm_modules"]()
    assert total is not None, "module scan failed — coverage is unmeasured"
    assert total >= 20, (
        f"only {total} model-request modules found; the scan predicate or the "
        "layout changed and this guard has stopped guarding")


def test_call_site_scan_floor():
    """The call-site scan must still resolve real sites. If this drops to zero
    while BASELINE is non-empty, the AST walk broke — that is a broken scan,
    not a finished migration, and it must not read as success."""
    _counts, sites = _gap_counts()
    assert len(sites) > 0 or not BASELINE, (
        "call-site scan found NOTHING while BASELINE still lists work — the "
        "AST walk is broken. A scan that cannot find anything always passes.")


def test_anthropic_arg_resolver_actually_resolves():
    """Pin the resolver itself: it must see through a local variable and into
    a urllib Request, which is how most of these call sites are written."""
    f = NS["_is_anthropic_arg"]
    mod = ast.parse(
        "u = anthropic_messages_url()\n"
        "req = Request(u, data=b)\n"
        "lit = 'https://api.anthropic.com/v1/messages'\n"
        "other = 'https://api.linkedin.com/rest/posts'\n")
    assigns = {n.targets[0].id: n.value for n in mod.body
               if isinstance(n, ast.Assign)}
    assert f(assigns["u"], assigns) is True, "direct helper call not resolved"
    assert f(assigns["req"], assigns) is True, "Request-wrapped URL not resolved"
    assert f(assigns["lit"], assigns) is True, "literal endpoint not resolved"
    assert f(assigns["other"], assigns) is False, "LinkedIn matched as Anthropic"


# ── The ratchet ──────────────────────────────────────────────────────────

def test_no_new_uninstrumented_module():
    counts, _ = _gap_counts()
    new = sorted(set(counts) - set(BASELINE))
    assert not new, (
        f"new module(s) posting to Anthropic without the ledger: {new}\n"
        "Wire them: `from routes.brain_llm_spend import instrumented_post as "
        "_llm_post` (requests) or `instrumented_urlopen` (urllib). Both return "
        "the same object and raise the same exceptions as what they replace.")


def test_no_module_gained_a_call_site():
    counts, _ = _gap_counts()
    grew = {m: (counts[m], BASELINE[m]) for m in counts
            if m in BASELINE and counts[m] > BASELINE[m]}
    assert not grew, f"module(s) gained raw call sites (now, baseline): {grew}"


def test_ratchet_is_tight():
    """When a batch is wired, BASELINE must come down with it. A stale, too-
    generous allowance is how a debt register quietly stops being one."""
    counts, _ = _gap_counts()
    slack = {m: (counts.get(m, 0), BASELINE[m]) for m in BASELINE
             if counts.get(m, 0) < BASELINE[m]}
    assert not slack, (
        "these modules have FEWER raw call sites than BASELINE allows — good, "
        f"now lower BASELINE to match (now, baseline): {slack}")


def test_wired_modules_never_regress():
    counts, _ = _gap_counts()
    back = [m for m in WIRED if m in counts]
    assert not back, (
        f"module(s) wired on 2026-09-20 have raw Anthropic calls again: {back}")


# ── Scanner integrity ────────────────────────────────────────────────────

def test_numerator_and_denominator_agree():
    total = NS["count_llm_modules"]()
    inst = NS["instrumented_modules"]()
    gap = NS["uninstrumented_modules"]()
    assert len(inst) + len(gap) == total, (
        f"instrumented({len(inst)}) + gap({len(gap)}) != total({total}) — the "
        "two scans disagree, so coverage is arithmetic on mismatched sets")


def test_ledger_module_excluded_from_both_sides():
    """brain_llm_spend.py contains the literals it searches for, so it matches
    its own predicate without issuing a request. Counted only in the
    denominator it produced a permanent phantom gap that looked exactly like
    one real uninstrumented module."""
    assert "brain_llm_spend" not in NS["instrumented_modules"]()
    assert "brain_llm_spend" not in NS["uninstrumented_modules"]()
    assert "brain_llm_spend" not in {m for m, _ in NS["uninstrumented_call_sites"]()}


#: Directory names that mean "not the deployed tree": vendored copies and whole
#: stale checkouts. Matched BY SEGMENT against the path relative to the repo
#: root — see _stale_dirs_reached.
_STALE_DIRS = frozenset({".claude", "worktrees", "node_modules", "venv",
                         ".venv", "site-packages"})


def _stale_dirs_reached(files, root):
    """[(path, [segment, ...])] for every scanned file that lies inside a
    vendored dir or a stale checkout — judged RELATIVE to `root`, by SEGMENT.

    ★ It used to be `bad in str(f)` over the ABSOLUTE path, and an absolute
    path carries the checkout's own ANCESTORS. Claude Code puts its worktrees
    at ~/dchub-backend/.claude/worktrees/<name>/, so from one of those EVERY
    file of a pristine tree matched ".claude" and this guard failed on a clean
    repo — the "skip-list vs absolute path" class, see
    tests/test_repo_scans_are_path_independent.py. Substring matching is wrong
    in the other direction too: routes/venv_probe.py would match "venv".

    A path that is not under `root` at all is an offender in its own right:
    that is the scan reading some other checkout entirely.
    """
    root = pathlib.Path(root).resolve()
    out = []
    for f in files:
        p = pathlib.Path(f).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            out.append((str(p), ["<outside the repo root>"]))
            continue
        hit = sorted(_STALE_DIRS.intersection(rel.parts[:-1]))
        if hit:
            out.append((str(rel), hit))
    return out


def test_scan_excludes_stale_worktree_checkouts():
    """.claude/worktrees holds whole stale copies of these same modules. A
    recursive glob would count code that is not deployed and cannot be."""
    files = NS["_scanned_files"]()
    # Floor: zero files would make the check below pass while measuring
    # nothing — the same shape as the bug it guards.
    assert len(files) > 200, f"only {len(files)} files scanned — glob is broken"
    offenders = _stale_dirs_reached(files, ROOT)
    assert not offenders, (
        f"scan reached vendored/stale paths: {offenders[:3]} — these are "
        "judged relative to the repo root, so this is a real reach, not the "
        "checkout's own ancestors")


def test_known_root_level_call_sites_are_seen():
    """main.py and content_publisher.py are in the repo ROOT, not routes/. The
    scan was routes/-only until 2026-09-20 while its own note claimed to cover
    'the tree', so these two were invisible to both sides of the ratio."""
    inst = NS["instrumented_modules"]()
    for stem in ("main", "content_publisher"):
        assert stem in inst, (
            f"{stem}.py posts to Anthropic but the scan does not see it — "
            "_scanned_files() is back to routes/-only")


def test_both_wrappers_are_recognised():
    """instrumented_urlopen is the urllib half. If it falls out of
    _WRAPPER_NAMES every wired urllib site silently reappears as a gap."""
    for name in ("instrumented_post", "instrumented_urlopen"):
        assert name in NS["_WRAPPER_NAMES"], f"{name} is not a known wrapper"
    assert "Request" not in NS["_RAW_CALLEES"], (
        "Request is back in _RAW_CALLEES — wiring replaces the urlopen and "
        "leaves the Request, so this would flag correctly-wired sites")
