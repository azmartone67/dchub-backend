"""
Phase ZZZZ-brain-pr-opener (2026-05-18) — Brain L1: detect → propose PR.

Closes the gap the user called out: "brain detects but doesn't auto-fix."
This endpoint takes a brain finding (issue type + url + diagnostic detail)
and opens a PR via the GitHub REST API with a proposed fix.

For now we support 3 fix types — the patterns brain has caught multiple
times this month and can be safely templated. Each fix template produces
a tiny, reviewable diff:

  • blueprint_registered_but_not_serving — move late-line registration
    into the safe zone at ~line 1180 of main.py
  • cron_endpoint_unscheduled — add a JOBS entry to dchub-scheduler.py
  • shadowed_route — comment out the second registration with a TODO

Each PR opens against a fresh branch `brain/fix-{issue}-{ts}` and is
ASSIGNED + LABELED so the operator notices.
"""

import os
import re
import json
import time
import base64
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_pr_opener_bp = Blueprint("brain_pr_opener", __name__)

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
_GITHUB_REPO = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _gh(method: str, path: str, body=None):
    """Minimal GitHub REST client."""
    import requests
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dchub-brain-pr-opener/1.0",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    r = requests.request(method, url, headers=headers,
                         json=body, timeout=20)
    return r


def open_pr_exists(title: str) -> bool:
    """True if an OPEN PR with this exact title already exists. The brain's
    L5/L6 drafters re-propose the same ideas every cycle, piling up a 'draft
    graveyard' (28 stale drafts as of 2026-06-28). Call this before opening a
    draft so the same idea isn't re-drafted — the same dedupe fix that worked
    for the L23 capability proposals. Fail-open (returns False) on any error so
    a transient GitHub blip never silently blocks legitimate drafts."""
    t = (title or "").strip()
    if not t:
        return False
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code == 200:
            return any((p.get("title") or "").strip() == t for p in r.json())
    except Exception:
        pass
    return False


_TITLE_STOPWORDS = frozenset((
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with",
    "via", "into", "from", "by", "at", "as", "is", "are", "be", "that",
))


def _title_tokens(title: str) -> set:
    """Lowercased alphanumeric tokens of a PR title, bracket prefix and
    stopwords stripped — the comparison basis for fuzzy title dedup."""
    t = re.sub(r"^\[[^\]]*\]\s*", "", (title or "").lower())
    return set(re.findall(r"[a-z0-9]{3,}", t)) - _TITLE_STOPWORDS


def titles_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """True when two PR titles share >= threshold of their tokens (measured
    against the shorter title). The L6 planner paraphrases the same theme
    slightly differently every run ("Self-serve checkout for MCP unlock" vs
    "MCP unlock self-serve checkout flow"), so exact-match dedup missed
    essentially every duplicate — token overlap catches the paraphrases."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


# ── Semantic dedup layer (Phase rag-gap-prdedup, 2026-07-04) ────────────
# Token overlap catches word-shuffle paraphrases but misses true semantic
# rephrasings — each L6 run re-words the same theme with different words
# ("Trust-signal scoring for connector marketplace" vs "Security audit
# grade for MCP server"), so paraphrase dups still slipped past the 60%
# token gate. When the cheap token check does NOT flag, embed the
# candidate (title + optional summary) and the already-fetched open PR
# titles in ONE Cohere batch and compare by LOCAL cosine (0-1 scale).
# NEVER threshold on retrieve_context() scores for this — those are
# cross-encoder RERANK relevance scores whose absolute values sit around
# 0.05-0.3 even for excellent hits, so an absolute cutoff on them is
# meaningless. Raw-embedding cosine is the only sane 0-1 scale here.
# Fail-soft everywhere: any import/embed failure degrades to EXACTLY the
# pre-semantic (token-overlap) behavior.

def _pr_dup_cosine() -> float:
    """Cosine threshold for semantic PR-title dedup. Env-tunable via
    PR_DUP_COSINE (default 0.88)."""
    try:
        return float(os.environ.get("PR_DUP_COSINE", "0.88"))
    except Exception:
        return 0.88


def _pr_dup_embed_cap() -> int:
    """Max candidate titles embedded per dedup check (one batch). GitHub
    lists newest-first, so the cap keeps the freshest ~30 open drafts.
    Env-tunable via PR_DUP_EMBED_CAP (default 30)."""
    try:
        return max(1, int(os.environ.get("PR_DUP_EMBED_CAP", "30")))
    except Exception:
        return 30


def _cosine(a, b) -> float:
    """Plain local cosine similarity between two raw embedding vectors."""
    try:
        num = sum(x * y for x, y in zip(a, b))
        da = sum(x * x for x in a) ** 0.5
        db = sum(y * y for y in b) ** 0.5
        if not da or not db:
            return 0.0
        return num / (da * db)
    except Exception:
        return 0.0


def _strip_bracket_prefix(title: str) -> str:
    """Drop the leading '[brain-…]' tag before embedding — every brain
    draft shares it, and a long shared prefix inflates cosine between
    otherwise-unrelated titles."""
    return re.sub(r"^\[[^\]]*\]\s*", "", (title or "")).strip()


def semantic_title_dup(candidate_text: str, other_titles,
                       threshold: float | None = None) -> bool:
    """True when `candidate_text` embeds within cosine >= threshold of ANY
    title in `other_titles`. ONE batched _embed call ([candidate]+titles,
    input_type='search_document' for both — fine for symmetric
    comparison); cosine computed locally on the raw vectors. Caps the
    candidate list at PR_DUP_EMBED_CAP (~30). Fail-soft: returns False on
    ANY failure (no COHERE_API_KEY, import error, short/None response) so
    callers keep their exact pre-semantic behavior."""
    cand = (candidate_text or "").strip()
    titles = [_strip_bracket_prefix(t) for t in (other_titles or [])]
    titles = [t for t in titles if t][:_pr_dup_embed_cap()]
    if not cand or not titles:
        return False
    if threshold is None:
        threshold = _pr_dup_cosine()
    try:
        from routes.brain_rag import _embed
        vecs = _embed([cand] + titles, input_type="search_document")
        if not vecs or len(vecs) != len(titles) + 1:
            return False
        cv = vecs[0]
        return any(_cosine(cv, v) >= threshold for v in vecs[1:])
    except Exception:
        return False


def open_similar_pr_exists(title: str, prefix: str = "",
                           threshold: float = 0.6,
                           summary: str = "",
                           extra_titles=None) -> bool:
    """Fuzzy sibling of open_pr_exists: True if an OPEN PR whose title starts
    with `prefix` overlaps `title` by >= threshold tokens — and, when the
    token check does NOT flag (Phase rag-gap-prdedup 2026-07-04), if any
    open PR title embeds within PR_DUP_COSINE (default 0.88) of the
    candidate title(+summary). The cheap token check always runs FIRST; the
    one-batch semantic check only runs on a token miss.

    Optional kwargs (backwards-compatible — existing call sites unchanged):
      summary      — extra candidate context (e.g. the rec's spec_md head);
                     embedded alongside the title for the semantic pass.
      extra_titles — additional titles to dedup against with BOTH checks,
                     e.g. this week's already-PR'd rec titles from the L6
                     planner (which knows them from brain_strategic_recs).

    Fail-open like open_pr_exists — a GitHub blip never blocks a legitimate
    draft, and any embed failure degrades to exactly the token-overlap
    behavior; the supersede pass in expire_stale_draft_prs catches any dup
    that slips through."""
    t = (title or "").strip()
    if not t:
        return False
    candidates = []
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code == 200:
            for p in r.json():
                pt = (p.get("title") or "").strip()
                if prefix and not pt.startswith(prefix):
                    continue
                if titles_overlap(t, pt, threshold):
                    return True
                candidates.append(pt)
    except Exception:
        pass
    # Same checks against caller-supplied titles (this week's already-PR'd
    # rec titles) — token first, then pooled into the semantic batch.
    for et in (extra_titles or []):
        et = (et or "").strip()
        if not et:
            continue
        if titles_overlap(t, et, threshold):
            return True
        candidates.append(et)
    # Semantic layer — only reached when nothing token-flagged.
    cand_text = _strip_bracket_prefix(t)
    s = (summary or "").strip()
    if s:
        cand_text = f"{cand_text}\n{s[:500]}"
    return semantic_title_dup(cand_text, candidates)


def expire_stale_draft_prs(days: int = 5,
                           prefixes=("[brain-l5 draft]", "[brain-l6 strategic-draft]",
                                     "[brain-spec]"),
                           max_open: int | None = None) -> dict:
    """Auto-close brain DRAFT PRs whose title starts with one of the brain
    prefixes — the drain half of the draft-graveyard fix. Three passes:

    1. SUPERSEDE (2026-07-02): when two open drafts under the same prefix
       have overlapping titles (token overlap >= 60%), close the OLDER one
       as superseded. The L6 planner re-paraphrased the same themes across
       runs (two 5-PR bursts on 07-02 alone), and nothing drained the
       overlap until the 7-day expiry.
    2. EXPIRE: close survivors older than `days` (default dropped 14 → 5 on
       2026-07-02 — a strategic draft nobody touched in 5 days is stale, and
       the brain re-proposes anything still worth doing).
    3. CAP (2026-07-03): the brain opens ~8 spec/strategic drafts a DAY, so
       even a 5-day expiry lets ~40 accumulate before any drain. After
       supersede+expire, if more than `max_open` brain drafts remain, close
       the OLDEST beyond the cap — bounds the graveyard by count, not just
       age. Newest survive because they're the freshest phrasing of each
       theme; the brain re-proposes anything still worth doing.

    Only touches drafts (never a human-readied PR). Best-effort; returns a
    summary. Safe to cron daily."""
    import datetime as _dt
    if max_open is None:
        try:
            max_open = int(os.environ.get("BRAIN_DRAFT_PR_MAX_OPEN", "8"))
        except Exception:
            max_open = 8
    closed, superseded, capped, scanned = [], [], [], 0

    def _close(number: int, comment: str) -> bool:
        try:
            _gh("POST",
                f"/repos/{_GITHUB_REPO}/issues/{number}/comments",
                {"body": comment})
        except Exception:
            pass
        cr = _gh("PATCH", f"/repos/{_GITHUB_REPO}/pulls/{number}",
                 {"state": "closed"})
        return cr.status_code == 200

    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code != 200:
            return {"ok": False, "error": f"list {r.status_code}"}
        drafts = []
        for p in r.json():
            scanned += 1
            title = (p.get("title") or "")
            if not p.get("draft"):
                continue
            prefix = next((pre for pre in prefixes
                           if title.startswith(pre)), None)
            if prefix is None:
                continue
            try:
                created = _dt.datetime.fromisoformat(
                    (p.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            drafts.append((created, p["number"], title, prefix))

        # Pass 1 — supersede: newest-first so the freshest phrasing of each
        # theme survives and every older overlapping draft closes against it.
        kept = []   # (created, number, title, prefix)
        for created, number, title, prefix in sorted(drafts, reverse=True):
            winner = next((k for k in kept
                           if k[3] == prefix and titles_overlap(title, k[2])),
                          None)
            if winner is None:
                kept.append((created, number, title, prefix))
                continue
            if _close(number, (f"🤖 Auto-closed by the brain draft-PR janitor: "
                               f"superseded by the newer overlapping draft "
                               f"#{winner[1]} ({winner[2]!r}).")):
                superseded.append(number)
            else:
                kept.append((created, number, title, prefix))

        # Pass 2 — expire survivors older than the TTL.
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        survivors = []
        for created, number, title, prefix in kept:
            if created < cutoff:
                if _close(number, (f"🤖 Auto-closed by the brain draft-PR expiry: "
                                   f"draft sat unactioned for {days}+ days. The "
                                   f"brain re-proposes anything still worth doing.")):
                    closed.append(number)
            else:
                survivors.append((created, number, title, prefix))

        # Pass 3 — count-cap: keep only the `max_open` NEWEST brain drafts;
        # close the oldest beyond it so a burst can't accumulate for `days`.
        if max_open is not None and max_open >= 0 and len(survivors) > max_open:
            # newest-first; everything past the cap index is closed
            over = sorted(survivors, reverse=True)[max_open:]
            for created, number, title, prefix in over:
                if _close(number, (f"🤖 Auto-closed by the brain draft-PR cap: more "
                                   f"than {max_open} brain drafts were open, so the "
                                   f"oldest were drained. The brain re-proposes "
                                   f"anything still worth doing.")):
                    capped.append(number)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"ok": True, "scanned": scanned, "closed": closed,
            "closed_count": len(closed), "superseded": superseded,
            "superseded_count": len(superseded), "capped": capped,
            "capped_count": len(capped), "older_than_days": days,
            "max_open": max_open}


def _get_default_branch_sha() -> str | None:
    r = _gh("GET", f"/repos/{_GITHUB_REPO}/git/refs/heads/main")
    if r.status_code != 200: return None
    return ((r.json() or {}).get("object") or {}).get("sha")


def _get_file(path: str, ref: str = "main") -> tuple[str | None, str | None]:
    """Returns (content_decoded, sha). None if file doesn't exist."""
    r = _gh("GET", f"/repos/{_GITHUB_REPO}/contents/{path}?ref={ref}")
    if r.status_code != 200: return None, None
    j = r.json() or {}
    content_b64 = (j.get("content") or "").replace("\n", "")
    try:
        return base64.b64decode(content_b64).decode("utf-8"), j.get("sha")
    except Exception:
        return None, None


def _create_branch(branch_name: str, from_sha: str) -> bool:
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/git/refs",
            {"ref": f"refs/heads/{branch_name}", "sha": from_sha})
    return r.status_code in (200, 201)


def _commit_file(path: str, content: str, message: str,
                 branch: str, sha: str | None) -> bool:
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha: body["sha"] = sha
    r = _gh("PUT", f"/repos/{_GITHUB_REPO}/contents/{path}", body)
    return r.status_code in (200, 201)


def _open_pr(title: str, head: str, body: str) -> dict | None:
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls",
            {"title": title, "head": head, "base": "main", "body": body})
    if r.status_code not in (200, 201): return None
    return r.json()


# ── Spec-PR condition fingerprint (Phase spec-lifecycle, 2026-07-17) ────
# The exact-title dedup above can't catch the SAME condition arriving as two
# different items: #1632 (agenda #100107) and #1634 (prop #100040) were the
# same "[data_coverage] 311 DCPI markets…" condition filed twice in ONE
# MINUTE, because the titles embed kind + item_id. Reading the source rows'
# STAMPED fingerprints doesn't help either — the agenda pipeline hashes its
# title while the enhancer hashes its signal, so those two rows carry
# DIFFERENT stamps for the same condition. So the filer computes its OWN
# fingerprint from its own inputs (heading/directive), stamps it into the PR
# body as an HTML comment, and skips filing when an OPEN spec PR already
# carries the same stamp. REUSES routes.brain_proposal_dedup (2026-07-16) —
# never reimplement the fingerprint.
_SPEC_TITLE_PREFIX = "[brain-spec]"
_SPEC_FP_RE = re.compile(r"<!--\s*fingerprint:([0-9a-f]{8,64})\s*-->")


def spec_condition_fingerprint(heading: str, directive: str = ""):
    """Condition fingerprint for a spec PR, from the FILER's inputs. Number-
    stripped (via brain_proposal_dedup.norm_condition inside), so live-count
    drift hashes identically. None on any failure — dedup fails OPEN so a
    hiccup can only ever cost a duplicate PR, never block filing."""
    try:
        from routes.brain_proposal_dedup import condition_fingerprint
        return condition_fingerprint("brain-spec", heading or directive,
                                     directive)
    except Exception:
        return None


def landed_spec_with_fingerprint(fp):
    """Filename of an already-LANDED spec in docs/brain-proposals/ whose body
    carries <!-- fingerprint:fp -->, else None.

    ★2026-08-07 (audit SH52-041, the duplicate-spec treadmill): the filer
    dedup only consulted OPEN PRs, so every owner-merge cleared the dedup key
    and the next investigation re-filed the same condition — the anon
    caller_tier finding was filed and merged SIX times (inv-100025..100039)
    while the fix never landed. Landed spec docs embed the fingerprint on
    line 1 and the deployed image carries the repo, so this is a local scan:
    no GitHub call, no rate limit, and merged history IS the docs tree.
    Fail-open: None on any error.

    ★★★ 2026-08-16 — THIS HAS NEVER DEMONSTRABLY WORKED IN PRODUCTION, and it
    took nine days to notice because the denominator was wrong. 58 spec docs
    landed after it shipped and only 2 were duplicates, which reads like a 97%
    success rate. It is not a rate at all: a dedup can only work when there is
    something to dedup against, and there were exactly **2 such occasions**.
    It caught **0 of 2**. The 56 novel filings never consulted it.

        inv-100145 (08-15) — fp ea39ea9e…, already landed as inv-100075 (08-10)
        inv-100146 (08-15) — fp cb1561c0…, already landed as inv-100079 (08-10)

    Verified not-the-cause, each against the worker's actual running commit
    4cb4738f: the function was present, the call was wired, and the stamped doc
    was in the tree. So the miss is a RUNTIME fact this function cannot report,
    because both excepts swallowed it and the miss path was indistinguishable
    from an honest "no match". That silence is the defect being fixed here —
    the scan now says what it saw, and a GitHub check backs it up so the answer
    no longer rests on a filesystem assumption that was never tested.
    """
    if not fp:
        return None
    scanned = 0
    d = ""
    try:
        d = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "brain-proposals")
        needle = "<!-- fingerprint:%s -->" % fp
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"):
                continue
            scanned += 1
            try:
                with open(os.path.join(d, name), encoding="utf-8",
                          errors="replace") as f:
                    # Stamp is on line 1 by construction (survives body
                    # truncation) — read a small head, not whole files.
                    if needle in f.read(600):
                        return name
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        # ★ LOUD. A scan that cannot read its own corpus must never be
        # mistaken for a scan that found nothing — that is the whole reason
        # this went unnoticed for nine days.
        logger.warning("[spec-dedup] LANDED SCAN FAILED dir=%s exists=%s "
                       "err=%s — filing will proceed UNDEDUPED",
                       d, os.path.isdir(d) if d else "?", str(e)[:160])
        return None
    if scanned == 0:
        logger.warning("[spec-dedup] LANDED SCAN READ 0 DOCS dir=%s exists=%s "
                       "— the corpus is missing from this image, so every "
                       "landed condition looks novel", d, os.path.isdir(d))
    else:
        logger.info("[spec-dedup] landed scan: %d docs, no match for %s",
                    scanned, fp[:12])
    return None


def merged_spec_pr_with_fingerprint(fp):
    """Number of a MERGED [brain-spec] PR carrying <!-- fingerprint:fp -->.

    ★ The filesystem-independent twin of landed_spec_with_fingerprint. The
    local scan is cheaper and is still tried first, but it rests on an
    assumption about the deployed image that the 0-of-2 record above shows was
    never verified. GitHub is the other copy of the same fact, reached the same
    way the OPEN-PR check already reaches it, so a wrong assumption about the
    container can no longer cost a duplicate.

    Fail-open: None on any error — a dedup that blocks proposing when GitHub is
    down would be a worse failure than a duplicate spec.

    ★ 2026-08-17: two passes. The pulls list reads only the 100 most-recently-
    updated CLOSED PRs — agenda-41's spec merged in July, sat far outside that
    horizon, and its condition re-filed as agenda-100198 anyway. Pass 2 is
    issue search, which is horizon-free and indexes PR bodies (live-verified:
    the exact query returns #2530/#2693 for fp ea39ea9e…). Split of labor:
    search indexing lags by minutes, so the recent-100 pass owns the fresh
    case; pre-07-17 spec PRs never carried a body stamp (#2762 backfilled the
    DOCS only), so that legacy class remains the local scan's job.
    """
    if not fp:
        return None
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls"
                       "?state=closed&per_page=100&sort=updated&direction=desc")
        if r.status_code != 200:
            logger.warning("[spec-dedup] merged-PR check HTTP %s — trying "
                           "the deep search anyway", r.status_code)
        else:
            for p in r.json():
                if not (p.get("title") or "").startswith(_SPEC_TITLE_PREFIX):
                    continue
                if not p.get("merged_at"):
                    continue  # closed-unmerged is a REJECTION, never a dedup hit
                m = _SPEC_FP_RE.search(p.get("body") or "")
                if m and m.group(1) == fp:
                    return p.get("number")
    except Exception as e:  # noqa: BLE001
        logger.warning("[spec-dedup] merged-PR check failed: %s", str(e)[:160])
    try:
        from urllib.parse import quote as _quote
        r = _gh("GET", "/search/issues?q=" + _quote(
            f'repo:{_GITHUB_REPO} is:pr is:merged "fingerprint:{fp}"',
            safe=""))
        if r.status_code != 200:
            logger.warning("[spec-dedup] deep merged-PR search HTTP %s — "
                           "filing will proceed on the shallow checks alone",
                           r.status_code)
            return None
        for it in (r.json() or {}).get("items", []):
            if "pull_request" not in it:  # issue, not a PR
                continue
            # re-verify the stamp — never trust the phrase match alone
            if f"fingerprint:{fp}" in (it.get("body") or ""):
                return it.get("number")
    except Exception as e:  # noqa: BLE001
        logger.warning("[spec-dedup] deep merged-PR search failed: %s",
                       str(e)[:160])
    return None


def open_spec_pr_with_fingerprint(fp):
    """Number of an OPEN [brain-spec] PR whose body carries
    <!-- fingerprint:fp -->, else None. One GitHub list call (the pulls list
    payload includes each PR's body). Fail-open: None on any error."""
    if not fp:
        return None
    try:
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/pulls?state=open&per_page=100")
        if r.status_code != 200:
            return None
        for p in r.json():
            if not (p.get("title") or "").startswith(_SPEC_TITLE_PREFIX):
                continue
            m = _SPEC_FP_RE.search(p.get("body") or "")
            if m and m.group(1) == fp:
                return p.get("number")
    except Exception:
        pass
    return None


def _gate_verdict_md(verdict: dict) -> str:
    """Markdown 'Escalation gate verdict' section embedded in a gated spec PR
    body (r-escalation-ladder, docs/brain-pr-escalation-gates.md) — the gate
    verdict + confidence + evidence summary the reviewer needs to audit WHY
    this condition auto-escalated. Wording deliberately avoids the substance
    gate's fix-claim verbs (r-spec-honesty). '' on any malformed verdict."""
    try:
        lines = [
            "## Escalation gate verdict",
            "",
            "_Auto-escalated via the evidence-gated ladder "
            "(docs/brain-pr-escalation-gates.md). The gate governs PR "
            "creation only — a human still merges or discards._",
            "",
            f"- **State:** `{verdict.get('state')}`",
            f"- **Confidence:** {verdict.get('confidence')} "
            f"(threshold {verdict.get('threshold')})",
        ]
        for name, g in (verdict.get("gates") or {}).items():
            mark = "PASS" if (g or {}).get("passed") else "FAIL"
            lines.append(f"- gate `{name}`: **{mark}** — "
                         f"{(g or {}).get('reason', '')}")
        hb = verdict.get("hard_blocks") or []
        lines.append("- hard blocks: "
                     + ("none" if not hb
                        else ", ".join(f"`{(b or {}).get('name', '?')}`"
                                       for b in hb)))
        ins = verdict.get("inputs") or {}
        ev = ", ".join(ins.get("evidence") or []) or "none"
        lines.append(
            f"- evidence summary: kinds [{ev}] · "
            f"repeat_count {ins.get('repeat_count')} · "
            f"distinct_sources {ins.get('distinct_sources')} · "
            f"expected_improvement {ins.get('expected_improvement')} · "
            f"recent_5xx_rate {ins.get('recent_5xx_rate')}")
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""


def open_spec_pr(directive: str, heading: str = "", kind: str = "item",
                 item_id=0, label: str = "", gate_verdict=None) -> dict:
    """r-brain-loop (2026-06-30): the actuator FALLBACK that turns an approval
    into a shippable artifact. When draft_and_open_pr REFUSES a directive (it's a
    'build X / instrument Y / gather Z' PLAN, not a single-file find/replace), this
    captures the APPROVED directive as a DRAFT spec PR — a markdown file under
    docs/brain-proposals/ with the recommendation + a human checklist. It adds a
    DOC only (zero code execution), opens as a draft, and a human merges — so the
    approval becomes a visible, trackable, human-implementable PR instead of
    'recorded only'. Inherits can_open_pr() (kill switch + daily cap), dedups by
    title. NEVER raises — always returns a dict.

    r-escalation-ladder (2026-07-18): `gate_verdict` — the evaluate_escalation()
    verdict (routes/brain_fix_gates, spec docs/brain-pr-escalation-gates.md)
    from an EVIDENCE-GATED caller. When provided it is ENFORCED here too
    (defense in depth): anything short of state='draft_pr' returns gated=True
    without touching GitHub, and a passing verdict is EMBEDDED in the PR body
    (confidence + per-gate reasons + evidence summary) so the reviewer sees why
    it auto-escalated. Callers without a verdict (human-approved directives)
    keep the pre-ladder behavior unchanged. The human-merge requirement is
    untouched either way — this gates PR CREATION only."""
    directive = (directive or "").strip()
    if not directive:
        return {"ok": False, "acted": False, "error": "empty directive"}
    if gate_verdict is not None and not (
            isinstance(gate_verdict, dict)
            and gate_verdict.get("state") == "draft_pr"):
        _st = (gate_verdict.get("state")
               if isinstance(gate_verdict, dict) else None)
        _hb = ([b.get("name") for b in (gate_verdict.get("hard_blocks") or [])]
               if isinstance(gate_verdict, dict) else [])
        return {"ok": True, "acted": False, "gated": True,
                "gate_state": _st, "hard_blocks": _hb,
                "note": "escalation gate verdict below draft_pr — the "
                        "condition stays agenda-only"}
    try:
        from routes.brain_guardrails import can_open_pr
        ok_gate, why = can_open_pr()
        if not ok_gate:
            return {"ok": False, "acted": False, "error": "autonomy_gate_closed", "reason": why}
    except Exception:
        pass
    import re as _re, datetime as _dt
    _slug = (_re.sub(r"[^a-z0-9]+", "-", (heading or directive)[:48].lower())
             .strip("-") or "proposal")
    title = f"[brain-spec] {kind} #{item_id}: {(heading or directive)[:70]}"
    try:
        if open_pr_exists(title):
            return {"ok": True, "acted": False, "note": "spec PR already open (dedup)"}
    except Exception:
        pass
    # Phase spec-lifecycle (2026-07-17): condition-fingerprint dedup — the
    # same condition arriving as a DIFFERENT item (agenda vs prop) must not
    # file twice. Fail-open for the DEDUP LOOKUPS only: fp=None skips them.
    # Filing itself is NOT fail-open any more — an unstamped doc is refused
    # at the commit gate below (2026-08-17).
    fp = spec_condition_fingerprint(heading, directive)
    dup = open_spec_pr_with_fingerprint(fp)
    if dup:
        return {"ok": True, "acted": False, "dup_pr": dup, "fingerprint": fp,
                "note": f"same-condition spec PR #{dup} already open "
                        f"(fingerprint dedup)"}
    # ★SH52-041: a MERGED spec is a stronger dedup hit than an open one — the
    # condition is already specced and approved; filing again is the
    # 6x-duplicate treadmill. Route to implementation, not re-investigation.
    landed = landed_spec_with_fingerprint(fp)
    if landed:
        return {"ok": True, "acted": False, "landed_spec": landed,
                "fingerprint": fp, "awaiting_implementation": True,
                "note": f"condition already specced and MERGED as "
                        f"docs/brain-proposals/{landed} — needs an "
                        f"implementation, not another spec (landed-spec "
                        f"fingerprint dedup, 2026-08-07)"}
    # ★ 2026-08-16: second opinion, filesystem-independent. The local scan
    # above is 0-for-2 in production and its misses were silent, so a single
    # source of truth for "already specced" is not good enough.
    merged_pr = merged_spec_pr_with_fingerprint(fp)
    if merged_pr:
        logger.warning("[spec-dedup] LOCAL SCAN MISSED a condition the merged"
                       "-PR check caught (fp=%s, PR #%s) — the docs corpus in "
                       "this image is not answering", fp[:12], merged_pr)
        return {"ok": True, "acted": False, "landed_spec_pr": merged_pr,
                "fingerprint": fp, "awaiting_implementation": True,
                "note": f"condition already specced and MERGED as PR "
                        f"#{merged_pr} — needs an implementation, not another "
                        f"spec (merged-PR fingerprint dedup, 2026-08-16)"}
    base = _get_default_branch_sha()
    if not base:
        return {"ok": False, "acted": False, "error": "no base sha"}
    branch = f"brain-spec/{kind}-{item_id}-{_slug}"[:90]
    path = f"docs/brain-proposals/{kind}-{item_id}-{_slug}.md"
    content = (
        # First line so the stamp survives the body[:4000] truncation — the
        # filer dedup + spec-PR janitor both grep open-PR bodies for it.
        (f"<!-- fingerprint:{fp} -->\n" if fp else "")
        # r-spec-honesty (2026-07-18): the SPEC-ONLY marker is the substance
        # gate's authoritative "honest scaffold" declaration. The old body
        # tripped the gate's fix-claim regex (\bclos(e|es|ed)\b …) via its own
        # "or close this PR" disclaimer — every spec PR self-flagged as a
        # scaffold CLAIMING a fix and blocked as a required check (the user
        # was manually bypassing them). Wording below deliberately avoids
        # close/fix/resolve; the marker makes intent machine-readable.
        + "**SPEC-ONLY** — this PR changes no running code and is not a fix; "
        "it captures an approved recommendation as an implementable spec.\n\n"
        + f"# Brain proposal — {(heading or directive[:80])}\n\n"
        f"> Auto-captured from an **approved** brain {kind} item (#{item_id}). The brain's\n"
        f"> recommendation couldn't be expressed as a single-file edit, so it's filed here\n"
        f"> as a spec for a human to implement (or discard). **Draft PR — a human merges.**\n\n"
        f"_Filed {_dt.datetime.utcnow().isoformat()}Z · {label}_\n\n"
        # r-escalation-ladder (2026-07-18): a gated caller's verdict rides in
        # the body — confidence, per-gate reasons, evidence summary.
        + (_gate_verdict_md(gate_verdict) if isinstance(gate_verdict, dict)
           else "")
        + f"## The approved recommendation\n\n{directive}\n\n"
        f"## Human checklist\n\n"
        f"- [ ] Confirm this is still worth doing\n"
        f"- [ ] Scope it to a concrete change (file(s) + approach)\n"
        f"- [ ] Implement + verify\n"
        f"- [ ] Or discard this PR if superseded / not worth it\n"
    )
    # ★ 2026-08-17 — writer-side twin of tests/test_brain_heal_fix_learn_
    # rewire.py::test_every_landed_spec_carries_a_fingerprint_stamp. That
    # REQUIRED check scans the live docs/brain-proposals corpus, so ONE
    # unstamped doc landing turns main red and blocks every PR (the #2745
    # class). fp is fail-open (None whenever the fingerprint helper errors),
    # and the template only stamps when fp is truthy — so a helper outage
    # used to file an UNSTAMPED doc: invisible to all three dedup passes
    # forever (the 6x-treadmill class, inv-100025..100039) and a red-main
    # landmine. Refuse instead: the condition stays agenda-only and refiles
    # on a later cycle once fingerprints answer again. Checked on
    # content[:600] because that is the exact window every scanner reads
    # (landed scan, the corpus test, _some_landed_fingerprint) — a stamp
    # below that fold would pass a naive full-body search and still be
    # invisible to dedup.
    if not _SPEC_FP_RE.search(content[:600]):
        logger.error("[spec-filer] REFUSING unstamped spec doc (fp=%r, "
                     "kind=%s, item=%s) — an unstamped landed doc is dedup-"
                     "invisible and turns the corpus stamp check red on main",
                     fp, kind, item_id)
        return {"ok": False, "acted": False, "error": "unstamped_spec_refused",
                "fingerprint": fp,
                "note": "no fingerprint stamp in the doc's first 600 chars — "
                        "filing would land a doc invisible to dedup and turn "
                        "required unit-tests red on main; retry after the "
                        "fingerprint helper recovers"}
    if not _create_branch(branch, base):
        return {"ok": False, "acted": False, "error": "create_branch failed"}
    if not _commit_file(path, content, f"brain-spec: {(heading or directive)[:60]}",
                        branch, None):
        return {"ok": False, "acted": False, "error": "commit_file failed"}
    # Open as a DRAFT PR (draft=true so it can't be merged without a human flip).
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls",
            {"title": title, "head": branch, "base": "main", "draft": True,
             "body": content[:4000]})
    if r.status_code not in (200, 201):
        return {"ok": False, "acted": False, "error": f"open_pr failed ({r.status_code})"}
    pr = r.json() or {}
    return {"ok": True, "acted": True, "spec_pr": True,
            "pr": {"number": pr.get("number"), "url": pr.get("html_url")}}


# ─── Fix templates ──────────────────────────────────────────────────────

def _fix_blueprint_silent_failure(finding: dict) -> tuple[str | None, str | None, str]:
    """For blueprint_registered_but_not_serving — relocate the import +
    register_blueprint pair into the safe zone after weekly_digest_bp.

    Returns (file_path, proposed_new_content, diff_summary). Returns None
    content if it can't safely automate the fix.
    """
    # Extract blueprint var name from finding.url like "main.py: register_blueprint(foo_bp)"
    m = re.search(r"register_blueprint\((\w+)\)", finding.get("url", ""))
    if not m: return None, None, "Could not parse blueprint name from finding"
    bp_var = m.group(1)

    main_py, _sha = _get_file("main.py")
    if not main_py: return None, None, "Could not read main.py"

    # Find any existing try/except wrap that registers this bp
    pattern = re.compile(
        r"try:\s*\n\s*from routes\.(\w+) import " + re.escape(bp_var)
        + r"\s*\n\s*app\.register_blueprint\(" + re.escape(bp_var)
        + r"\)\s*\n(?:\s*except[^\n]*\n(?:\s*[^\n]*\n)*?)?",
        re.MULTILINE)
    found = pattern.search(main_py)
    if not found:
        return None, None, f"Could not locate try/except for {bp_var} in main.py"
    module = found.group(1)

    # Find the safe-zone anchor (the weekly_digest_bp block)
    safe_anchor = ("    try:\n"
                   "        from routes.weekly_digest import weekly_digest_bp\n"
                   "        app.register_blueprint(weekly_digest_bp)")
    if safe_anchor not in main_py:
        return None, None, "Could not find safe-zone anchor"

    # Build the replacement
    new_block = (
        f"\n    # Phase brain-auto-relocate (2026-05-18): moved here by\n"
        f"    # brain_pr_opener after blueprint_registered_but_not_serving fired.\n"
        f"    try:\n"
        f"        from routes.{module} import {bp_var}\n"
        f"        app.register_blueprint({bp_var})\n"
        f"    except Exception as _bp_relo:\n"
        f"        import logging\n"
        f"        logging.getLogger(__name__).warning('{bp_var} wiring failed: %s', _bp_relo)\n"
    )
    new_main = main_py.replace(safe_anchor, safe_anchor + new_block, 1)
    # Remove the old (broken) registration block
    new_main = new_main.replace(found.group(0), "", 1)

    summary = (f"Moved `from routes.{module} import {bp_var}` + "
               f"`app.register_blueprint({bp_var})` from line ~unknown "
               f"(late-line zone) to ~line 1180 (safe zone next to "
               f"weekly_digest_bp).")
    return "main.py", new_main, summary


def _fix_generic_find_replace(finding: dict) -> tuple[str | None, str | None, str]:
    """Generic, deterministic single-file edit: apply an exact string
    find→replace to a named file. This is the same primitive Layer 4's
    healer already produces (brain_proposed_fixes.find/replace), now able to
    open a review PR for ANY file — not just main.py.

    Finding fields:
      file     — repo-relative path (required)
      find     — exact substring to replace (required, must be present + unique)
      replace  — replacement text (required; may be empty for deletion)

    Safety: refuses if `find` is missing, absent from the file, or appears
    more than once (ambiguous). Path is constrained to the repo (no '..',
    no leading '/'). PR is review-gated — humans merge.
    """
    # ★ Validate the RAW path BEFORE normalising it. `.lstrip("/")` used to run
    #   first, which made the `startswith("/")` guard below unreachable —
    #   "/etc/passwd" was silently rewritten to "etc/passwd" and accepted as
    #   repo-relative. Harmless in practice only because the read then 404s; the
    #   check was still doing nothing. Order restored.
    path = (finding.get("file") or "").strip()
    find = finding.get("find")
    replace = finding.get("replace", "")
    if not path or find is None:
        return None, None, "generic_find_replace needs 'file' and 'find'"
    if ".." in path or path.startswith("/"):
        return None, None, f"unsafe path: {path}"
    if not find:
        return None, None, "'find' must be a non-empty string"
    content, _sha = _get_file(path)
    if content is None:
        return None, None, f"could not read {path}"
    n = content.count(find)
    if n == 0:
        return None, None, f"'find' string not present in {path}"
    if n > 1:
        return None, None, f"'find' appears {n}× in {path} — ambiguous, refused"
    new_content = content.replace(find, replace, 1)
    if new_content == content:
        return None, None, "no-op edit (find == replace)"
    summary = (f"Applied a single exact find→replace in `{path}` "
               f"({len(find)}→{len(replace)} chars).")
    return path, new_content, summary


_FIX_HANDLERS = {
    "blueprint_registered_but_not_serving": _fix_blueprint_silent_failure,
    "generic_find_replace": _fix_generic_find_replace,
}


# ─── Endpoint ───────────────────────────────────────────────────────────

@brain_pr_opener_bp.route("/api/v1/brain/open-pr-for-finding", methods=["POST"])
def open_pr_for_finding():
    """Take a brain finding, open a PR with the proposed fix.

    POST body:
      { "issue": "blueprint_registered_but_not_serving",
        "url":   "main.py: register_blueprint(industry_pulse_bp)",
        "detail": "..." }

    Admin-gated. Returns the PR URL on success.
    """
    # Admin gate
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    if not _GITHUB_TOKEN:
        return jsonify(ok=False, error="GITHUB_TOKEN env var not set; cannot open PR"), 503

    # Stage-5 guardrails: kill switch + daily change budget. Every autonomous
    # PR passes through here. Manual callers can override with ?force=1.
    if request.args.get("force") not in ("1", "true", "yes"):
        try:
            from routes.brain_guardrails import can_open_pr
            _ok, _why = can_open_pr()
            if not _ok:
                return jsonify(ok=False, error="autonomy_gate_closed",
                               reason=_why), 429
        except Exception as _ge:
            return jsonify(ok=False, error=f"guardrail check failed: {_ge}"), 503

    finding = request.get_json(silent=True) or {}
    issue = finding.get("issue", "")
    fix_handler = _FIX_HANDLERS.get(issue)
    if not fix_handler:
        return jsonify(ok=False,
                       error=f"No fix template for issue type '{issue}'",
                       supported=list(_FIX_HANDLERS.keys())), 400

    # ★ Optional caller-supplied title. Without it every generic_find_replace PR
    #   is titled "[brain auto-fix] generic_find_replace" — indistinguishable in
    #   the PR list, and any dedupe keyed on title would reject the second
    #   distinct fix as a duplicate of the first. Callers that propose more than
    #   one kind of fix (the QA super-user) pass their own.
    pr_title = (finding.get("pr_title") or "").strip()[:120]
    if pr_title and open_pr_exists(pr_title):
        return jsonify(ok=False, error="duplicate",
                       reason=f"an open PR titled {pr_title!r} already exists",
                       ), 409

    file_path, new_content, summary = fix_handler(finding)
    if not new_content or not file_path:
        return jsonify(ok=False, error=f"Fix could not be templated: {summary}"), 422

    # Open the PR
    sha = _get_default_branch_sha()
    if not sha:
        return jsonify(ok=False, error="Could not read main branch SHA"), 503

    ts = int(time.time())
    issue_short = issue[:30]
    branch_name = f"brain/fix-{issue_short}-{ts}"
    if not _create_branch(branch_name, sha):
        return jsonify(ok=False, error=f"Could not create branch {branch_name}"), 503

    # Get current sha for the target file
    _cur, file_sha = _get_file(file_path, ref="main")
    if not file_sha:
        return jsonify(ok=False, error=f"Could not get sha for {file_path}"), 503

    if not _commit_file(file_path, new_content,
                         f"fix(brain): {issue} in {file_path}", branch_name, file_sha):
        return jsonify(ok=False, error=f"Could not commit fix to {file_path}"), 503

    pr_body = (
        f"## Brain auto-fix\n\n"
        f"**Issue:** `{issue}`\n"
        f"**Finding URL:** `{finding.get('url','?')}`\n\n"
        f"### What this PR does\n\n"
        f"{summary}\n\n"
        f"### Original finding\n\n"
        f"> {finding.get('detail', '(no detail)')[:500]}\n\n"
        f"---\n"
        f"_Auto-generated by `routes/brain_pr_opener.py`. Review carefully before merging — "
        f"this is L1 brain auto-remediation; humans hold the merge button._"
    )
    pr = _open_pr(
        title=pr_title or f"[brain auto-fix] {issue}",
        head=branch_name,
        body=pr_body,
    )
    if not pr:
        return jsonify(ok=False,
                        error="PR creation failed (branch + commit succeeded)",
                        branch=branch_name), 503

    # Count this PR against today's budget (after success only).
    try:
        from routes.brain_guardrails import record_pr_opened
        _budget_used = record_pr_opened()
    except Exception:
        _budget_used = None

    return jsonify(
        ok=True,
        pr_url=pr.get("html_url"),
        pr_number=pr.get("number"),
        branch=branch_name,
        summary=summary,
        auto_prs_today=_budget_used,
    ), 200


@brain_pr_opener_bp.route("/api/v1/brain/pr-opener/health", methods=["GET"])
def health():
    return jsonify(
        ok=True,
        github_token_set=bool(_GITHUB_TOKEN),
        github_repo=_GITHUB_REPO,
        supported_fixes=list(_FIX_HANDLERS.keys()),
        note=("POST a finding dict to /api/v1/brain/open-pr-for-finding "
              "with X-Admin-Key header to auto-generate a fix PR."),
    ), 200
