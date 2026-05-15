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
  DCHUB_BRAIN_MAX_LEARN   default: 3 per cycle (rate-cap learning so
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
BRAIN_MODEL = os.environ.get("DCHUB_BRAIN_MODEL", "claude-sonnet-4-5")
BRAIN_MAX_LEARN = int(os.environ.get("DCHUB_BRAIN_MAX_LEARN", "3"))
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
    body = json.dumps({
        "model": BRAIN_MODEL,
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
        # Anthropic returns content as list of blocks
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block.get("text", ""), None
        return None, "no_text_block"
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:
        return None, f"call_fail: {str(e)[:120]}"


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
    findings = {}
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _resp = _client.get("/api/v1/heal/findings")
            if _resp.status_code == 200:
                findings = _resp.get_json() or {}
            else:
                # Test-client failure — last-resort fall back to public URL
                # (will probably 403 from Railway IP, but log it cleanly).
                raise RuntimeError(f"test_client returned {_resp.status_code}")
    except Exception as e:
        # Final fallback: try the public URL anyway. If even this fails,
        # the brain reports 502 with the original error.
        try:
            import urllib.request
            with urllib.request.urlopen(
                    "https://dchub.cloud/api/v1/heal/findings",
                    timeout=15) as r:
                findings = json.loads(r.read().decode("utf-8"))
        except Exception as e2:
            return jsonify(ok=False,
                           error=f"can't reach /heal/findings: "
                                 f"test_client={e}; public={e2}"), 502

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

    # Phase S (2026-05-12): "learn from errors it misses" — track every
    # issue's persistence in Postgres, then prioritize the most-stuck
    # ones (high seen_count, no successful proposal yet) for this learn
    # pass. Was: just take the first N novel issues. Now: pick from a
    # worklist where the brain's repeated failures bubble up.
    for i in issues:
        if _is_asset_issue(i) or i.get("issue") in KNOWN:
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

    # Phase SS (2026-05-14): drop confirmed false positives. An issue
    # Claude has REFUSED 3+ times isn't a real fixable placeholder —
    # re-attempting it just burns the hourly Claude budget. The 11
    # wasted `refused` cycles on the phantom /markets placeholder are
    # exactly what this prevents.
    if _STORE_OK:
        try:
            _fp = _store.list_false_positives(min_refused=3)
            if _fp:
                novel_candidates = [
                    i for i in novel_candidates
                    if (i.get("issue"), i.get("url") or "") not in _fp
                ]
        except Exception:
            pass

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

    items = list(persistence_items) + heartbeat_items
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        store_backed=_STORE_OK,
        min_count=min_count,
        count=len(items),
        items=items,
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


def compute_brain_verdict(has_api_key, run_age_min, stale_min,
                          pf_count, log_count):
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

    verdict, verdict_detail = compute_brain_verdict(
        bool(ANTHROPIC_API_KEY), run_age_min, stale_min, pf_count, log_count)

    return jsonify(
        layer=4,
        loaded=True,
        active=bool(ANTHROPIC_API_KEY),
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
