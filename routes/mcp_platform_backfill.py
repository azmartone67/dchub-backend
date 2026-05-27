"""
mcp_platform_backfill.py — r47.34 (2026-05-26).

The r47.30 mcp-server fix (detectPlatformFromInit reading clientInfo.name
per MCP spec) tags new init calls correctly going forward, but ~109K
historical rows in `mcp_call_log` still carry `platform='mcp'` — the
generic catch-all bucket that meant "we saw the request but couldn't
classify the client." That hides real platform diversity in the
citations endpoint + visitor-intel page.

This blueprint exposes a one-shot reclassifier:

  POST /api/v1/admin/mcp/backfill-platforms      (X-Admin-Key)

It walks rows where `platform IN ('mcp','','unknown')` and re-applies
the same UA-pattern matching the citations endpoint uses, then UPDATEs
the platform column for any rows whose UA actually has a useful marker.
Returns per-platform reclassification counts.

Idempotent. Safe to re-run. Rows whose UA truly is generic ('node',
'fetch') stay tagged 'mcp' — they're genuinely unclassifiable.
"""
import os
import logging
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

logger = logging.getLogger(__name__)
mcp_platform_backfill_bp = Blueprint("mcp_platform_backfill", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _is_internal_key(req):
    """Reuse the same auth pattern the rest of the admin endpoints use."""
    provided = req.headers.get("X-Admin-Key") or req.headers.get("X-Internal-Key")
    if not provided:
        return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    # Fallback to is_valid_internal_key if available
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(provided))
    except Exception:
        return False


# UA / referrer pattern → canonical platform tag. Mirrors the citation
# endpoint's _UA_RULES order so reclassification is consistent.
_RULES = [
    # Direct UA markers
    ("claude",          "claude"),
    ("claudebot",       "claude"),
    ("mcp-remote",      "claude"),
    ("chatgpt",         "chatgpt"),
    ("gptbot",          "chatgpt"),
    ("openai-",         "chatgpt"),
    ("perplexity",      "perplexity"),
    ("perplexitybot",   "perplexity"),
    ("cursor",          "cursor"),
    ("cline",           "cline"),
    ("continue",        "continue"),
    ("continue.dev",    "continue"),
    ("windsurf",        "windsurf"),
    ("gemini",          "gemini"),
    ("google-extended", "gemini"),
    ("googlebot",       "googlebot"),
    ("groq",            "groq"),
    ("nvidia",          "nvidia"),
    ("grok",            "grok"),
    ("copilot",         "copilot"),
    ("meta-external",   "meta"),
    ("bytespider",      "bytedance"),
    ("petalbot",        "huawei"),
    ("amazonbot",       "amazon"),
    ("ccbot",           "commoncrawl"),
    ("anthropic-ai",    "anthropic-crawler"),
]


def _classify(ua: str, referrer: str) -> str:
    """Return a canonical platform tag, or '' if nothing matches."""
    if not ua and not referrer:
        return ""
    blob = ((ua or "") + " " + (referrer or "")).lower()
    if not blob.strip():
        return ""
    for pat, tag in _RULES:
        if pat in blob:
            return tag
    return ""


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/backfill-platforms",
                                 methods=["POST"], strict_slashes=False)
def backfill_platforms():
    """Reclassify mcp_call_log rows where platform is generic.

    Admin only (X-Admin-Key or X-Internal-Key). Returns per-platform
    reclassification counts so the caller can verify what got tagged."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401

    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    # Bound the work — accept ?max=N (default 50000)
    try:
        max_rows = int(request.args.get("max", 50000))
    except (TypeError, ValueError):
        max_rows = 50000
    max_rows = max(100, min(max_rows, 200000))

    updated_by_platform: dict = {}
    skipped = 0
    scanned = 0
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, user_agent, COALESCE(referrer, '')
                  FROM mcp_call_log
                 WHERE platform IN ('mcp', '', 'unknown')
                    OR platform IS NULL
                 ORDER BY id DESC
                 LIMIT %s
            """, (max_rows,))
            rows = cur.fetchall()
            scanned = len(rows)

            for row_id, ua, referrer in rows:
                tag = _classify(ua or "", referrer or "")
                if not tag:
                    skipped += 1
                    continue
                cur.execute(
                    "UPDATE mcp_call_log SET platform = %s WHERE id = %s",
                    (tag, row_id),
                )
                updated_by_platform[tag] = updated_by_platform.get(tag, 0) + 1
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

    return jsonify({
        "ok":         True,
        "scanned":    scanned,
        "skipped":    skipped,
        "updated":    sum(updated_by_platform.values()),
        "by_platform": dict(sorted(updated_by_platform.items(),
                                    key=lambda kv: -kv[1])),
        "hint":       ("Re-run until 'updated' approaches 0 to clear the "
                       "backlog. Remaining 'skipped' rows are genuinely "
                       "unclassifiable (UA='node', no referrer markers)."),
    }), 200


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/backfill-platforms/preview",
                                 methods=["GET"], strict_slashes=False)
def preview_backfill():
    """Read-only preview: what WOULD reclassify if we ran the full update.

    Admin-only. Useful for sanity-checking the rule set before running the
    real backfill — same UA pattern matching, but no DB writes."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401

    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        sample = int(request.args.get("sample", 5000))
    except (TypeError, ValueError):
        sample = 5000
    sample = max(100, min(sample, 50000))

    would_update: dict = {}
    unclassified = 0
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT user_agent, COALESCE(referrer, '')
                  FROM mcp_call_log
                 WHERE platform IN ('mcp', '', 'unknown')
                    OR platform IS NULL
                 ORDER BY id DESC
                 LIMIT %s
            """, (sample,))
            for ua, ref in cur.fetchall():
                tag = _classify(ua or "", ref or "")
                if tag:
                    would_update[tag] = would_update.get(tag, 0) + 1
                else:
                    unclassified += 1
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

    return jsonify({
        "sample_size":  sample,
        "would_update": dict(sorted(would_update.items(),
                                     key=lambda kv: -kv[1])),
        "unclassified": unclassified,
        "hint":         ("Run POST /api/v1/admin/mcp/backfill-platforms with "
                          "X-Admin-Key to apply. Add ?max=N to bound the work."),
    }), 200
