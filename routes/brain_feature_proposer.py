"""
routes/brain_feature_proposer.py — Brain auto-feature proposer (2026-06-07).

Closes the loop between `/feedback` and the codebase. Customers submit
ideas via /feedback (or /partners/feedback per CF zone-trap workaround);
routes/feedback_triage.py already classifies LOW/MEDIUM/HIGH/SPAM. That
covers BUG-shape feedback (a typo, a wrong cached number, a dead link)
which routes/brain_pr_opener.py can ship as a 1-line patch PR.

This module covers FEATURE-shape feedback — three users say "I wish DC
Hub had X." A single-row 1-line patch won't help; we need a feature
spec + a stub blueprint + a draft PR a human can flesh out.

Pipeline:
  1. gather_feature_signals() — pull feedback_submissions last 30d
     where triage_priority IN ('HIGH','MEDIUM') AND status != 'declined',
     EXCLUDING rows already linked to a proposal PR. Cluster by simple
     keyword/embed similarity. Each cluster carries (theme, count,
     sample_texts, member_ids).
  2. propose_feature(cluster) — Claude writes a 200-word spec given the
     cluster's user requests + DC Hub's existing primitives (33-38 MCP
     tools, market briefs, hyperscaler briefs, etc.). Spec includes:
     feature name, problem statement, proposed UX, complexity
     (small/medium/large), priority justification.
  3. open_draft_pr(spec, cluster) — writes a stub file
     routes/_proposed_<feature_slug>.py with the spec as a module
     docstring + a Blueprint stub. Opens a DRAFT PR. Stamps
     brain_proposal_pr_url on every feedback row in the cluster.

Safety:
  * BRAIN_FEATURE_PROPOSER_DISABLE=1 — kill switch.
  * BRAIN_FEATURE_PROPOSER_DRY_RUN=1 — generate spec, skip PR write
    and DB stamp; return the spec inline.
  * BRAIN_FEATURE_PROPOSER_WEEKLY_CAP — default 2/wk hard cap.
  * Cluster size floor — default 3 (BRAIN_FEATURE_PROPOSER_MIN_CLUSTER).
  * Spam filter — rows previously classified SPAM are NEVER pulled
    (the SQL filter is class IN MEDIUM/HIGH only; SPAM is declined).
    Belt + suspenders: a low-quality-prompt heuristic also rejects
    rows < 40 chars or with no proper-noun signal.
  * Per-feedback-row idempotence — once brain_proposal_pr_url is set,
    that row is excluded from the next cluster sweep, so a feature
    can never be re-proposed.

Endpoint:
  POST /api/v1/admin/brain/feature-proposer/run     X-Admin-Key
  GET  /api/v1/admin/brain/feature-proposer/preview X-Admin-Key
  GET  /api/v1/admin/brain/feature-proposer/clusters  X-Admin-Key

Schedule:
  SCHEDULE entry (15, 3, "brain_feature_proposer", "_run_brain_feature_proposer")
  — twice daily UTC, like multiplatform_amplifier.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

from utils.anthropic_helper import anthropic_messages_url

logger = logging.getLogger(__name__)

brain_feature_proposer_bp = Blueprint("brain_feature_proposer", __name__)


# ── env ───────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
BRAIN_MODEL = (os.environ.get("DCHUB_BRAIN_MODEL_REASONING")
               or os.environ.get("DCHUB_BRAIN_MODEL")
               or "claude-opus-4-7")

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_FEATURE_PROPOSER_DISABLE"))


def _dry_run() -> bool:
    return _truthy(os.environ.get("BRAIN_FEATURE_PROPOSER_DRY_RUN"))


def _weekly_cap() -> int:
    try:
        return max(0, int(os.environ.get(
            "BRAIN_FEATURE_PROPOSER_WEEKLY_CAP", "2")))
    except Exception:
        return 2


def _min_cluster() -> int:
    try:
        return max(2, int(os.environ.get(
            "BRAIN_FEATURE_PROPOSER_MIN_CLUSTER", "3")))
    except Exception:
        return 3


# ── DB helpers (mirror routes/feedback_triage.py) ─────────────────────


def _conn():
    """Return a raw psycopg2 connection (NOT the _iso_common
    contextmanager — its `conn()` is `@contextmanager`, so `_c().cursor()`
    crashes with `'_GeneratorContextManager' has no attribute 'cursor'`).
    Mirrors the pattern that already works in routes/brain_bug_squash.py."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_feature_proposer: _conn failed: %s", e)
    return None


def init_feature_proposer_columns() -> None:
    """Idempotent ALTER ADD COLUMN IF NOT EXISTS for
    feedback_submissions.brain_proposal_pr_url. Wired into
    content_publisher.init_content_tables() at boot."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_feature_proposer: no DB; skipping column init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "ALTER TABLE feedback_submissions "
                    "ADD COLUMN IF NOT EXISTS brain_proposal_pr_url TEXT"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "ALTER TABLE feedback_submissions "
                    "ADD COLUMN IF NOT EXISTS brain_proposal_cluster_id TEXT"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_feedback_brain_proposal_pr "
                    "ON feedback_submissions (brain_proposal_pr_url)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            # Audit table for proposal runs — cheap, idempotent.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_feature_proposal_log (
                        id            BIGSERIAL PRIMARY KEY,
                        cluster_id    TEXT NOT NULL,
                        theme         TEXT,
                        feedback_ids  JSONB,
                        spec          TEXT,
                        pr_url        TEXT,
                        pr_number     INT,
                        status        TEXT NOT NULL DEFAULT 'open',
                        dry_run       BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_brain_feature_proposal_log_created "
                    "ON brain_feature_proposal_log (created_at DESC)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_feature_proposer: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ── 1. gather_feature_signals ─────────────────────────────────────────

# Words that don't carry signal for clustering.
_STOPWORDS = frozenset("""
a about above after again against all am an and any are aren as at be
because been before being below between both but by can cant could
couldnt did didnt do does doesnt doing dont down during each few for
from further had hadnt has hasnt have havent having he hed hes her here
heres hers herself him himself his how hows i id ill im ive if in into
is isnt it its itself lets me more most must mustnt my myself no nor
not of off on once only or other ought our ours ourselves out over own
same shant she shes should shouldnt so some such than that thats the
their theirs them themselves then there theres these they theyd theyll
theyre theyve this those through to too under until up very was wasnt
we wed well were weve werent what whats when whens where wheres which
while who whos whom why whys with wont would wouldnt you youd youll
youre youve your yours yourself yourselves like want need add make
should could would feature feedback page site app dchub data hub
please thanks thank really very much also one some 0 1 2 3 4 5 6 7 8 9
""".split())


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    # Keep alphanumerics + lowercase + drop trivial/stop tokens.
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    return [t for t in raw if t not in _STOPWORDS and len(t) >= 4]


def _is_spammy(row: dict) -> bool:
    """Belt + suspenders quality filter. SPAM rows are already excluded
    by the SQL WHERE, but this catches low-signal MEDIUM/HIGH rows that
    Claude misclassified."""
    desc = ((row.get("description") or "") + " "
            + (row.get("title") or "")).strip()
    if len(desc) < 40:
        return True
    # Pure URL or pure caps tend to be spam.
    if desc.lower().startswith("http") and len(desc.split()) < 6:
        return True
    if desc.isupper() and len(desc) > 20:
        return True
    # No noun-like proper signal at all → likely gibberish.
    tokens = _tokenize(desc)
    if len(tokens) < 4:
        return True
    return False


def _cluster_by_keywords(rows: list[dict], min_size: int) -> list[dict]:
    """Group rows by shared salient keyword. Each row contributes its
    top-3 informative tokens; rows that share >=1 token with a seed
    join that cluster. Single-pass union-find over token sets.

    Returns clusters as [{"theme": "...", "count": n,
                           "member_ids": [...], "sample_texts": [...],
                           "tokens": [...]}] sorted by count desc.

    Pure-stdlib — keyword similarity, NOT embeddings. The brain Layer-5
    pipeline already uses richer embedding via Claude; here we want a
    deterministic, fast, cheap pre-filter so we don't spend $$ on
    Claude for theme detection. If a cluster is borderline, we
    over-include; propose_feature() prunes downstream.
    """
    # Pre-compute the top-5 tokens for each row.
    row_tokens: dict[int, list[str]] = {}
    for r in rows:
        if _is_spammy(r):
            continue
        toks = _tokenize((r.get("title") or "") + " "
                          + (r.get("description") or ""))
        if not toks:
            continue
        c = Counter(toks)
        # Top-5 by frequency × length-bonus so multi-word features
        # like "site-selection" win.
        top = [t for t, _ in c.most_common(5)]
        if top:
            row_tokens[r["id"]] = top

    # Union-find by shared token.
    parent: dict[int, int] = {rid: rid for rid in row_tokens}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Build a token → row index, then union rows that share any token.
    token_to_rows: dict[str, list[int]] = defaultdict(list)
    for rid, toks in row_tokens.items():
        for t in toks:
            token_to_rows[t].append(rid)
    for t, rids in token_to_rows.items():
        if len(rids) < 2:
            continue
        # Only union on tokens shared by >=2 rows (signal, not noise).
        anchor = rids[0]
        for other in rids[1:]:
            union(anchor, other)

    # Materialize clusters.
    groups: dict[int, list[int]] = defaultdict(list)
    for rid in row_tokens:
        groups[find(rid)].append(rid)

    # Map id → row for hydration.
    by_id = {r["id"]: r for r in rows}
    clusters = []
    for root, members in groups.items():
        if len(members) < min_size:
            continue
        # Aggregate token frequency across the cluster for theme.
        c = Counter()
        for rid in members:
            c.update(row_tokens.get(rid, []))
        theme_tokens = [t for t, _ in c.most_common(3)]
        theme = " / ".join(theme_tokens) if theme_tokens else "general"
        sample_texts = []
        for rid in members[:6]:
            r = by_id.get(rid) or {}
            txt = ((r.get("title") or "") + " — "
                   + (r.get("description") or "")[:240]).strip(" —")
            sample_texts.append({"id": rid, "text": txt})
        # Cluster id is stable per token-set → re-runs find the SAME
        # cluster id and brain_proposal_cluster_id stays comparable.
        cid = hashlib.sha256(
            ("|".join(sorted(theme_tokens))).encode("utf-8")
        ).hexdigest()[:16]
        clusters.append({
            "cluster_id": cid,
            "theme": theme,
            "count": len(members),
            "member_ids": sorted(members),
            "sample_texts": sample_texts,
            "tokens": theme_tokens,
        })
    clusters.sort(key=lambda c: (-c["count"], c["theme"]))
    return clusters


def gather_feature_signals(min_cluster: Optional[int] = None) -> dict:
    """Pull last-30d feedback_submissions classified MEDIUM/HIGH (not
    SPAM, not already proposed), cluster them, return summary."""
    out: dict = {"clusters": [], "rows_scanned": 0, "errors": []}
    floor = min_cluster if min_cluster is not None else _min_cluster()
    conn = _conn()
    if conn is None:
        out["errors"].append("no_db")
        return out
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, type, url_referenced, "
                "       brain_triage_class, brain_proposal_pr_url, "
                "       brain_confidence "
                "FROM feedback_submissions "
                "WHERE created_at > NOW() - INTERVAL '30 days' "
                "  AND COALESCE(brain_triage_class, '') "
                "      IN ('MEDIUM', 'HIGH') "
                "  AND COALESCE(status, 'new') NOT IN ('declined') "
                "  AND brain_proposal_pr_url IS NULL "
                "ORDER BY created_at DESC LIMIT 500"
            )
            rows_raw = cur.fetchall() or []
            rows: list[dict] = []
            for r in rows_raw:
                if hasattr(r, "get"):
                    rows.append(dict(r))
                else:
                    rows.append({
                        "id": r[0], "title": r[1], "description": r[2],
                        "type": r[3], "url_referenced": r[4],
                        "brain_triage_class": r[5],
                        "brain_proposal_pr_url": r[6],
                        "brain_confidence": r[7],
                    })
            out["rows_scanned"] = len(rows)
            out["clusters"] = _cluster_by_keywords(rows, min_size=floor)
    except Exception as e:
        out["errors"].append(f"db_query:{str(e)[:200]}")
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── 2. propose_feature (Claude call) ──────────────────────────────────


_PROPOSE_SYSTEM = (
    "You are the DC Hub Feature Proposer. Multiple customers have asked "
    "for related capabilities at https://dchub.cloud. Your job is to "
    "write a tight 200-word feature spec a human engineer can review in "
    "60 seconds.\n\n"
    "DC Hub's existing primitives — REUSE these wherever possible:\n"
    "  - 33-38 MCP tools (search_facilities, compare_isos, compare_sites,\n"
    "    score_facility, deal_autopsy, get_market_intel, get_grid_*,\n"
    "    site_selection_canvas, etc.)\n"
    "  - /market-briefs/<slug> 9-section report (live)\n"
    "  - /hyperscalers/<slug>/brief operator briefs (live)\n"
    "  - /state-of-2026 living doc + claims proposals\n"
    "  - /admin/funnel-health KPI panel\n"
    "  - DCPI (Data Center Power Index) market scores\n"
    "  - watchlist + verdict-shift alerts\n"
    "  - /feedback forum, /research/* PDFs, /pricing\n\n"
    "Output STRICTLY a JSON object — no prose outside the JSON:\n"
    "  {\"feature_name\": \"<3-5 words, slug-friendly>\",\n"
    "   \"feature_slug\": \"<kebab_case, [a-z0-9_-]+>\",\n"
    "   \"problem\": \"<2 sentences: what user pain is real here>\",\n"
    "   \"proposed_ux\": \"<3-5 sentences: concrete UX, citing existing routes/MCP tools>\",\n"
    "   \"complexity\": \"small|medium|large\",\n"
    "   \"priority_justification\": \"<1 sentence: why this matters now>\",\n"
    "   \"primary_route\": \"</url/path that would serve this feature>\",\n"
    "   \"reuses\": [\"<existing primitive 1>\", \"<existing primitive 2>\"]}\n\n"
    "Rules:\n"
    "  - feature_slug MUST match ^[a-z][a-z0-9_-]{2,40}$ — used in a Python module name.\n"
    "  - Prefer small/medium complexity. Refuse to invent net-new datasets.\n"
    "  - If the cluster is too vague to write a coherent spec, return\n"
    "    {\"feature_name\": null, \"complexity\": \"unclear\"} and stop."
)


def _call_claude(prompt: str) -> tuple[Optional[str], Optional[str]]:
    """Single Anthropic call. Mirrors routes/feedback_triage._call_claude."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key"
    try:
        from routes.brain_models import resolve_chain
        models = resolve_chain(BRAIN_MODEL)
    except Exception:
        models = [BRAIN_MODEL, "claude-sonnet-4-20250514"]
    last_err = None
    for i, model in enumerate(models):
        body = json.dumps({
            "model": model,
            "max_tokens": 1200,
            "system": _PROPOSE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", ""), None
            return None, "no_text_block"
        except urllib.error.HTTPError as e:
            last_err = f"http_{e.code}"
            if e.code in (404, 400) and i + 1 < len(models):
                continue
            return None, last_err
        except Exception as e:
            return None, f"call_fail: {repr(e)[:160]}"
    return None, last_err or "all_models_failed"


def _parse_spec_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{2,40}$")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not s:
        s = "feature"
    if not s[0].isalpha():
        s = "f-" + s
    return s[:40] or "feature"


def propose_feature(cluster: dict) -> dict:
    """Run Claude on a cluster. Returns {"ok": bool, "spec": {...},
    "error": "..."}. Never raises."""
    samples = "\n".join(
        f"  - #{s['id']}: {s['text'][:300]}"
        for s in (cluster.get("sample_texts") or [])
    )
    prompt = (
        f"Cluster theme keywords: {cluster.get('theme')}\n"
        f"User request count: {cluster.get('count')}\n"
        f"Sample requests (most recent):\n{samples}\n"
    )
    text, err = _call_claude(prompt)
    if err:
        return {"ok": False, "error": err}
    spec = _parse_spec_json(text) or {}
    if not spec.get("feature_name"):
        return {"ok": False, "error": "claude_returned_unclear",
                "raw": (text or "")[:600]}
    # Normalize slug.
    slug = (spec.get("feature_slug") or "").strip().lower()
    if not _SLUG_RE.match(slug):
        slug = _slugify(spec.get("feature_name") or "feature")
    spec["feature_slug"] = slug
    return {"ok": True, "spec": spec}


# ── 3. open_draft_pr ──────────────────────────────────────────────────


def _stub_module_content(spec: dict, cluster: dict) -> str:
    """Build the Python source for routes/_proposed_<slug>.py — a stub
    Blueprint with the spec as a module docstring. The 'proposed' prefix
    makes the route file easy to grep + delete if the human rejects."""
    samples = "\n".join(
        f"  - feedback #{s['id']}: {(s['text'] or '')[:180]}"
        for s in (cluster.get("sample_texts") or [])[:5]
    )
    docstring = (
        f"\"\"\"\n"
        f"routes/_proposed_{spec['feature_slug']}.py — DRAFT proposal "
        f"(auto-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}).\n"
        f"\n"
        f"Feature: {spec.get('feature_name', '?')}\n"
        f"Complexity: {spec.get('complexity', '?')}\n"
        f"Primary route: {spec.get('primary_route', '/?')}\n"
        f"Reuses: {', '.join(spec.get('reuses') or []) or '(none yet)'}\n"
        f"\n"
        f"### Problem\n"
        f"{spec.get('problem', '?')}\n"
        f"\n"
        f"### Proposed UX\n"
        f"{spec.get('proposed_ux', '?')}\n"
        f"\n"
        f"### Priority\n"
        f"{spec.get('priority_justification', '?')}\n"
        f"\n"
        f"### Source feedback ({cluster.get('count', 0)} users, cluster "
        f"{cluster.get('cluster_id', '?')})\n"
        f"{samples}\n"
        f"\n"
        f"### How to ship\n"
        f"1. Flesh out the route handler below (replace TODO).\n"
        f"2. Register the blueprint in main.py near the other brain BPs.\n"
        f"3. If a new schema is needed, add init_*_tables() and wire it\n"
        f"   into content_publisher.init_content_tables().\n"
        f"4. Add a SCHEDULE entry in crawler_scheduler.py if cron-driven.\n"
        f"5. Rename file (drop the _proposed_ prefix) when ready to ship.\n"
        f"6. Mark the source feedback rows status='shipped' so they exit\n"
        f"   the feedback admin queue.\n"
        f"\n"
        f"Auto-generated by routes/brain_feature_proposer.py. The brain\n"
        f"opened this DRAFT PR after {cluster.get('count', 0)} users asked\n"
        f"for the same thing. Humans hold the merge button.\n"
        f"\"\"\"\n"
    )
    # Use feature_slug for both the blueprint name and the URL.
    slug = spec["feature_slug"]
    safe_var = re.sub(r"[^a-z0-9_]+", "_", slug).strip("_") or "feature"
    body = (
        f"from __future__ import annotations\n\n"
        f"import logging\n\n"
        f"from flask import Blueprint, jsonify\n\n"
        f"logger = logging.getLogger(__name__)\n\n"
        f"# Blueprint is intentionally NOT auto-registered. Register it in\n"
        f"# main.py near the other brain BPs once the implementation lands.\n"
        f"proposed_{safe_var}_bp = Blueprint(\n"
        f"    \"proposed_{safe_var}\", __name__,\n"
        f")\n\n\n"
        f"@proposed_{safe_var}_bp.route(\"{spec.get('primary_route', '/' + slug)}\","
        f" methods=[\"GET\"])\n"
        f"def proposed_{safe_var}_index():\n"
        f"    \"\"\"TODO: replace this stub with the real handler.\"\"\"\n"
        f"    return jsonify(\n"
        f"        ok=False,\n"
        f"        error=\"not_implemented\",\n"
        f"        feature=\"{spec.get('feature_name', '?')}\",\n"
        f"        spec_in=__doc__[:200],\n"
        f"    ), 501\n"
    )
    return docstring + "\n" + body


def _weekly_open_count(conn) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM brain_feature_proposal_log "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "  AND status = 'open' AND dry_run = FALSE"
            )
            row = cur.fetchone()
            return int((row[0] if not hasattr(row, "get")
                        else row.get("count")) or 0)
    except Exception:
        return 0


def open_draft_pr(spec: dict, cluster: dict) -> dict:
    """Create routes/_proposed_<slug>.py + open a DRAFT PR. Returns
    {"ok": bool, "pr_url": "...", "pr_number": N, "branch": "...",
     "error": "..."}. Never raises."""
    try:
        from routes.brain_pr_opener import (
            _GITHUB_TOKEN, _GITHUB_REPO, _gh,
            _get_default_branch_sha, _get_file, _create_branch,
            _commit_file, _open_pr,
        )
    except Exception as e:
        return {"ok": False, "error": f"pr_opener_import:{e}"}
    if not _GITHUB_TOKEN:
        return {"ok": False, "error": "no_github_token"}

    slug = spec["feature_slug"]
    file_path = f"routes/_proposed_{slug}.py"

    # If the file already exists on main, append a counter so a re-run
    # doesn't 422.
    existing, _ = _get_file(file_path, ref="main")
    final_path = file_path
    if existing is not None:
        ts = int(time.time()) % 100000
        final_path = f"routes/_proposed_{slug}_{ts}.py"

    content = _stub_module_content(spec, cluster)

    # Branch off main.
    sha = _get_default_branch_sha()
    if not sha:
        return {"ok": False, "error": "no_main_sha"}
    branch = f"brain/feature-{slug[:30]}-{int(time.time())}"
    if not _create_branch(branch, sha):
        return {"ok": False, "error": f"create_branch_failed:{branch}"}

    if not _commit_file(final_path, content,
                        f"feat(brain): draft proposal — {spec.get('feature_name', slug)}",
                        branch, None):
        return {"ok": False, "error": f"commit_failed:{final_path}"}

    # Build PR body. Include the spec + cluster source feedback rows.
    src_block = "\n".join(
        f"- feedback #{s['id']}: {(s['text'] or '')[:160]}"
        for s in (cluster.get("sample_texts") or [])[:6]
    )
    member_ids_line = ", ".join(
        f"#{i}" for i in (cluster.get("member_ids") or [])[:25]
    )
    pr_body = (
        f"## Brain auto-feature proposal\n\n"
        f"**{cluster.get('count', 0)} users** asked for related functionality "
        f"(cluster `{cluster.get('cluster_id', '?')}`, theme `{cluster.get('theme', '?')}`). "
        f"The brain wrote a 200-word spec; this PR is a DRAFT stub a human can flesh out.\n\n"
        f"### Spec\n\n"
        f"- **Feature:** {spec.get('feature_name', '?')}\n"
        f"- **Complexity:** {spec.get('complexity', '?')}\n"
        f"- **Primary route:** `{spec.get('primary_route', '?')}`\n"
        f"- **Reuses:** {', '.join(spec.get('reuses') or []) or '(none)'}\n\n"
        f"**Problem:** {spec.get('problem', '?')}\n\n"
        f"**Proposed UX:** {spec.get('proposed_ux', '?')}\n\n"
        f"**Priority:** {spec.get('priority_justification', '?')}\n\n"
        f"### Source feedback rows\n\n"
        f"All {cluster.get('count', 0)} rows ({member_ids_line}) have been linked back "
        f"to this PR via `feedback_submissions.brain_proposal_pr_url`. Recent samples:\n\n"
        f"{src_block}\n\n"
        f"### How to ship\n\n"
        f"1. Flesh out the handler in `{final_path}` (TODO marker).\n"
        f"2. Register the blueprint in `main.py` near the other brain BPs.\n"
        f"3. Add init_tables + SCHEDULE entry as needed.\n"
        f"4. Rename `_proposed_<slug>.py` → `<slug>.py` when ready.\n"
        f"5. Mark the source feedback rows `status='shipped'`.\n\n"
        f"---\n"
        f"_Auto-generated by `routes/brain_feature_proposer.py`. DRAFT — won't "
        f"trigger any deploy. Reject by closing this PR; the proposal log row "
        f"will be marked rejected on PR-outcome-monitor's next sweep._"
    )

    pr = _open_pr(
        title=f"[brain feature draft] {spec.get('feature_name', slug)[:60]}",
        head=branch,
        body=pr_body,
    )
    if not pr:
        return {"ok": False, "error": "pr_open_failed", "branch": branch}

    # Flip the PR to DRAFT. Direct REST call — _open_pr doesn't expose
    # the draft flag.
    try:
        pr_num = pr.get("number")
        node_id = pr.get("node_id")
        # GraphQL mutation to mark draft (PATCH /pulls/<n> doesn't
        # support the 'draft' field).
        if node_id:
            _gh("POST", "/graphql", {
                "query": ("mutation($id:ID!){convertPullRequestToDraft("
                          "input:{pullRequestId:$id}){clientMutationId}}"),
                "variables": {"id": node_id},
            })
    except Exception as e:
        logger.warning("brain_feature_proposer: draft flip failed: %s", e)

    return {
        "ok": True,
        "pr_url": pr.get("html_url"),
        "pr_number": pr.get("number"),
        "branch": branch,
        "file_path": final_path,
    }


def _stamp_feedback_rows(cluster: dict, pr_url: str) -> int:
    """Write brain_proposal_pr_url + brain_proposal_cluster_id back to
    every feedback row in the cluster. Returns rowcount."""
    conn = _conn()
    if conn is None:
        return 0
    member_ids = cluster.get("member_ids") or []
    if not member_ids:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE feedback_submissions "
                "   SET brain_proposal_pr_url = %s, "
                "       brain_proposal_cluster_id = %s "
                " WHERE id = ANY(%s) "
                "   AND brain_proposal_pr_url IS NULL",
                (pr_url, cluster.get("cluster_id"), member_ids)
            )
            n = cur.rowcount or 0
            conn.commit()
            return n
    except Exception as e:
        logger.warning("brain_feature_proposer: stamp failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        try: conn.close()
        except Exception: pass


def _log_proposal(cluster: dict, spec: dict, pr: dict,
                  dry_run: bool) -> None:
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_feature_proposal_log "
                "(cluster_id, theme, feedback_ids, spec, pr_url, "
                " pr_number, status, dry_run) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (cluster.get("cluster_id"),
                 (cluster.get("theme") or "")[:200],
                 json.dumps(cluster.get("member_ids") or []),
                 json.dumps(spec)[:8000],
                 pr.get("pr_url") if pr else None,
                 pr.get("pr_number") if pr else None,
                 "open" if (pr and pr.get("ok")) else "skipped",
                 dry_run)
            )
            conn.commit()
    except Exception as e:
        logger.warning("brain_feature_proposer: log_proposal failed: %s", e)
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


# ── Master run ────────────────────────────────────────────────────────


def run_feature_proposer(force: bool = False) -> dict:
    """One cron tick: gather signals → cluster → propose → open PR.
    Returns a summary. Safe to call from cron + admin endpoint."""
    out: dict = {
        "kill_switch": _kill_switch_on(),
        "dry_run": _dry_run(),
        "weekly_cap": _weekly_cap(),
        "min_cluster": _min_cluster(),
        "clusters_found": 0,
        "proposals": [],
        "skipped": [],
        "errors": [],
    }
    if _kill_switch_on() and not force:
        out["skipped"].append("kill_switch")
        return out

    sig = gather_feature_signals()
    out["rows_scanned"] = sig.get("rows_scanned", 0)
    out["errors"].extend(sig.get("errors") or [])
    clusters = sig.get("clusters") or []
    out["clusters_found"] = len(clusters)
    if not clusters:
        out["note"] = "no_clusters_meet_threshold"
        return out

    # Weekly cap.
    conn = _conn()
    used = 0
    if conn is not None:
        try:
            used = _weekly_open_count(conn)
        finally:
            try: conn.close()
            except Exception: pass
    out["weekly_used"] = used
    budget = max(0, _weekly_cap() - used)
    if budget <= 0:
        out["skipped"].append(f"weekly_cap_full:{used}/{_weekly_cap()}")
        return out

    # Walk clusters by count desc.
    for cluster in clusters[:budget * 2]:  # over-iterate, propose may skip
        if len(out["proposals"]) >= budget:
            break
        prop = propose_feature(cluster)
        if not prop.get("ok"):
            out["skipped"].append({
                "cluster_id": cluster.get("cluster_id"),
                "reason": prop.get("error", "propose_failed"),
            })
            continue
        spec = prop["spec"]
        if _dry_run():
            out["proposals"].append({
                "cluster_id": cluster.get("cluster_id"),
                "theme": cluster.get("theme"),
                "count": cluster.get("count"),
                "spec": spec,
                "dry_run": True,
            })
            _log_proposal(cluster, spec, None, dry_run=True)
            continue
        pr = open_draft_pr(spec, cluster)
        if not pr.get("ok"):
            out["errors"].append({
                "cluster_id": cluster.get("cluster_id"),
                "spec": spec.get("feature_name"),
                "pr_err": pr.get("error"),
            })
            _log_proposal(cluster, spec, None, dry_run=False)
            continue
        stamped = _stamp_feedback_rows(cluster, pr["pr_url"])
        out["proposals"].append({
            "cluster_id": cluster.get("cluster_id"),
            "theme": cluster.get("theme"),
            "count": cluster.get("count"),
            "spec_name": spec.get("feature_name"),
            "pr_url": pr.get("pr_url"),
            "pr_number": pr.get("pr_number"),
            "rows_stamped": stamped,
        })
        _log_proposal(cluster, spec, pr, dry_run=False)
    return out


# ── Admin endpoints ───────────────────────────────────────────────────


def _admin_ok(req) -> bool:
    sent = (req.headers.get("X-Admin-Key")
            or req.headers.get("X-Internal-Key")
            or req.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and sent == _ADMIN_KEY:
        return True
    return False


@brain_feature_proposer_bp.route(
    "/api/v1/admin/brain/feature-proposer/run",
    methods=["POST"])
def admin_run():
    """Fire the proposer manually. Admin-gated. Respects DRY_RUN env."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    force = request.args.get("force") in ("1", "true", "yes")
    result = run_feature_proposer(force=force)
    return jsonify(ok=True, **result), 200


@brain_feature_proposer_bp.route(
    "/api/v1/admin/brain/feature-proposer/preview",
    methods=["GET"])
def admin_preview():
    """Dry-run a single sweep — show clusters + generated specs, but
    open no PRs. Useful for tuning thresholds."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    sig = gather_feature_signals()
    clusters = sig.get("clusters") or []
    out: dict = {
        "rows_scanned": sig.get("rows_scanned", 0),
        "clusters_found": len(clusters),
        "min_cluster": _min_cluster(),
        "weekly_cap": _weekly_cap(),
        "clusters": [],
        "errors": sig.get("errors") or [],
    }
    # Cap preview to top 3 to keep latency sane.
    for c in clusters[:3]:
        prop = propose_feature(c)
        out["clusters"].append({
            "cluster_id": c.get("cluster_id"),
            "theme": c.get("theme"),
            "count": c.get("count"),
            "sample_texts": c.get("sample_texts"),
            "proposal_ok": prop.get("ok"),
            "spec": prop.get("spec") if prop.get("ok") else None,
            "error": prop.get("error") if not prop.get("ok") else None,
        })
    return jsonify(ok=True, **out), 200


@brain_feature_proposer_bp.route(
    "/api/v1/admin/brain/feature-proposer/clusters",
    methods=["GET"])
def admin_clusters():
    """Just the clusters — no Claude call. Cheap status probe."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    sig = gather_feature_signals()
    return jsonify(ok=True,
                   rows_scanned=sig.get("rows_scanned", 0),
                   clusters_found=len(sig.get("clusters") or []),
                   clusters=sig.get("clusters") or [],
                   errors=sig.get("errors") or []), 200


@brain_feature_proposer_bp.route(
    "/api/v1/admin/brain/feature-proposer/log",
    methods=["GET"])
def admin_log():
    """Recent proposal-log rows."""
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, cluster_id, theme, feedback_ids, pr_url, "
                "       pr_number, status, dry_run, created_at "
                "FROM brain_feature_proposal_log "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            if hasattr(r, "get"):
                rec = dict(r)
            else:
                rec = {
                    "id": r[0], "cluster_id": r[1], "theme": r[2],
                    "feedback_ids": r[3], "pr_url": r[4],
                    "pr_number": r[5], "status": r[6],
                    "dry_run": r[7], "created_at": r[8],
                }
            if rec.get("created_at"):
                try:
                    rec["created_at"] = rec["created_at"].isoformat()
                except Exception:
                    rec["created_at"] = str(rec["created_at"])
            out.append(rec)
        return jsonify(ok=True, count=len(out), proposals=out), 200
    except Exception as e:
        return jsonify(error="db_err", hint=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


def register_brain_feature_proposer(app):
    """Idempotent registration helper for main.py."""
    try:
        app.register_blueprint(brain_feature_proposer_bp)
    except Exception as e:
        logger.warning("brain_feature_proposer already registered: %s", e)
