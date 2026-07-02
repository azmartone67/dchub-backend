"""
brain_issue_janitor.py — 2026-07-01. CLOSE-OUT half of the L15 issue loop.

L15 (brain_layer15_auto_action) OPENS a GitHub issue for every high-confidence
L14 causal chain, but nothing ever CLOSES them — the issue body literally says
"Close manually after fixing — L15 won't re-open". Result: the open-issue list
only grows (11 open brain-l15 issues as of 2026-07-01).

This janitor closes the loop:

  RESOLVED close — a brain-l15-auto issue older than the grace window whose
    chain title is NO LONGER present in the CURRENT L14 causal chains is
    considered fixed (L14 re-derives chains 4x/day; a fixed problem drops out).
    Closed as state_reason=completed. If the problem recurs, L14 re-derives the
    chain and L15 re-files a fresh issue (its dedup only blocks against OPEN
    issues + a 7d local title window) — so closing is safe, not lossy.

  STALE close — a brain-l15-auto issue open longer than STALE_DAYS (default 21)
    is closed as state_reason=not_planned regardless: three weeks un-actioned
    means the queue entry is noise, and L15 will re-file if still real.

  L23 PROPOSAL close — brain-l23-lifecycle capability proposals older than
    PROPOSAL_STALE_DAYS (default 30) are closed as not_planned (same rationale).

SAFETY INVARIANTS:
  · Only issues carrying the exact `brain-l15-auto` or `brain-l23-lifecycle`
    label are EVER touched — a human-filed issue can never match.
  · RESOLVED closes FAIL CLOSED: if the L14 causal fetch errors OR returns
    zero chains, NO resolution-closes happen that run (an empty chain list
    must never be read as "everything is fixed"). Age-based stale closes
    still run — they don't depend on the causal read.
  · Per-run cap (BRAIN_ISSUE_JANITOR_MAX_PER_RUN, default 10) bounds blast
    radius; kill switch BRAIN_ISSUE_JANITOR_DISABLE=1 halts all writes
    instantly without a deploy (same convention as BRAIN_L15_DISABLE).
  · Every close posts a breadcrumb comment explaining the auto-close and the
    re-file path, and is recorded in brain_janitor_closes for audit.
  · NEVER deletes, locks, or edits issue bodies — close + comment only.

Endpoints:
  GET  /api/v1/brain/issue-janitor/status — dry preview, never writes
  POST /api/v1/brain/issue-janitor/run    — acts (admin-gated); ?dry=1 previews

Cron: dchub-scheduler.py 'brain_issue_janitor' — 2x daily.
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_issue_janitor_bp = Blueprint("brain_issue_janitor", __name__)

_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
_L15_LABEL = "brain-l15-auto"
_L23_LABEL = "brain-l23-lifecycle"
_L15_TITLE_PREFIX = "[brain-l15] "


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _disabled() -> bool:
    return os.environ.get("BRAIN_ISSUE_JANITOR_DISABLE", "") in ("1", "true", "True", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _grace_days() -> int:
    return max(0, _env_int("BRAIN_ISSUE_JANITOR_GRACE_DAYS", 1))


def _stale_days() -> int:
    return max(1, _env_int("BRAIN_ISSUE_JANITOR_STALE_DAYS", 21))


def _proposal_stale_days() -> int:
    return max(1, _env_int("BRAIN_ISSUE_JANITOR_PROPOSAL_STALE_DAYS", 30))


def _max_per_run() -> int:
    return max(0, _env_int("BRAIN_ISSUE_JANITOR_MAX_PER_RUN", 10))


def _gh(method: str, path: str, body=None, params=None):
    import requests
    return requests.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dchub-brain-issue-janitor/1.0",
        },
        json=body,
        params=params,
        timeout=20,
    )


def _current_chain_titles() -> set[str] | None:
    """Titles of the CURRENT L14 causal chains (any confidence). Returns None
    on fetch failure or an empty analysis — callers MUST treat None as
    'unknown' and skip resolution-closes (fail closed)."""
    try:
        import requests
        r = requests.get("http://localhost:8080/api/v1/brain/causal", timeout=10)
        if r.status_code != 200:
            return None
        chains = ((r.json() or {}).get("analysis") or {}).get("causal_chains") or []
        titles = {(c.get("title") or "")[:120] for c in chains if c.get("title")}
        return titles or None
    except Exception:
        return None


def _open_issues(label: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= 3:  # 300 issues is far beyond any sane pileup
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/issues",
                params={"state": "open", "labels": label,
                        "per_page": 100, "page": page})
        if r.status_code != 200:
            break
        batch = [i for i in (r.json() or []) if "pull_request" not in i]
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def _age_days(issue: dict) -> float:
    try:
        created = _dt.datetime.fromisoformat(
            (issue.get("created_at") or "").replace("Z", "+00:00"))
        return (_dt.datetime.now(_dt.timezone.utc) - created).total_seconds() / 86400.0
    except Exception:
        return -1.0


def _close_issue(number: int, state_reason: str, comment: str) -> tuple[bool, str]:
    try:
        cr = _gh("POST", f"/repos/{_GITHUB_REPO}/issues/{number}/comments",
                 body={"body": comment})
        if cr.status_code not in (200, 201):
            logger.warning("issue-janitor: comment on #%s failed (%s)",
                           number, cr.status_code)
        r = _gh("PATCH", f"/repos/{_GITHUB_REPO}/issues/{number}",
                body={"state": "closed", "state_reason": state_reason})
        if r.status_code != 200:
            return False, f"github_{r.status_code}: {r.text[:150]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def _record_close(number: int, title: str, reason: str, ok: bool, err: str):
    conn = None
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_janitor_closes (
                id          SERIAL PRIMARY KEY,
                closed_at   TIMESTAMPTZ DEFAULT NOW(),
                kind        TEXT NOT NULL,
                number      INTEGER NOT NULL,
                title       TEXT,
                reason      TEXT,
                ok          BOOLEAN,
                error       TEXT
            )
        """)
        cur.execute(
            "INSERT INTO brain_janitor_closes (kind, number, title, reason, ok, error) "
            "VALUES ('issue', %s, %s, %s, %s, %s)",
            (number, title[:200], reason, ok, err or None),
        )
        conn.commit()
        try: cur.close()
        except Exception: pass
    except Exception as e:
        logger.warning("issue-janitor record failed: %s", e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


def janitor_sweep(dry_run: bool = True) -> dict:
    """Scan open brain-labeled issues and close the resolved/stale ones.
    dry_run=True (or the kill switch) → zero GitHub writes, returns the
    would-close list."""
    if _disabled():
        dry_run = True
    if not _token():
        return {"ok": False, "error": "no_github_token"}

    chain_titles = _current_chain_titles()
    grace, stale = _grace_days(), _stale_days()
    cap = _max_per_run()

    candidates: list[dict] = []  # {number, title, reason, state_reason, comment}

    for issue in _open_issues(_L15_LABEL):
        num, title = issue.get("number"), (issue.get("title") or "")
        age = _age_days(issue)
        if age < 0:
            continue
        chain_title = title[len(_L15_TITLE_PREFIX):] if title.startswith(_L15_TITLE_PREFIX) else title
        if age >= stale:
            candidates.append({
                "number": num, "title": title, "reason": f"stale_{stale}d",
                "state_reason": "not_planned",
                "comment": (f"🤖 **Auto-closed by the brain issue janitor** — open "
                            f"{int(age)}d with no action. If this is still real, L14 "
                            f"will re-derive the chain and L15 will re-file a fresh "
                            f"issue automatically."),
            })
        elif age >= grace and chain_titles is not None and chain_title[:120] not in chain_titles:
            candidates.append({
                "number": num, "title": title, "reason": "resolved_not_redetected",
                "state_reason": "completed",
                "comment": ("🤖 **Auto-closed by the brain issue janitor** — this "
                            "causal chain is no longer present in the current L14 "
                            "analysis, so the underlying problem reads as resolved. "
                            "If it recurs, L15 will re-file automatically."),
            })

    for issue in _open_issues(_L23_LABEL):
        num, title = issue.get("number"), (issue.get("title") or "")
        age = _age_days(issue)
        if age >= _proposal_stale_days():
            candidates.append({
                "number": num, "title": title,
                "reason": f"proposal_stale_{_proposal_stale_days()}d",
                "state_reason": "not_planned",
                "comment": ("🤖 **Auto-closed by the brain issue janitor** — this "
                            "capability proposal sat unactioned past the stale "
                            "window. The brain will re-propose it if it still "
                            "scores as valuable."),
            })

    candidates = candidates[:cap]
    if dry_run:
        return {"ok": True, "dry_run": True, "disabled": _disabled(),
                "causal_chains_seen": len(chain_titles) if chain_titles else 0,
                "would_close": candidates, "would_close_count": len(candidates)}

    closed, errors = [], []
    for c in candidates:
        ok, err = _close_issue(c["number"], c["state_reason"], c["comment"])
        _record_close(c["number"], c["title"], c["reason"], ok, err)
        (closed if ok else errors).append(
            {"number": c["number"], "reason": c["reason"], **({"error": err} if err else {})})

    return {"ok": True, "dry_run": False,
            "causal_chains_seen": len(chain_titles) if chain_titles else 0,
            "closed": closed, "closed_count": len(closed), "errors": errors}


@brain_issue_janitor_bp.route("/api/v1/brain/issue-janitor/status", methods=["GET"])
def issue_janitor_status():
    """Always-dry preview of what the next run would close."""
    preview = janitor_sweep(dry_run=True)
    return jsonify(
        ok=True,
        disabled=_disabled(),
        grace_days=_grace_days(),
        stale_days=_stale_days(),
        proposal_stale_days=_proposal_stale_days(),
        max_per_run=_max_per_run(),
        preview=preview,
    )


@brain_issue_janitor_bp.route("/api/v1/brain/issue-janitor/run", methods=["POST"])
def issue_janitor_run():
    """Acting sweep (cron target). ?dry=1 forces a preview. Admin-gated when
    DCHUB_ADMIN_KEY is set — same convention as L15's run endpoint."""
    if _admin_key():
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _admin_key():
            return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry") == "1"
    result = janitor_sweep(dry_run=dry)
    status = 200 if result.get("ok") else 503
    return jsonify(result), status
