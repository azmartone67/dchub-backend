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
    conn = None
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
        return True
    except Exception as e:
        logger.warning(f"L15 table create failed: {e}")
        return False
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


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
    conn = None
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
    except Exception as e:
        logger.warning(f"L15 dedup lookup failed: {e}")
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass
    return out


def _chain_fingerprint(chain: dict) -> str:
    """Stable id for a chain's ROOT CAUSE, independent of the LLM-phrased title.
    L14 re-words the title every run ("Edge gating change…", "Edge routing/WAF
    change…", "Edge/WAF change is blocking all AI agents"), which defeats the
    exact-title dedup and floods duplicates. The chain's SYMPTOMS lead with a
    stable metric key (e.g. `ai_platform_crawl_drop: chatgpt -95%`), so fingerprint
    the sorted set of those keys instead."""
    import hashlib
    import re as _re
    keys = set()
    for s in (chain.get("symptoms", []) or []):
        m = _re.match(r"\s*([a-z0-9_]+)", str(s), _re.I)
        if m:
            keys.add(m.group(1).lower())
    if not keys:  # no structured symptoms → fall back to a normalized title
        keys.add(_re.sub(r"[^a-z0-9]+", "", str(chain.get("title", "")).lower())[:40])
    return hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:12]


def _chain_tokens(chain: dict) -> set[str]:
    """The set of SIGNIFICANT metric-key tokens a chain's symptoms lead with
    (e.g. `conversions_30d`, `ai_agent_requests_total`, `calls_by_platform_30d`).

    Used for near-duplicate detection: L14 re-words the title AND shuffles WHICH
    metrics it cites for the same finding across runs, so the exact-fp hash drifts
    and the same problem re-files under a new title (the funnel/attribution flood:
    #1577/#1583/#1585 and #1584/#1597/#1599/#1600). Overlap on these stable metric
    keys survives that drift. Tokens < 5 chars or in a small generic stopword set
    are dropped so a shared token means a real shared metric, not a common word."""
    import re as _re
    _STOP = {"error", "value", "total", "count", "delta", "ratio", "brain",
             "layer", "check", "issue", "metric", "status", "which", "there",
             "these", "their", "signal", "signals"}
    toks = set()
    for s in (chain.get("symptoms", []) or []):
        m = _re.match(r"\s*([a-z0-9_]+)", str(s), _re.I)
        if m:
            t = m.group(1).lower()
            if len(t) >= 5 and t not in _STOP:
                toks.add(t)
    return toks


def _verify_chain(chain: dict) -> tuple[bool, str]:
    """For chains that make a LIVE-CHECKABLE claim, test it before filing a
    high-confidence issue. Returns (True, '') when the claim holds or isn't
    checkable, (False, reason) when live evidence CONTRADICTS the hypothesis.

    This kills the self-perpetuating false-positive loop: L14 kept deriving
    "a WAF/edge change is blocking all AI crawlers" (confidence-high), but the
    crawlers actually return 200 and the worker IS on the route — so the fix
    targets a non-problem, the chain never 'resolves', the janitor never closes
    it, and L15 re-files it forever. Fail OPEN (a verification error keeps the
    normal filing path) so we never SUPPRESS a real finding by accident."""
    blob = " ".join([
        str(chain.get("title", "")),
        str(chain.get("root_cause_hypothesis", "")),
        " ".join(str(x) for x in (chain.get("symptoms", []) or [])),
    ]).lower()
    claims_crawler_block = (
        ("block" in blob or "challeng" in blob)
        and any(k in blob for k in ("crawler", "ai-agent", "ai agent", "waf",
                                     "edge gating", "edge routing", "worker route",
                                     "worker_version_header_missing"))
    )
    if not claims_crawler_block:
        return True, ""
    try:
        import requests as _rq
        base = "https://dchub.cloud"
        ok200 = 0
        for ua in ("GPTBot/1.0 (+https://openai.com/gptbot)", "ClaudeBot/1.0"):
            try:
                if _rq.get(base + "/llms.txt", headers={"User-Agent": ua},
                           timeout=8).status_code == 200:
                    ok200 += 1
            except Exception:
                pass
        worker_ok = False
        try:
            worker_ok = bool(_rq.get(base + "/api/v1/dcpi/scores?limit=1",
                                     timeout=8).headers.get("x-dc-worker-version"))
        except Exception:
            pass
        if ok200 >= 1 and worker_ok:
            return False, (f"verify_contradicted: crawlers HTTP 200 ({ok200}/2) "
                           f"on /llms.txt + worker header present — not a block")
    except Exception:
        return True, ""   # verification failed → fail open, keep filing
    return True, ""


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
    fp = _chain_fingerprint(chain)
    _fp_marker = f"brain_chain_fp_{fp}"   # embedded in the body; searchable
    _tokens = _chain_tokens(chain)        # stable metric keys for overlap-dedup
    # r-fp-dedup (2026-07-10): dedup by ROOT-CAUSE FINGERPRINT, not exact title.
    # L14 re-words the title every run, so the exact-title search below let the
    # SAME chain spawn 4 dupes (#1521/#1524/#1527/#1531). The fp is stable, is
    # stamped into every issue body, and is searched here. FAIL CLOSED on error.
    try:
        import requests as _rq0
        _fr = _rq0.get(
            "https://api.github.com/search/issues",
            headers={"Authorization": f"Bearer {_GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            params={"q": f'repo:{_GITHUB_REPO} is:issue is:open label:{_ISSUE_LABEL} "{_fp_marker}"'},
            timeout=10,
        )
        if _fr.status_code != 200:
            return False, f"dedup_fp_search_http_{_fr.status_code}_skip"
        if (_fr.json() or {}).get("items"):
            return False, f"dedup_same_chain_fp_#{_fr.json()['items'][0].get('number')}"
    except Exception as _e:
        return False, f"dedup_fp_search_error_skip_{type(_e).__name__}"
    # r-fp-overlap (2026-07-13): the exact-fp match above is brittle — L14 shuffles
    # WHICH metrics it cites for the same finding run-to-run, so the fp hash drifts
    # and the SAME problem re-files under a fresh title (the funnel + attribution
    # dupe floods). Also dedup on METRIC-TOKEN OVERLAP: if this chain shares >=2
    # significant metric keys with an already-open L15 issue, it's that finding
    # reworded → skip. FAIL-OPEN (any error → fall through and file) so this can
    # never silently suppress a genuine new finding; over-suppression is bounded
    # by requiring >=2 shared >=5-char metric tokens.
    if len(_tokens) >= 2:
        try:
            import requests as _rq2, re as _re2
            _lr = _rq2.get(
                "https://api.github.com/search/issues",
                headers={"Authorization": f"Bearer {_GITHUB_TOKEN}",
                         "Accept": "application/vnd.github+json"},
                params={"q": f"repo:{_GITHUB_REPO} is:issue is:open label:{_ISSUE_LABEL}",
                        "per_page": 50},
                timeout=10,
            )
            if _lr.status_code == 200:
                for _it in (_lr.json() or {}).get("items", []):
                    _mm = _re2.search(r"brain_chain_tokens:\s*([a-z0-9_ ]+)",
                                      _it.get("body") or "", _re2.I)
                    _other = set((_mm.group(1) if _mm else "").split())
                    if len(_tokens & _other) >= 2:
                        return False, f"dedup_token_overlap_#{_it.get('number')}"
        except Exception:
            pass  # fail-open: overlap dedup is best-effort, never blocks a real finding
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
        f"The brain issue janitor auto-closes this once the chain stops "
        f"being re-detected; if it recurs, L15 re-files._",
        "",
        f"<!-- {_fp_marker} -->",   # stable root-cause fingerprint for dedup
        f"<!-- brain_chain_tokens: {' '.join(sorted(_tokens))} -->",  # metric keys for overlap-dedup
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
    conn = None
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
    except Exception as e:
        logger.warning(f"L15 record failed: {e}")
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@brain_layer15_bp.route("/api/v1/brain/auto-action", methods=["GET"])
def auto_action_list():
    """Recent auto-actions (last 20)."""
    _ensure_table()
    conn = None
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
        return jsonify(
            ok=True,
            count=len(rows),
            dedup_window_days=_DEDUP_WINDOW_DAYS,
            actions=rows,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


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
        # r-verify (2026-07-10): don't file a high-confidence claim that live
        # evidence contradicts (e.g. "crawlers blocked" when they return 200).
        _vok, _vreason = _verify_chain(c)
        if not _vok:
            skipped.append({"title": title, "reason": _vreason})
            _record(c, False, _vreason)
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
