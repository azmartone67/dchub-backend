"""
Brain L15 — Auto-Action (2026-05-19).

When L14 (Causal Reasoner) returns a causal chain with confidence='high'
and a concrete smallest_safe_fix, L15 automatically opens a GitHub
issue with the chain's details. Closes the loop from "brain finds" →
"human work queue" without requiring a human to be reading the L14
output each cycle.

Why this is the right design (not auto-PR):
  - GitHub issues are low-friction (no diff to compute, no merge risk)
  - Each issue includes verification steps + smallest_safe_fix so a
    human can act on it directly
  - L15 tracks which chains have already been issued (idempotent —
    same chain title in 7d window = no duplicate issue)
  - High-confidence-only: medium/low confidence chains stay surfaced
    in the dashboards but don't auto-create work items

Endpoints:
  GET  /api/v1/brain/auto-action            — list recent auto-issues
  POST /api/v1/brain/auto-action/run        — admin: scan L14 + open
                                              new issues (idempotent)

Cron: every 6h at :30 (between L14 :15 and L2 narrative :25 vs L8 :45).
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer15_bp = Blueprint("brain_layer15", __name__)

_GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_ISSUE_LABEL = "brain-l15-auto"  # so humans can filter for these
_DEDUP_WINDOW_DAYS = 7


def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_auto_actions (
                id              SERIAL PRIMARY KEY,
                opened_at       TIMESTAMPTZ DEFAULT NOW(),
                chain_title     TEXT NOT NULL,
                chain_confidence TEXT,
                root_cause      TEXT,
                fix_proposed    TEXT,
                github_issue_url TEXT,
                github_issue_num INTEGER,
                error           TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_auto_actions_title_time "
                    "ON brain_auto_actions(chain_title, opened_at DESC)")
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L15 table create failed: {e}")
        return False


def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


def _recently_issued_titles() -> set[str]:
    """Pull chain titles that already got an issue in the dedup window."""
    out: set[str] = set()
    try:
        from main import get_db
        conn = get_db()
        if not conn: return out
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT chain_title FROM brain_auto_actions "
            "WHERE opened_at > NOW() - INTERVAL %s "
            "AND github_issue_url IS NOT NULL",
            (f"{_DEDUP_WINDOW_DAYS} days",),
        )
        for r in cur.fetchall():
            t = r.get("chain_title") if hasattr(r, "get") else r[0]
            if t: out.add(t)
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as e:
        logger.warning(f"L15 dedup lookup failed: {e}")
    return out


def _open_issue(chain: dict) -> tuple[bool, str]:
    """Open one GitHub issue for a causal chain. Returns (ok, url_or_err)."""
    # r65 (2026-06-01): runtime kill switch — set BRAIN_L15_DISABLE=1 in
    # Railway to halt ALL auto-issue creation instantly without a deploy.
    import os as _os
    if _os.environ.get("BRAIN_L15_DISABLE", "") in ("1", "true", "True", "yes"):
        return False, "disabled_via_env"
    if not _GITHUB_TOKEN:
        return False, "no_github_token"
    title = chain.get("title", "Brain L14 auto-action")[:120]
    # r65: HARD dedup against GitHub itself. The local brain_auto_actions
    # dedup table failed to prevent re-creation and FLOODED 100+ identical
    # "404 spike" issues (#3-#949), emailing a real repo watcher. Before
    # creating, query GitHub for an OPEN issue with this exact title and skip
    # if one exists. FAIL CLOSED — on ANY search error, do NOT create (better
    # to miss one issue than to spam a human's inbox). Makes the flood
    # structurally impossible regardless of the local-table dedup state.
    _full_title = f"[brain-l15] {title}"
    try:
        import requests as _rq
        _sr = _rq.get(
            "https://api.github.com/search/issues",
            headers={"Authorization": f"Bearer {_GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            params={"q": f'repo:{_GITHUB_REPO} is:issue is:open in:title "{_full_title}"'},
            timeout=10,
        )
        if _sr.status_code != 200:
            return False, f"dedup_search_http_{_sr.status_code}_skip"
        for _it in (_sr.json() or {}).get("items", []):
            if (_it.get("title") or "") == _full_title:
                return False, f"dedup_existing_open_issue_#{_it.get('number')}"
    except Exception as _e:
        return False, f"dedup_search_error_skip_{type(_e).__name__}"
    syms = chain.get("symptoms", []) or []
    body_lines = [
        f"**Auto-opened by Brain L15** ([routes/brain_layer15_auto_action.py](https://github.com/{_GITHUB_REPO}/blob/main/routes/brain_layer15_auto_action.py))",
        "",
        f"Confidence: **{chain.get('confidence', '?')}**",
        "",
        "## Symptoms (cross-layer findings that point at this root cause)",
    ]
    for s in syms[:8]:
        body_lines.append(f"- {s}")
    body_lines += [
        "",
        "## Root-cause hypothesis",
        chain.get("root_cause_hypothesis", "(none)"),
        "",
        "## Smallest-safe fix",
        f"```\n{chain.get('smallest_safe_fix', '(none)')}\n```",
        "",
        "## How to verify",
        chain.get("verification", "(none)"),
        "",
        "---",
        "",
        f"_Generated by L14 (Causal Reasoner) + L15 (Auto-Action). "
        f"Same chain title won't be re-issued for {_DEDUP_WINDOW_DAYS}d. "
        f"Close manually after fixing — L15 won't re-open._",
    ]
    body = "\n".join(body_lines)

    try:
        import requests
        r = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers={
                "Authorization": f"Bearer {_GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": f"[brain-l15] {title}",
                "body": body,
                "labels": [_ISSUE_LABEL,
                           f"confidence-{chain.get('confidence', 'unknown')}"],
            },
            timeout=15,
        )
        if r.status_code not in (200, 201):
            return False, f"github_{r.status_code}: {r.text[:200]}"
        data = r.json() or {}
        return True, data.get("html_url") or ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def _record(chain: dict, ok: bool, url_or_err: str, issue_num: int | None = None):
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brain_auto_actions "
            "(chain_title, chain_confidence, root_cause, fix_proposed, "
            " github_issue_url, github_issue_num, error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (chain.get("title", "?"),
             chain.get("confidence", "?"),
             chain.get("root_cause_hypothesis", "")[:1000],
             chain.get("smallest_safe_fix", "")[:1000],
             url_or_err if ok else None,
             issue_num,
             None if ok else url_or_err),
        )
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as e:
        logger.warning(f"L15 record failed: {e}")


@brain_layer15_bp.route("/api/v1/brain/auto-action", methods=["GET"])
def auto_action_list():
    """Recent auto-actions (last 20)."""
    _ensure_table()
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        cur = conn.cursor()
        cur.execute(
            "SELECT opened_at, chain_title, chain_confidence, "
            "       github_issue_url, github_issue_num, error "
            "FROM brain_auto_actions "
            "ORDER BY opened_at DESC LIMIT 20"
        )
        rows = []
        for r in cur.fetchall():
            if hasattr(r, "get"):
                rows.append({
                    "opened_at":  str(r.get("opened_at") or ""),
                    "title":      r.get("chain_title"),
                    "confidence": r.get("chain_confidence"),
                    "issue_url":  r.get("github_issue_url"),
                    "issue_num":  r.get("github_issue_num"),
                    "error":      r.get("error"),
                })
            else:
                rows.append({
                    "opened_at":  str(r[0]),
                    "title":      r[1],
                    "confidence": r[2],
                    "issue_url":  r[3],
                    "issue_num":  r[4],
                    "error":      r[5],
                })
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return jsonify(
            ok=True,
            count=len(rows),
            dedup_window_days=_DEDUP_WINDOW_DAYS,
            actions=rows,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503


@brain_layer15_bp.route("/api/v1/brain/auto-action/run",
                        methods=["POST", "GET"])
def auto_action_run():
    """Scan L14's causal chains; open a GitHub issue for any high-
       confidence chain not seen in the dedup window. Idempotent — safe
       to fire on a cron."""
    if request.method == "POST" and _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    if not _GITHUB_TOKEN:
        return jsonify(ok=False, error="GITHUB_TOKEN not set"), 503

    _ensure_table()
    causal = _internal("/api/v1/brain/causal", 8)
    chains = (causal.get("analysis") or {}).get("causal_chains") or []
    if not chains:
        return jsonify(
            ok=True,
            note="No L14 causal chains available. Trigger L14 analysis first.",
            issued=0, skipped=0,
        )

    recently_issued = _recently_issued_titles()
    issued, skipped, errors = [], [], []
    for c in chains:
        conf = (c.get("confidence") or "").lower()
        title = c.get("title", "")
        if conf != "high":
            skipped.append({"title": title, "reason": f"confidence={conf}"})
            continue
        if not c.get("smallest_safe_fix"):
            skipped.append({"title": title, "reason": "no smallest_safe_fix"})
            continue
        if title in recently_issued:
            skipped.append({"title": title, "reason": "dedup_recently_issued"})
            continue
        ok, url_or_err = _open_issue(c)
        _record(c, ok, url_or_err)
        if ok:
            issued.append({"title": title, "issue_url": url_or_err})
        else:
            errors.append({"title": title, "error": url_or_err})

    return jsonify(
        ok=True,
        issued_count=len(issued),
        skipped_count=len(skipped),
        error_count=len(errors),
        issued=issued,
        skipped=skipped[:10],
        errors=errors[:5],
        ran_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
