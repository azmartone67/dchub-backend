"""Phase 289 — Brain v2 Layer 4: self-learning novel-fix loop.

What this is
============
The healer (dchub_self_heal.py) detects bugs. The master-heal workflow
(dchub-frontend/.github/workflows/master-heal.yml) auto-fixes patterns
in a hand-curated FIX_MAP. But the FIX_MAP only knows the bugs we've
seen before — every new pattern has to be added manually.

This module closes that loop. When the healer surfaces an actionable
issue NOT in any known fix-table, this module:

  1. Pulls the surrounding HTML context (the cell + ~200 chars before/after).
  2. Sends it to Anthropic's Claude API with a prompt asking for a
     concrete find/replace fix.
  3. Validates the suggestion (find pattern actually exists in the file,
     replacement is non-empty, no obvious safety issues like raw HTML
     injection or unbounded substitution).
  4. Writes the suggested fix to a structured record at
     /api/v1/brain/proposed-fixes for the master-heal workflow to consume.

The master-heal workflow's "Auto-fix HTML quality issues" step is then
updated (Phase J of this PR) to also read FIX_MAP entries from the
brain. New patterns get auto-fixed without a human ever touching the
workflow YAML.

Environment
-----------
  ANTHROPIC_API_KEY   required for the API call. If unset, the loop
                       returns 503 — never blocks the heal cycle.
  DCHUB_BRAIN_MODEL   default: claude-sonnet-4-5 (fast + cheap enough
                       for a 5-min cron).
  DCHUB_BRAIN_MAX_LEARN   default: 10 per cycle (rate-cap learning so
                       a single broken page can't burn the model budget).

Endpoints
---------
  POST /api/v1/brain/learn                   admin-gated; triggers one
                                              learning pass on the
                                              current heal/findings
  GET  /api/v1/brain/proposed-fixes          returns the latest learned
                                              fix-map suggestions
                                              (master-heal consumes this)
  GET  /api/v1/brain/learning-log            recent learning attempts +
                                              outcomes (success / refused
                                              / api_error / validation_fail)

Safety
------
  • Read-only against the live HTML — never auto-pushes a file edit.
    Suggestions get queued; the master-heal workflow applies them.
  • Validation: the proposed (find, replace) must satisfy:
      - len(find) >= 3
      - find actually present in at least one repo file
      - replace doesn't contain raw HTML tags unless the find does
      - replace isn't a duplicate of find
  • Rate-cap: max 3 learning calls per heal cycle, max 30 per day.
  • All proposals are LOGGED, never silently auto-applied.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, jsonify, request

brain_v2_bp = Blueprint("brain_v2", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# 2026-05-24 r30: route through brain_models tier registry. "inspector"
# tier maps to claude-opus-4-7 — the 1M-context model brain L4 actually
# needs for synthesis. DCHUB_BRAIN_MODEL_INSPECTOR env override per-tier;
# legacy DCHUB_BRAIN_MODEL still works as a global fallback inside
# brain_model_for(). Falls back gracefully if brain_models can't import.
try:
    from routes.brain_models import brain_model_for as _brain_model_for
    BRAIN_MODEL = (os.environ.get("DCHUB_BRAIN_MODEL_INSPECTOR")
                   or os.environ.get("DCHUB_BRAIN_MODEL")
                   or _brain_model_for("inspector"))
except Exception:
    BRAIN_MODEL = os.environ.get("DCHUB_BRAIN_MODEL", "claude-opus-4-7")
# 2026-05-30 WIDEN LEARNING SURFACE: raised default 3 → 10. With ~95 live
# actionable_backend_issues and only 3 reaching the model per cycle, the
# review queue was starved. 10/cycle widens throughput while still rate-
# capping model spend (override with DCHUB_BRAIN_MAX_LEARN). Proposals still
# land in the review queue only — no auto-PR / auto-apply.
BRAIN_MAX_LEARN = int(os.environ.get("DCHUB_BRAIN_MAX_LEARN", "10"))
ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")

# Phase S (2026-05-12): in-memory state is now a FALLBACK only. The
# durable copy lives in Postgres via routes.brain_v2_store, which:
#   - Survives Railway deploys (the old in-memory state reset every push,
#     which meant the 2-cycle approval gate could never trigger more
#     than once per deploy window).
#   - Is shared across gunicorn workers (the old per-worker state meant
#     proposals from worker A were invisible to worker B; identical
#     proposals just re-incremented two separate counters that never
#     reached the approval threshold).
#   - Tracks issue persistence so the brain can prioritize the bugs
#     master-heal's FIX_MAP couldn't auto-fix (the "learn from errors
#     it misses" worklist).
#
# When DATABASE_URL is unset (e.g. local dev), every store call returns
# a fail-soft sentinel and the in-memory lists keep the old behaviour
# so the brain never crashes the heal cycle.
_proposed_fixes: list[dict] = []   # in-memory fallback only
_learning_log: list[dict] = []     # in-memory fallback only
_MAX_BUFFER = 50

try:
    from routes import brain_v2_store as _store
    _STORE_OK = _store.init_schema()
    if _STORE_OK:
        print("[brain_v2_layer4] Phase S: durable state ENABLED via brain_v2_store",
              flush=True)
    else:
        print("[brain_v2_layer4] Phase S: store init failed, using in-memory fallback",
              flush=True)
except Exception as _store_err:
    _store = None
    _STORE_OK = False
    print(f"[brain_v2_layer4] Phase S: store import failed ({_store_err}), "
          "using in-memory fallback", flush=True)


def _require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return wrapper


def _log(entry: dict) -> None:
    """Append to learning log. Writes durable copy via store when available;
       always writes in-memory copy for back-compat with existing tests."""
    entry["t"] = datetime.now(timezone.utc).isoformat()
    if _STORE_OK:
        try: _store.log_event(entry)
        except Exception: pass
    _learning_log.append(entry)
    if len(_learning_log) > _MAX_BUFFER:
        _learning_log.pop(0)


def _validate_proposal(find: str, replace: str) -> tuple[bool, str]:
    """Reject obviously-unsafe fix suggestions. Returns (ok, reason).

    Phase NN (2026-05-13): bumped the find_too_short floor from 3 → 8
    chars. The brain dashboard showed every learning attempt failing
    with find_too_short — Claude was returning bare em-dashes (1-3
    chars) that, even if accepted, would have nuked typography across
    the page. Real placeholder fixes target leaf elements like
    `<td>—</td>` (9 chars) or `<div class="v">—</div>` (24 chars), so
    8 is a safe minimum that filters single-char hallucinations
    without rejecting legitimate proposals. Auto-expansion (see
    _auto_expand_find below) tries to rescue short finds before this
    validator sees them.
    """
    if not find:
        # Empty find = explicit refusal per the prompt contract.
        # Logged as "refused" in the learning loop, not validation_fail.
        return False, "refused"
    if len(find) < 8:
        return False, "find_too_short"
    if find == replace:
        return False, "noop"
    if not replace:
        return False, "empty_replace"
    # Don't let the model invent HTML tags
    if ("<" in replace or ">" in replace) and not ("<" in find or ">" in find):
        return False, "replace_introduces_html"
    # Don't allow JS in replacement
    if re.search(r"<script|javascript:|onerror=|onload=", replace, re.I):
        return False, "replace_has_js"
    # Don't let replace be much longer than find (sanity)
    if len(replace) > len(find) + 200:
        return False, "replace_too_long"
    return True, "ok"


def _auto_expand_find(find: str, snippet: str) -> str:
    """Phase NN (2026-05-13): rescue a short find by expanding to the
    enclosing HTML LEAF element — but ONLY when the find is the sole
    non-whitespace content of that element.

    Earlier draft of this helper grabbed the first textual match — which
    on /dc-hub-media meant it picked up the H1 typography em-dash
    ('DC Hub Media — the autonomous feed') instead of the actual
    placeholder div. That would be a regression: auto-expand turns the
    correct refusal into a typography-mangling fix.

    Leaf-only heuristic: scan every occurrence of `find` in the snippet,
    find the enclosing element bounds, and keep the candidate only if
    everything between the open and close tags is just whitespace +
    `find` itself. That guarantees we're expanding a placeholder cell,
    not a sentence with prose around the find string.
    """
    if not find or not snippet:
        return find
    if len(find) >= 16:
        return find  # already specific enough, leave alone

    LEFT_CAP, RIGHT_CAP = 240, 240
    # Walk all occurrences (most pages have em-dashes in multiple
    # places — prose AND placeholders).
    cursor = 0
    while True:
        idx = snippet.find(find, cursor)
        if idx < 0:
            return find  # exhausted; no leaf-only context found
        cursor = idx + len(find)
        # Bound the candidate to the enclosing tag.
        left_start = max(0, idx - LEFT_CAP)
        right_end = min(len(snippet), idx + len(find) + RIGHT_CAP)
        lt_open_start = snippet.rfind("<", left_start, idx)
        if lt_open_start < 0:
            continue
        # The open tag ends at the next `>` after lt_open_start
        open_tag_end = snippet.find(">", lt_open_start, idx)
        if open_tag_end < 0:
            continue
        # Find the matching close tag — the next `</…>` after the find.
        close_tag_start = snippet.find("</", idx + len(find), right_end)
        if close_tag_start < 0:
            continue
        close_tag_end = snippet.find(">", close_tag_start, right_end)
        if close_tag_end < 0:
            continue
        # The element's text content is everything between open_tag_end+1
        # and close_tag_start. Require that to be ONLY whitespace + the
        # original find — i.e., a leaf placeholder, not prose.
        inner = snippet[open_tag_end + 1:close_tag_start]
        if inner.strip() != find.strip():
            continue
        expanded = snippet[lt_open_start:close_tag_end + 1]
        low = expanded.lower()
        if "<script" in low or "<style" in low:
            continue
        if 8 <= len(expanded) <= 240:
            return expanded
    # Unreachable, but Python flow-analysis safety
    return find


def _call_claude(prompt: str, system: str) -> tuple[str | None, str | None]:
    """Single Anthropic call. Returns (text, error)."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key"
    try:
        import urllib.request
        import urllib.error
    except Exception as e:
        return None, f"stdlib_import_fail: {e}"
    # 2026-05-31 MODEL SELF-HEAL: try BRAIN_MODEL, but on a 404/400 (model not
    # found/invalid — e.g. a bad DCHUB_BRAIN_MODEL env like an unreleased
    # claude-opus-4-8-* date, which 404'd every Layer-5 call → 0 proposals for
    # 30d) retry ONCE with a confirmed-valid model so a misconfigured model
    # can't zero out the whole brain. Other codes (401/429/5xx) don't retry.
    _FALLBACK_MODEL = "claude-sonnet-4-20250514"
    _models = [BRAIN_MODEL] + ([_FALLBACK_MODEL] if BRAIN_MODEL != _FALLBACK_MODEL else [])
    last_err = None
    for _i, _model in enumerate(_models):
        body = json.dumps({
            "model": _model,
            "max_tokens": 800,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "Anthropic-Version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", ""), None
            return None, "no_text_block"
        except urllib.error.HTTPError as e:
            last_err = f"http_{e.code}"
            if e.code in (404, 400) and _i + 1 < len(_models):
                print(f"[brain_v2_layer4] model {_model} -> http_{e.code}; "
                      f"self-heal retry with {_models[_i+1]}", file=sys.stderr)
                continue
            return None, last_err
        except Exception as e:
            # EGRESS DIAGNOSTICS (2026-05-30): log the FULL exception repr
            # (type + message + errno) un-truncated so [Errno 101] IPv6/egress
            # failures land intact in the caller's api_error / /proposed-fixes.
            full = repr(e)
            print(f"[brain_v2_layer4] _call_claude egress failure: {full}",
                  file=sys.stderr)
            return None, f"call_fail: {full}"
    return None, last_err or "all_models_failed"


_LEARN_SYSTEM = (
    "You are the Brain v2 self-learning module for DC Hub. The HTML quality "
    "healer has detected a placeholder/stale-text issue on a page and the "
    "existing FIX_MAP has no entry for it. Your job: propose a single "
    "(find, replace) text substitution that fixes ONLY the actual data-cell "
    "placeholder, not stylistic typography elsewhere on the page.\n\n"
    "CRITICAL CONTEXT — em-dashes are tricky:\n"
    "  - Em-dashes inside `<td>—</td>` or `<div class=\"v\">—</div>` ARE "
    "    placeholders (a data cell that didn't populate). These should be fixed.\n"
    "  - Em-dashes inside meta descriptions, titles, alt text, or sentences "
    "    like 'DC Hub Media — the autonomous feed' ARE INTENTIONAL TYPOGRAPHY. "
    "    DO NOT propose replacing these. Refuse with rationale='intentional_typography'.\n"
    "  - If you can't isolate a specific placeholder cell, refuse — don't guess.\n\n"
    "Output STRICTLY a JSON object:\n"
    "  {\"find\": \"...\", \"replace\": \"...\", \"rationale\": \"...\"}\n\n"
    "Rules:\n"
    "  - `find` MUST be at least 8 characters and SHOULD be 12+. Bare em-dashes "
    "    are too generic — always include the enclosing tag.\n"
    "  - GOOD find examples (unambiguous, leaf-element-scoped):\n"
    "      `<td class=\"kpi-val\">—</td>`\n"
    "      `<span class=\"v\">—</span>`\n"
    "      `<div class=\"big-num\">—</div>`\n"
    "      `id=\"stat-total\">—<`     ← attribute-anchored, also fine\n"
    "  - BAD find examples (auto-rejected):\n"
    "      `—`              ← 1 char, matches everything\n"
    "      `>—<`            ← 3 chars, too generic\n"
    "      `the —`          ← matches prose\n"
    "  - `find` must include enough surrounding HTML context (the parent tag) "
    "    so the substitution can't accidentally match similar patterns elsewhere.\n"
    "  - `replace` should not introduce new HTML tags unless the original had them.\n"
    "  - `rationale` is a one-sentence explanation a human can verify in 5s.\n"
    "  - When in doubt, refuse: {\"find\": \"\", \"replace\": \"\", "
    "    \"rationale\": \"refused: <why>\"}. Empty find is the ONLY way to refuse. "
    "    A short non-empty find counts as a failed attempt, not a refusal.\n"
)


def _build_prompt(issue: dict, snippet: str) -> str:
    # Phase 300 (Phase R-2): include the issue label, count, page URL, AND
    # explicit guidance about scoping the find to a data-cell context (not
    # the broader page). Previously the prompt just said "find the pattern"
    # and Claude picked the FIRST em-dash on the page (usually in the meta
    # description) — leading to typography-mangling proposals.
    return (
        f"Issue label: {issue.get('issue','?')}\n"
        f"URL: {issue.get('url','?')}\n"
        f"Healer reports this pattern appears {issue.get('count','?')} time(s) on the page.\n\n"
        "Your task: find an UNAMBIGUOUS placeholder (a data-cell context — "
        "the em-dash is the sole text content of a `<td>`, `<span class=\"v\">`, "
        "`<div class=\"...kpi...\">`, or similar leaf element). NOT em-dashes "
        "in titles, meta descriptions, alt text, or prose.\n\n"
        "If you cannot isolate a placeholder cell (e.g. the only em-dashes on "
        "the page are in `<title>` or `content=\"...\"` attribute values), "
        "return refused.\n\n"
        f"HTML snippet (raw):\n```html\n{snippet[:1500]}\n```\n\n"
        "Propose a fix or refuse — JSON only."
    )


def _fetch_snippet(url: str, max_bytes: int = 6000) -> str:
    """Pull the live HTML around the issue. Best-effort — empty on failure."""
    try:
        import urllib.request
        H = {"User-Agent": "Mozilla/5.0 (DCHub-Brain-v2-Layer4)"}
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(max_bytes).decode("utf-8", errors="ignore")
        return body
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────

@brain_v2_bp.post("/api/v1/brain/learn")
@_require_admin
def trigger_learn():
    """Run one learning pass against /api/v1/heal/findings."""
    # Phase RR (2026-05-14): heartbeat. Stamp last_run_at on EVERY
    # invocation — including no-op passes that log nothing because the
    # healer findings were clean. brain_status reads this to tell
    # "healthy + quiet" (cron firing, nothing to learn) apart from
    # "stalled" (cron stopped firing). Before this they were
    # indistinguishable, so a healthy-quiet brain read as broken.
    if _STORE_OK:
        try:
            _store.set_meta("last_run_at",
                            datetime.now(timezone.utc).isoformat())
        except Exception:
            pass
    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set",
                       hint="Configure in Railway env to activate Layer 4"), 503

    # 1. Get current healer findings
    #
    # Phase QQ+15 (2026-05-13): switched from urllib network calls to
    # Flask's in-process test_client. The old code hit
    # http://127.0.0.1:8080/api/v1/heal/findings — but Railway uses
    # the $PORT env var (NOT 8080), so the loopback failed instantly.
    # Then it fell back to https://dchub.cloud/.../heal/findings which
    # bounces back through the CF Worker → got 403'd by the rate
    # limiter / Worker security rules when called from Railway's
    # outbound IP. Net effect: every hourly brain_learn cron returned
    # 502 with "can't reach /heal/findings: HTTP Error 403", which
    # explains why the brain had 0 proposed fixes for 18+ hours.
    #
    # Flask test_client invokes the route handler in-process — no
    # network, no port discovery, no CF Worker, no rate limiter. The
    # request gets a real Flask context and runs the actual handler
    # so the response is identical to a real HTTP call.
    # Phase GG (2026-05-15): the public-URL urllib fallback was causing
    # Railway 502 timeouts on every cron tick — Railway's outbound IP
    # hits the CF Worker which rate-limits, urllib waits 15s, Railway's
    # gateway returns 502 before the Flask handler can recover. Result:
    # the brain heartbeat was 8.7h stale even though the cron fires
    # hourly. Fix: read the heal cache DIRECTLY from main.py — no
    # test_client, no urllib, no HTTP at all. The cache is the same one
    # /heal/findings serves from, so we get identical data with zero
    # network risk.
    findings = {}
    try:
        from main import _HEAL_FINDINGS_CACHE, _HEAL_FINDINGS_LOCK
        with _HEAL_FINDINGS_LOCK:
            cached = _HEAL_FINDINGS_CACHE.get("payload")
        findings = cached or {}
    except Exception as e:
        # Cache not importable (very early in boot, or main.py changed).
        # Fall back to the in-process test_client once — bounded short.
        try:
            from flask import current_app
            with current_app.test_client() as _client:
                _resp = _client.get("/api/v1/heal/findings")
                if _resp.status_code == 200:
                    findings = _resp.get_json() or {}
        except Exception as e2:
            # Give up and proceed with empty findings — heartbeat is the
            # priority. Next cron tick will retry.
            print(f"[brain_v2_layer4] findings unavailable: "
                  f"cache={e}; test_client={e2}", flush=True)
            findings = {}

    issues = findings.get("actionable_frontend_issues", [])
    # Filter: only patterns NOT already in the known FIX_MAP. We hardcode
    # known patterns here; in production this should query master-heal.yml
    # or a shared config so we never relearn a known pattern.
    KNOWN = {
        "multi-GW placeholder", "NaN ago timestamp bug",
        "NAND AGO timestamp bug", "Save 34% stale text",
        "$249.50 stale text", "$798 stale text",
    }
    # Phase V (2026-05-12): linked-asset findings (issue label starts with
    # "asset_") can NEVER be fixed by a body-text find/replace — the actual
    # fix is "remove the broken <link> tag" or "deploy the missing file."
    # If we let Claude propose a substitution it would either no-op or
    # mangle the page. So we skip them entirely. master-heal's GH-issue
    # fallback path still surfaces these to a human.
    def _is_asset_issue(i):
        lbl = (i.get("issue") or "")
        # Phase Z (2026-05-12): api_contract_* labels also need backend
        # code changes (filter logic, gate removal, response shape fix),
        # not body substitution. Group them with asset_* for filtering.
        return lbl.startswith("asset_") or lbl.startswith("api_contract_")

    def _is_data_placeholder(i):
        # Phase FF (2026-05-22): CLASSIFICATION. An em-dash "— placeholder"
        # finding is a DATA-binding gap — a cell whose live value didn't
        # populate (e.g. the homepage "— FACILITIES — MW" field-name bug we
        # just fixed). It is NOT fixable by a text find/replace: there is no
        # static string to swap in — the cell must bind to a live value. Asking
        # Claude to substitute a bare "—" is precisely why the learning log was
        # all find_too_short / refused and volume sat at 0/4. Route these to the
        # data worklist instead of wasting a Claude text-fix call. Literal
        # placeholders (__FOO__, {{x}}) have no em-dash and stay text-fixable.
        return "—" in (i.get("issue") or "")

    # r63 (2026-05-29): compute the confirmed-false-positive set ONCE up front
    # (was recomputed twice below). Reused to (1) stop bumping persistence for
    # already-suppressed data placeholders — which is what kept seen_count
    # climbing to "×155" even after r43-H routed them — and (2) gate the
    # per-cycle re-log of "data_placeholder_routed" so the Learning-log tail
    # stops filling with the same routed-placeholder rows every 30 min.
    _suppressed_fp = set()
    if _STORE_OK:
        try:
            _suppressed_fp = _store.list_false_positives(min_refused=3)
        except Exception:
            _suppressed_fp = set()

    # Phase S (2026-05-12): "learn from errors it misses" — track every
    # issue's persistence in Postgres, then prioritize the most-stuck
    # ones (high seen_count, no successful proposal yet) for this learn
    # pass. Was: just take the first N novel issues. Now: pick from a
    # worklist where the brain's repeated failures bubble up.
    for i in issues:
        if _is_asset_issue(i) or i.get("issue") in KNOWN:
            continue
        # r63: a data placeholder already confirmed as a repeat non-text-fixable
        # (refused/routed >= 3 times) must NOT keep bumping seen_count — that
        # was the spin-loop: a benign em-dash cell re-counted every cycle made
        # the brain look busy-but-stuck (the "data_placeholder_routed ×155").
        # The em-dash detector source is already silenced for the benign pages
        # (dchub_self_heal r43-J EMDASH_IGNORE_URLS); this stops any residual
        # placeholder on other URLs from accreting once it's confirmed benign.
        if (_is_data_placeholder(i)
                and (i.get("issue"), i.get("url") or "") in _suppressed_fp):
            continue
        if _STORE_OK:
            try:
                _store.bump_persistence(
                    issue_label=i.get("issue") or "",
                    url=i.get("url") or "",
                )
            except Exception:
                pass

    # Build the candidate set: novel issues from the current findings...
    novel_candidates = [i for i in issues
                        if i.get("issue") not in KNOWN
                        and not _is_asset_issue(i)]

    # Phase FF (2026-05-22): partition data-placeholders (em-dash value gaps,
    # not text-fixable) out of the Claude path. Routed below to the worklist
    # WITHOUT a Claude call; only text-fixable findings hit the Claude budget.
    data_findings = [i for i in novel_candidates if _is_data_placeholder(i)]
    novel_candidates = [i for i in novel_candidates if not _is_data_placeholder(i)]

    # Phase r43-H (2026-05-29): stop the data-placeholder spin-loop. These
    # em-dash "— placeholder" findings skip the Claude path, so they never
    # accrued a false-positive count — and re-logged "data_placeholder_routed"
    # every cycle forever (128× on the homepage, 101× on dc-hub-media, 77× on
    # /pricing). They're benign cells that need a live-data binding, not a text
    # fix, and re-routing them hourly just makes the brain look busy-but-stuck.
    # Drop ones already confirmed as repeat non-fixables; each remaining route
    # bumps mark_false_positive() below, so after 3 cycles they fall into this
    # set and stop re-surfacing. r63: reuse the _suppressed_fp set computed
    # once above (was a redundant 2nd DB fetch).
    if _suppressed_fp:
        data_findings = [
            i for i in data_findings
            if (i.get("issue"), i.get("url") or "") not in _suppressed_fp
        ]

    # Phase SS (2026-05-14): drop confirmed false positives. An issue
    # Claude has REFUSED 3+ times isn't a real fixable placeholder —
    # re-attempting it just burns the hourly Claude budget. The 11
    # wasted `refused` cycles on the phantom /markets placeholder are
    # exactly what this prevents. r63: reuse _suppressed_fp (was a 3rd fetch).
    if _suppressed_fp:
        novel_candidates = [
            i for i in novel_candidates
            if (i.get("issue"), i.get("url") or "") not in _suppressed_fp
        ]

    # ...prioritized by persistence (most-stuck first). When the store is
    # unavailable we fall back to "first N in feed order" which matches
    # the pre-Phase-S behaviour.
    if _STORE_OK:
        try:
            stuck = _store.most_persistent_unfixed(min_count=2,
                                                    limit=BRAIN_MAX_LEARN * 3)
            stuck_keys = {(s.get("issue_label"), s.get("url") or "")
                          for s in stuck}
            def _stuck_score(i):
                key = (i.get("issue"), i.get("url") or "")
                if key not in stuck_keys: return (0, 0)
                # match seen_count back from the worklist row
                for s in stuck:
                    if (s.get("issue_label"), s.get("url") or "") == key:
                        return (1, int(s.get("seen_count", 0)))
                return (1, 0)
            novel_candidates.sort(key=_stuck_score, reverse=True)
        except Exception as e:
            print(f"[brain_v2_layer4] persistence sort failed: {e}", flush=True)

    novel = novel_candidates[:BRAIN_MAX_LEARN]

    results = []
    # Phase FF (2026-05-22): route data-placeholders to the worklist with an
    # honest outcome — the classification fix. No Claude call: these need a
    # data/binding fix (like the homepage field-name bug), not a text swap.
    for issue in data_findings:
        _log({"issue": issue.get("issue"), "url": issue.get("url"),
              "outcome": "data_placeholder_routed",
              "detail": "needs a live-data/binding fix, not a text substitution"})
        if _STORE_OK:
            try:
                _store.set_persistence_outcome(
                    issue.get("issue") or "", issue.get("url") or "",
                    "data_placeholder_routed")
                # r43-H: count each route as a "confirmed non-text-fixable"
                # so after min_refused (3) cycles it joins the suppressed set
                # above and stops re-surfacing — killing the spin-loop.
                _store.mark_false_positive(
                    issue.get("issue") or "", issue.get("url") or "")
            except Exception:
                pass
        results.append({"issue": issue.get("issue"),
                        "outcome": "data_placeholder_routed",
                        "note": "needs data binding, not text fix"})

    for issue in novel:
        snippet = _fetch_snippet(issue.get("url", ""))
        if not snippet:
            _log({"issue": issue.get("issue"), "outcome": "no_snippet"})
            results.append({"issue": issue.get("issue"), "outcome": "no_snippet"})
            continue
        text, err = _call_claude(_build_prompt(issue, snippet), _LEARN_SYSTEM)
        if err or not text:
            _log({"issue": issue.get("issue"), "outcome": f"api_error: {err}"})
            results.append({"issue": issue.get("issue"), "outcome": f"api_error: {err}"})
            continue
        # Parse JSON response (Claude often wraps in code fence)
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            _log({"issue": issue.get("issue"), "outcome": "non_json_response"})
            results.append({"issue": issue.get("issue"), "outcome": "non_json"})
            continue
        try:
            proposal = json.loads(m.group(0))
        except Exception as e:
            _log({"issue": issue.get("issue"), "outcome": f"parse_fail: {e}"})
            results.append({"issue": issue.get("issue"), "outcome": "parse_fail"})
            continue
        find = proposal.get("find", "")
        replace = proposal.get("replace", "")
        rationale = proposal.get("rationale", "")
        # Phase NN (2026-05-13): rescue short finds by expanding to the
        # enclosing HTML leaf element. Brain dashboard showed every
        # learning attempt failing with find_too_short — auto-expansion
        # turns bare "—" into "<div class=\"v\">—</div>" so legitimate
        # proposals can clear validation.
        find_pre_expand = find
        find = _auto_expand_find(find, snippet)
        ok, reason = _validate_proposal(find, replace)
        if not ok:
            # Empty-find is the prompt's explicit refusal contract — log
            # as "refused" so the dashboard doesn't mislabel it as a
            # validation failure (the brain DID the right thing).
            outcome_tag = "refused" if reason == "refused" else f"validation_fail: {reason}"
            _log({"issue": issue.get("issue"), "outcome": outcome_tag,
                  "find": find[:80], "replace": replace[:80],
                  "find_pre_expand": find_pre_expand[:40] if find_pre_expand != find else None})
            # Phase SS (2026-05-14): a `refused` outcome is Claude saying
            # "this isn't a real fixable issue" — record it so that after
            # 3 refusals the brain stops re-attempting it (see the
            # false-positive filter on novel_candidates above).
            if reason == "refused" and _STORE_OK:
                try:
                    _store.mark_false_positive(issue.get("issue") or "",
                                               issue.get("url") or "")
                except Exception:
                    pass
            results.append({"issue": issue.get("issue"), "outcome": outcome_tag})
            continue
        # Phase 300 (Phase R-3): 2-cycle approval gate. If the exact same
        # (find, replace) pair has been proposed before, increment its
        # approval_count and flip to approved=true at >= 2. master-heal only
        # consumes approved entries — single-shot Claude hallucinations stay
        # in the queue but never get auto-applied. Real recurring bugs cross
        # the threshold within 2 hourly cycles and become eligible.
        #
        # Phase S (2026-05-12): the (find, replace) lookup now goes against
        # the Postgres store via upsert_proposal(), which is atomic and
        # shared across all gunicorn workers. The in-memory list mirror is
        # kept for back-compat with anything that inspects _proposed_fixes
        # directly.
        proposal_entry = {
            "issue_label": issue.get("issue"),
            "find": find,
            "replace": replace,
            "rationale": rationale,
            "source_url": issue.get("url"),
            "model": BRAIN_MODEL,
        }

        # Phase GG (2026-05-14): Bundle 4B — rejection memory.
        # Skip if a human reviewer has rejected this issue+find combo
        # twice already in the last 30 days. Fully opt-in / try-wrapped
        # so existing Layer 4 logic continues if brain_learning isn't
        # available.
        try:
            from routes.brain_learning import (check_rejection_skip,
                                                bump_temporal,
                                                record_model_run,
                                                issue_hash as _ihash)
            if check_rejection_skip(issue.get("issue"), find):
                _log({"issue": issue.get("issue"),
                      "outcome": "skipped_repeat_rejection",
                      "find": find[:80],
                      "rejection_hash": _ihash(issue.get("issue"), find)})
                # Don't propose; count rejected attempt against model perf.
                record_model_run("layer4", BRAIN_MODEL,
                                 outcome="skipped_by_rejection_memory",
                                 notes=str(issue.get("issue") or '')[:120])
                continue
            # Bundle 4C — bump temporal pattern for this issue.
            bump_temporal(issue.get("issue") or '', issue.get("url") or '')
        except Exception:
            pass  # learning module optional

        stored_row = None
        if _STORE_OK:
            try:
                stored_row = _store.upsert_proposal(proposal_entry)
            except Exception as e:
                print(f"[brain_v2_layer4] upsert_proposal failed: {e}", flush=True)
                stored_row = None

        # Bundle 4D — record this model-call's outcome for model perf tracking.
        try:
            from routes.brain_learning import record_model_run
            record_model_run(
                "layer4", BRAIN_MODEL,
                outcome=("proposed" if stored_row else "no_store"),
                proposal_id=(stored_row.get("id") if stored_row else None),
                approved=(bool(stored_row.get("approved")) if stored_row else None))
        except Exception:
            pass

        if stored_row:
            count = int(stored_row.get("approval_count", 1))
            approved = bool(stored_row.get("approved"))
            outcome = "approval_count_incremented" if count > 1 else "proposed"
            _log({"issue": issue.get("issue"), "outcome": outcome,
                  "find": find[:80], "count": count, "approved": approved})
            if _STORE_OK:
                try:
                    _store.set_persistence_outcome(
                        issue.get("issue") or "",
                        issue.get("url") or "",
                        outcome)
                except Exception:
                    pass
            results.append({"issue": issue.get("issue"), "outcome": outcome,
                            "find": find[:60], "rationale": rationale[:120],
                            "approval_count": count, "approved": approved})
        else:
            # Store unavailable — use the legacy in-memory path so the
            # heal cycle still works on local dev or during a transient
            # DB outage.
            existing = next((e for e in _proposed_fixes
                             if e.get("find") == find
                             and e.get("replace") == replace), None)
            if existing:
                existing["approval_count"] = existing.get("approval_count", 1) + 1
                existing["last_seen_at"] = datetime.now(timezone.utc).isoformat()
                existing["approved"] = existing["approval_count"] >= 2
                _log({"issue": issue.get("issue"),
                      "outcome": "approval_count_incremented",
                      "count": existing["approval_count"],
                      "approved": existing["approved"]})
                results.append({"issue": issue.get("issue"),
                                "outcome": "reproposed",
                                "approval_count": existing["approval_count"],
                                "approved": existing["approved"]})
            else:
                entry = {**proposal_entry,
                         "proposed_at": datetime.now(timezone.utc).isoformat(),
                         "last_seen_at": datetime.now(timezone.utc).isoformat(),
                         "approval_count": 1, "approved": False}
                _proposed_fixes.append(entry)
                if len(_proposed_fixes) > _MAX_BUFFER:
                    _proposed_fixes.pop(0)
                _log({"issue": issue.get("issue"), "outcome": "proposed",
                      "find": find[:80]})
                results.append({"issue": issue.get("issue"), "outcome": "proposed",
                                "find": find[:60], "rationale": rationale[:120],
                                "approval_count": 1, "approved": False})

    accepted_total = (_store.count_proposals() if _STORE_OK
                      else len(_proposed_fixes))
    return jsonify(
        ok=True,
        cycle_at=datetime.now(timezone.utc).isoformat(),
        novel_count=len(novel),
        results=results,
        accepted_total=accepted_total,
        store_backed=_STORE_OK,
    ), 200


@brain_v2_bp.get("/api/v1/brain/proposed-fixes")
def proposed_fixes():
    """Master-heal workflow polls this to merge novel patterns into FIX_MAP.

    Phase 300 (Phase R-3): supports ?approved=true to return ONLY proposals
    that have crossed the 2-cycle approval threshold. master-heal.yml uses
    this filter to avoid auto-applying single-shot Claude hallucinations.
    Without the filter, returns everything so the QA dashboard can show
    both pending + approved.

    Phase S (2026-05-12): read from Postgres store when available so the
    workflow sees proposals from every gunicorn worker, not just the one
    that happens to handle the poll. In-memory fallback covers local dev.
    """
    approved_only = request.args.get("approved", "").lower() in ("true", "1", "yes")
    if _STORE_OK:
        try:
            proposals = _store.list_proposals(approved_only=approved_only, limit=200)
            return jsonify(
                as_of=datetime.now(timezone.utc).isoformat(),
                count=len(proposals),
                filter={"approved_only": approved_only},
                store_backed=True,
                proposals=proposals,
            ), 200
        except Exception as e:
            print(f"[brain_v2_layer4] proposed-fixes store read failed: {e}",
                  flush=True)
            # fall through to in-memory
    proposals = _proposed_fixes
    if approved_only:
        proposals = [p for p in proposals if p.get("approved")]
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len(proposals),
        filter={"approved_only": approved_only},
        store_backed=False,
        proposals=list(reversed(proposals)),  # newest first
    ), 200


@brain_v2_bp.get("/api/v1/brain/learning-log")
@_require_admin
def learning_log():
    """Recent learning attempts + outcomes (for the QA dashboard)."""
    if _STORE_OK:
        try:
            log = _store.list_log(limit=200)
            return jsonify(
                as_of=datetime.now(timezone.utc).isoformat(),
                count=len(log),
                store_backed=True,
                log=log,
            ), 200
        except Exception:
            pass
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len(_learning_log),
        store_backed=False,
        log=list(reversed(_learning_log)),
    ), 200


@brain_v2_bp.get("/api/v1/brain/persistence")
def persistence_worklist():
    """Phase S (2026-05-12): public view of the brain's stuck-issue worklist.

    Returns the issues the healer has surfaced repeatedly that the brain
    has NOT yet produced a successful proposal for. The /brain dashboard
    can show this so the team can see exactly what the system is stuck on.

    Phase OO (2026-05-13): augmented with heartbeat-derived stale-surface
    items. Heartbeat tracks 856 surfaces (every API + page) with explicit
    cap_h freshness windows. When a surface goes past its cap it's a
    high-signal issue the brain SHOULD be working on — far more
    actionable than the em-dash placeholder findings the healer
    surfaces. Heartbeat items are emitted alongside (not instead of) the
    healer findings so the brain has both signals.
    """
    persistence_items = []
    if _STORE_OK:
        try:
            min_count = int(request.args.get("min_count", "2"))
        except ValueError:
            min_count = 2
        persistence_items = _store.most_persistent_unfixed(
            min_count=min_count, limit=50)
    else:
        min_count = 2

    # Phase OO: fold in heartbeat-stale surfaces. We import the status
    # function directly rather than going out over HTTP — avoids the
    # self-call edge case where the brain endpoint hangs waiting on
    # its own server.
    heartbeat_items = []
    try:
        from routes.heartbeat import _status as _hb_status
        rows = _hb_status()
        # Convert each stale surface into the same item shape as
        # persistence rows so the dashboard renders them uniformly.
        now_iso = datetime.now(timezone.utc).isoformat()
        for r in rows or []:
            age = r.get("age_hours")
            cap = r.get("stale_after_hours") or 0
            if not isinstance(age, (int, float)) or not cap:
                continue
            if age <= cap:
                continue  # fresh — skip
            # Severity proxy: how many cycles past cap. seen_count
            # convention is "how many times the healer has noticed
            # this", and the brain prioritizes higher seen_count first.
            cycles_past = max(2, int(age / max(cap, 1)))
            heartbeat_items.append({
                "issue_label": "stale_surface",
                "url": r.get("surface", "?"),
                "seen_count": cycles_past,
                "first_seen_at": r.get("last_updated") or now_iso,
                "last_seen_at": now_iso,
                "last_outcome": (
                    f"untried · {age:.1f}h old, cap {cap}h"
                    if isinstance(age, (int, float)) else "untried"
                ),
                # Carry the heartbeat's own refresh hint so a future
                # repair pathway can call /api/v1/heartbeat/refresh.
                "refresh_func": r.get("refresh_func"),
                "source": "heartbeat",
            })
        # Sort by cycles_past descending so the most-stale leads.
        heartbeat_items.sort(key=lambda x: -x["seen_count"])
        # Cap so a totally cold deploy doesn't dump 856 rows.
        heartbeat_items = heartbeat_items[:25]
    except Exception as e:
        # Heartbeat is supplementary — never fail the endpoint over it.
        print(f"[brain_v2_layer4] heartbeat fold-in failed: {e}",
              file=sys.stderr)

    # Phase FF+directives (2026-05-22): operator directives LEAD the worklist.
    # A human-queued "fix X / build Y" outranks every auto-detected finding —
    # high synthetic seen_count keeps it at the top of any seen_count sort.
    directive_items = []
    if _STORE_OK:
        try:
            for d in _store.list_directives(status="open", limit=20):
                directive_items.append({
                    "issue_label": "operator_directive",
                    "url": d.get("target", ""),
                    "seen_count": 10000 + int(d.get("priority") or 100),
                    "first_seen_at": d.get("created_at"),
                    "last_seen_at": d.get("updated_at") or d.get("created_at"),
                    "last_outcome": (f"operator [{d.get('kind')}]: "
                                     f"{(d.get('directive') or '')[:200]}"),
                    "source": "operator_directive",
                    "directive_id": d.get("id"),
                    "kind": d.get("kind"),
                })
        except Exception as e:
            print(f"[brain_v2_layer4] directive fold-in failed: {e}",
                  file=sys.stderr)

    items = directive_items + list(persistence_items) + heartbeat_items
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        store_backed=_STORE_OK,
        min_count=min_count,
        count=len(items),
        items=items,
        directive_count=len(directive_items),
        persistence_count=len(persistence_items),
        heartbeat_stale_count=len(heartbeat_items),
        hint=(None if _STORE_OK else
              "DATABASE_URL not set — persistence tracking unavailable; "
              "heartbeat items still included"),
    ), 200


def _brain_age_min(val):
    """Minutes since an ISO/datetime value, or None."""
    if not val:
        return None
    try:
        t = val if isinstance(val, str) else val.isoformat()
        ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    except Exception:
        return None


def _cached_actionable_count() -> int:
    """Cheap read of the open actionable-findings backlog for the verdict.

    Phase r36 (2026-05-31): /api/v1/heal/findings is cache-served and never
    computes synchronously (the detectors are run by a background thread), so
    an in-process test-client GET here is a dict read, not a crawl — safe on
    this hot, public, single-replica endpoint. Returns 0 on any error or
    while the cache is still warming, so a transient miss can never flip the
    verdict to a false backlog state."""
    try:
        from flask import current_app
        with current_app.test_client() as _c:
            r = _c.get("/api/v1/heal/findings")
            if r.status_code != 200:
                return 0
            d = r.get_json() or {}
        if d.get("_warming_up"):
            return 0
        be = d.get("actionable_backend_issues") or []
        fe = d.get("actionable_frontend_issues") or []
        return len(be) + len(fe)
    except Exception:
        return 0


def compute_brain_verdict(has_api_key, run_age_min, stale_min,
                          pf_count, log_count, actionable_count=0):
    """The honest, unambiguous Layer-4 state — shared by
    /api/v1/brain/status AND the /brain dashboard so both tell the same
    story. Returns (verdict, verdict_detail).

    The recurring "why isn't the brain learning?" confusion comes from a
    dashboard showing 0/0/0, which LOOKS like failure but is almost
    always success: nothing broken => nothing to propose. Crucially,
    `stalled` fires ONLY on positive evidence (a heartbeat that's
    genuinely old) — never on the mere absence of the heartbeat field,
    which is what made the first cut of this verdict cry wolf right
    after deploy.

    Phase r36 (2026-05-31): `actionable_count` closes the detect→escalate
    gap. The verdict used to look ONLY at Layer-4's own counters (proposals
    + learning log) and would announce "healthy_quiet — the healer's
    findings are clean" while /api/v1/heal/findings was sitting on dozens of
    open actionable issues. The issues weren't text-fixable (they're
    backend/SEO/infra fixes that route to autopilot + Layer 5), so 0 Layer-4
    proposals really IS correct — but "findings are clean" was a lie that
    buried a real backlog. When a backlog exists, say so honestly instead.
    """
    if not has_api_key:
        return ("dormant",
                "ANTHROPIC_API_KEY is not set — Layer 4 is off. "
                "Set it in Railway env to activate.")
    # Positive evidence of a stall: heartbeat exists AND is old.
    if run_age_min is not None and run_age_min > 180:
        return ("stalled",
                f"The learn loop's last run was {run_age_min}m ago — the "
                f"brain cron is dropped or erroring. This is the only "
                f"state that needs a human.")
    # Recent heartbeat — trust it.
    if run_age_min is not None:
        if pf_count > 0 or (stale_min is not None and stale_min < 180):
            return ("healthy_working",
                    f"Running normally — last pass {run_age_min}m ago, "
                    f"{pf_count} proposal(s) in flight.")
        if actionable_count > 0:
            return ("healthy_backlog",
                    f"Layer 4 is running fine (last pass {run_age_min}m ago) "
                    f"and correctly produced 0 text-fix proposals — but there "
                    f"are {actionable_count} open actionable finding(s) that "
                    f"need backend/SEO/infra fixes, not HTML edits. These "
                    f"route to autopilot + Layer 5, not Layer 4. See "
                    f"/api/v1/heal/findings and /api/v1/brain/findings/triage.")
        return ("healthy_quiet",
                f"Healthy. Last pass {run_age_min}m ago found nothing "
                f"text-fixable — the healer's findings are clean, so 0 "
                f"proposals is the correct result, not a failure.")
    # No heartbeat yet. Do NOT cry "stalled" on a brand-new field — fall
    # back to log history for evidence the brain has been running.
    if log_count > 0:
        return ("healthy_quiet",
                "Healthy. The run-heartbeat is newly added and gets "
                "stamped on the next learn pass; existing log history "
                "shows the brain has been running. 0 proposals = clean "
                "findings, not a failure.")
    return ("warming_up",
            "No activity recorded yet — the brain hasn't completed a "
            "learn pass since deploy. Give it one cron cycle.")


# Alias: /api/v1/brain/assessment → forwards to the existing
# /api/v1/brain/self-assessment handler. The /brain dashboard footer
# advertises the short-form URL; previously that 404'd. (2026-05-23)
@brain_v2_bp.get("/api/v1/brain/assessment")
def brain_assessment_alias():
    """Alias for /api/v1/brain/self-assessment. Same payload, shorter URL."""
    from flask import redirect
    return redirect("/api/v1/brain/self-assessment", code=307)


# Brain error-class registry — what classes of error the brain knows
# how to recognize + remediate. See routes/brain_error_classes.py.
# Surfacing this on /brain demonstrates the brain's actual capability
# surface, not just the legacy Layer-4 placeholder loop. (2026-05-23)
@brain_v2_bp.get("/api/v1/brain/error-classes")
def brain_error_classes():
    """List the error CLASSES the brain can self-match + remediate."""
    try:
        from routes.brain_error_classes import summary as _summary
        return jsonify(_summary())
    except Exception as e:
        return jsonify({"error": str(e), "total_classes": 0, "classes": []}), 500


# #4 de-noise (r43-H, 2026-05-28): split the brain's work-queue into what it
# can actually FIX (code bugs + infra) vs unactionable business KPIs
# (funnel/conversion/dedup) that dominate by count. Read-only; merges the same
# get_last_*_findings() sources /api/v1/heal/findings uses, then classifies.
@brain_v2_bp.get("/api/v1/brain/findings/triage")
def brain_findings_triage():
    """De-noised view of the work-queue. Buckets: code_bug (with matched
    ErrorClass + auto_fixable flag), infra, data, business_kpi, unknown."""
    try:
        from routes.brain_error_classes import triage_findings
    except Exception as e:
        return jsonify({"error": f"classifier unavailable: {str(e)[:120]}"}), 500
    try:
        import dchub_self_heal as h
    except Exception as e:
        return jsonify({"error": f"self-heal module unavailable: {str(e)[:120]}"}), 200
    merged: dict = {}
    for fn_name in ("get_last_backend_findings", "get_last_funnel_findings",
                    "get_last_radar_findings", "get_last_html_findings",
                    "get_last_qa_findings", "get_last_asset_findings",
                    "get_last_api_contract_findings"):
        fn = getattr(h, fn_name, None)
        if not callable(fn):
            continue
        try:
            raw = fn() or {}
            for url, labels in raw.items():
                if isinstance(labels, dict):
                    merged.setdefault(url, {}).update(labels)
        except Exception:
            continue
    out = triage_findings(merged)
    out["source_findings"] = sum(len(v) for v in merged.values() if isinstance(v, dict))
    return jsonify(out)


@brain_v2_bp.get("/api/v1/brain/status")
def brain_status():
    """Public health check — proves the layer is loaded + reports activation.

    Phase Y (2026-05-12): added staleness watchdog. The dashboard at /brain
    was showing all zeros for "Learning attempts (24h)" and "Proposed fixes"
    after the user merged Phase R/S, which made it look broken. In reality
    there were just no novel patterns for Brain to learn from — the FIX_MAP
    auto-fixes known ones and Phase R-2's tightened prompt correctly refuses
    to alter typography. The watchdog distinguishes "healthy but quiet" from
    "broken" so the visible state has truthful meaning.

    Three new fields:
      stale_minutes_since_last_log  — time since any learn attempt
      last_log_at                   — timestamp of most recent attempt
      health                        — 'active' / 'quiet' / 'stale' / 'dormant'
    """
    if _STORE_OK:
        try:
            pf_count = _store.count_proposals()
            log_count = _store.count_log()
            recent = _store.list_log(limit=1)
            last_t = recent[0].get("t") if recent else None
        except Exception:
            pf_count = len(_proposed_fixes)
            log_count = len(_learning_log)
            last_t = _learning_log[-1].get("t") if _learning_log else None
    else:
        pf_count = len(_proposed_fixes)
        log_count = len(_learning_log)
        last_t = _learning_log[-1].get("t") if _learning_log else None

    stale_min = _brain_age_min(last_t)

    # Phase RR (2026-05-14): the heartbeat. last_run_at is stamped on
    # EVERY trigger_learn() call (incl. no-op passes). last_log_at only
    # moves when there's an actual learn attempt to log. Comparing the
    # two is what tells "healthy + quiet" apart from "stalled".
    last_run_at = None
    run_age_min = None
    if _STORE_OK:
        try:
            _m = _store.get_meta("last_run_at")
            if _m:
                last_run_at = _m.get("value")
                run_age_min = _brain_age_min(last_run_at)
        except Exception:
            pass

    # Legacy `health` field — kept so older dashboard code keeps
    # rendering. The new `verdict` is the truthful one.
    health = "dormant"
    if not ANTHROPIC_API_KEY:
        health = "dormant"
    elif stale_min is None:
        health = "active" if log_count > 0 else "dormant"
    elif stale_min < 90:
        health = "active"
    elif stale_min < 360:
        health = "quiet"
    else:
        health = "stale"

    actionable_count = _cached_actionable_count()
    verdict, verdict_detail = compute_brain_verdict(
        bool(ANTHROPIC_API_KEY), run_age_min, stale_min, pf_count, log_count,
        actionable_count=actionable_count)

    return jsonify(
        layer=4,
        loaded=True,
        active=bool(ANTHROPIC_API_KEY),
        actionable_findings_count=actionable_count,
        model=BRAIN_MODEL,
        max_learn_per_cycle=BRAIN_MAX_LEARN,
        store_backed=_STORE_OK,
        proposed_fixes_count=pf_count,
        learning_log_count=log_count,
        last_log_at=last_t,
        stale_minutes_since_last_log=stale_min,
        last_run_at=last_run_at,
        minutes_since_last_run=run_age_min,
        health=health,
        verdict=verdict,
        verdict_detail=verdict_detail,
        hint=(None if ANTHROPIC_API_KEY
              else "Set ANTHROPIC_API_KEY in Railway env to activate"),
    ), 200


# ── r30 (2026-05-24): brain transparency endpoints ───────────────
#
# Two small additions that close gaps the user kept asking about:
# "what model is the brain actually running on?" and "why is the
# brain proposing 0 fixes — is it broken or just being conservative?"

@brain_v2_bp.get("/api/v1/brain/model-tiers")
def brain_model_tiers():
    """What model is each brain tier currently using?

    Answers "is the brain on Opus 4.7?". Reflects env overrides
    (DCHUB_BRAIN_MODEL_INSPECTOR etc.) — what brain_model_for actually
    returns, not the hardcoded default. The brain status endpoint
    shows only the L4 model; this exposes the full per-tier picture.
    """
    try:
        from routes.brain_models import brain_model_summary
        summary = brain_model_summary()
    except Exception as e:
        return jsonify(ok=False, error=f"brain_models import: {type(e).__name__}"), 200
    # Also surface what THIS module's BRAIN_MODEL actually resolved to,
    # since L4 has its own resolution path that env overrides can affect.
    summary["_brain_v2_layer4_resolved"] = BRAIN_MODEL
    return jsonify(ok=True, **summary), 200


# r39 (2026-05-25): in-memory cache + dual-window FILTER query.
# Previously this endpoint composed 12 sequential COUNT queries
# (6 tables × {7d,30d}) and timed out at 30s under load — the L23
# audit's _audit_value_shipped received {} and reported "None/7d"
# falsely as a 'weak' dim. Two fixes:
#   1. Each table now answers BOTH windows in a single query via
#      COUNT(*) FILTER (WHERE ...) — 12 queries → 6.
#   2. 60s module-level cache prevents dashboard refreshes + audit
#      hits + cron probes from all triggering fresh composes.
_VALUE_SHIPPED_CACHE: dict = {"at": 0.0, "value": None}
_VALUE_SHIPPED_TTL = 60.0


@brain_v2_bp.get("/api/v1/brain/value-shipped")
def brain_value_shipped():
    """What has the brain actually shipped that made the site more valuable?

    Counts autonomous output across the brain's value-creation surfaces:
      - code proposals shipped (brain_proposed_fixes, status=approved+shipped)
      - autopilot actions completed (brain_autopilot_actions)
      - autonomous press releases written (auto_press_releases)
      - LinkedIn posts sent (auto_press_releases.linkedin_sent_at)

    Returns counts at 7d / 30d windows so operators can answer
    "is the brain making us more valuable than last week?"
    with one query instead of digging through 6 endpoints.

    Query: ?force=1 bypasses the 60s cache.
    """
    import datetime as _dt
    import time as _t

    # r39 cache check
    _force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    _now = _t.time()
    if (not _force and _VALUE_SHIPPED_CACHE["value"] is not None
            and (_now - _VALUE_SHIPPED_CACHE["at"]) < _VALUE_SHIPPED_TTL):
        cached = dict(_VALUE_SHIPPED_CACHE["value"])
        cached["served_from_cache"] = True
        cached["cache_age_seconds"] = round(_now - _VALUE_SHIPPED_CACHE["at"], 1)
        return jsonify(cached), 200

    c = None
    try:
        if _STORE_OK:
            try:
                c = _store._conn() if hasattr(_store, "_conn") else None
            except Exception:
                c = None
        if c is None:
            try:
                import psycopg2 as _pg
                c = _pg.connect(
                    os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL", ""),
                    sslmode="require",
                    connect_timeout=5,
                )
            except Exception:
                c = None
    except Exception:
        c = None

    if c is None:
        return jsonify(
            ok=False,
            error="DB unreachable",
            generated_at=_dt.datetime.utcnow().isoformat() + "Z",
        ), 200

    def _sql(query, default=None):
        """Run a COUNT query, swallow errors per-call. Returns None on
        any failure (table missing, column missing, permission denied)
        so the response can show "(table missing)" instead of crashing.
        """
        try:
            # Each query gets its own cursor so a failure aborts cleanly
            # without poisoning a shared transaction.
            with c.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
                return int((row or [0])[0] or 0) if row else default
        except Exception:
            try:
                # Postgres aborts the transaction on error — rollback so
                # subsequent counts on the same connection still work.
                c.rollback()
            except Exception:
                pass
            return default

    # r39 (2026-05-25): dual-window COUNT(*) FILTER consolidation.
    # One query per table now answers both 7d and 30d counts in a
    # single round-trip → 12 queries collapsed to 6. Returns a
    # tuple (count_7d, count_30d) so the outer aggregation logic
    # stays identical.
    def _dual(query: str, default=(0, 0)) -> tuple[int, int]:
        try:
            with c.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
                if not row:
                    return default
                return (int(row[0] or 0), int(row[1] or 0))
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            return default

    # Each query returns (c_7d, c_30d) via FILTER. INTERVAL is inlined
    # because no parameter binding is needed (constant strings).
    code_7d, code_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE COALESCE(applied_at, created_at)
                              >= NOW() - INTERVAL '7 days'
                              AND status IN ('shipped','approved','applied','merged')),
              COUNT(*) FILTER (WHERE COALESCE(applied_at, created_at)
                              >= NOW() - INTERVAL '30 days'
                              AND status IN ('shipped','approved','applied','merged'))
           FROM brain_proposed_fixes"""
    )
    apa_7d, apa_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE COALESCE(completed_at, started_at, created_at)
                              >= NOW() - INTERVAL '7 days'),
              COUNT(*) FILTER (WHERE COALESCE(completed_at, started_at, created_at)
                              >= NOW() - INTERVAL '30 days')
           FROM brain_autopilot_actions"""
    )
    pr_7d, pr_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE generated_at >= NOW() - INTERVAL '7 days'),
              COUNT(*) FILTER (WHERE generated_at >= NOW() - INTERVAL '30 days')
           FROM auto_press_releases"""
    )
    li_7d, li_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE linkedin_sent_at IS NOT NULL
                              AND linkedin_sent_at >= NOW() - INTERVAL '7 days'),
              COUNT(*) FILTER (WHERE linkedin_sent_at IS NOT NULL
                              AND linkedin_sent_at >= NOW() - INTERVAL '30 days')
           FROM auto_press_releases"""
    )
    # outreach: try media_outreach_log first, fall back to dchub_outreach_log.
    out_7d, out_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '7 days'),
              COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '30 days')
           FROM media_outreach_log"""
    )
    if out_7d == 0 and out_30d == 0:
        out_7d, out_30d = _dual(
            """SELECT
                  COUNT(*) FILTER (WHERE attempted_at >= NOW() - INTERVAL '7 days'),
                  COUNT(*) FILTER (WHERE attempted_at >= NOW() - INTERVAL '30 days')
               FROM dchub_outreach_log"""
        )
    fac_7d, fac_30d = _dual(
        """SELECT
              COUNT(*) FILTER (WHERE COALESCE(last_seen_at, first_seen_at)
                              >= NOW() - INTERVAL '7 days'),
              COUNT(*) FILTER (WHERE COALESCE(last_seen_at, first_seen_at)
                              >= NOW() - INTERVAL '30 days')
           FROM discovered_facilities"""
    )

    shipped_7d = {
        "code_fixes":            code_7d,
        "autopilot_actions":     apa_7d,
        "press_releases":        pr_7d,
        "linkedin_posts":        li_7d,
        "outreach_pitches":      out_7d,
        "facilities_discovered": fac_7d,
    }
    shipped_30d = {
        "code_fixes":            code_30d,
        "autopilot_actions":     apa_30d,
        "press_releases":        pr_30d,
        "linkedin_posts":        li_30d,
        "outreach_pitches":      out_30d,
        "facilities_discovered": fac_30d,
    }

    def _sum_nonnull(d):
        return sum(v for v in d.values() if isinstance(v, int))

    total_7d = _sum_nonnull(shipped_7d)
    total_30d = _sum_nonnull(shipped_30d)

    # Verdict ladder — what does this volume mean?
    if total_7d >= 14:    verdict = "high_output"
    elif total_7d >= 7:   verdict = "steady"
    elif total_7d >= 1:   verdict = "slow"
    else:                 verdict = "silent"

    try: c.close()
    except Exception: pass

    payload = dict(
        ok=True,
        verdict=verdict,
        total_shipped_7d=total_7d,
        total_shipped_30d=total_30d,
        shipped_7d=shipped_7d,
        shipped_30d=shipped_30d,
        generated_at=_dt.datetime.utcnow().isoformat() + "Z",
        purpose=(
            "Aggregate brain value-creation. Counts code proposals shipped, "
            "autopilot actions, press releases written, LinkedIn posts, "
            "and journalist pitches over 7d/30d. Single answer to 'is the "
            "brain alive and making us more valuable this week?'"
        ),
    )
    # r39 cache write
    _VALUE_SHIPPED_CACHE["at"] = _now
    _VALUE_SHIPPED_CACHE["value"] = payload
    return jsonify(payload), 200


@brain_v2_bp.get("/api/v1/brain/filter-summary")
def brain_filter_summary():
    """Why is the brain proposing N fixes? Counts learning_log outcomes
    by category — proves the filter chain is doing work even when
    proposed_fixes stays at 0 (the system being conservative is
    healthy; the metric being 0 because of a bug is not).

    Returns a count of each outcome from the recent learning log
    (capped to 200 entries to stay cheap).
    """
    log_entries: list = []
    try:
        if _STORE_OK:
            log_entries = _store.list_log(limit=200) or []
        else:
            log_entries = list(_learning_log)[-200:]
    except Exception:
        log_entries = list(_learning_log)[-200:]

    from collections import Counter
    by_outcome = Counter(
        (e.get("outcome") or "_unknown").split(":")[0]
        for e in log_entries
    )
    # Most-recent timestamp + total count for context.
    last_t = log_entries[-1].get("t") if log_entries else None
    return jsonify(
        ok=True,
        total_entries_scanned=len(log_entries),
        last_entry_at=last_t,
        outcomes=dict(by_outcome.most_common()),
        purpose=(
            "Filter telemetry. If proposed_fixes is 0 but outcomes "
            "contains data_placeholder_routed / refused / no_snippet, "
            "the brain is correctly filtering — not broken. Empty "
            "outcomes here would be the real red flag."
        ),
    ), 200
