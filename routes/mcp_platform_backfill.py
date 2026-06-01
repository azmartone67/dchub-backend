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
    # r61: keep in sync with ai_tracking.AI_PLATFORMS so backfill tags the
    # same partners the live classifier now recognizes.
    ("cohere",          "cohere"),
    ("cohere-ai",       "cohere"),
    ("youbot",          "you"),
    ("you.com",         "you"),
    ("huggingface",     "huggingface"),
    ("hf_hub",          "huggingface"),
    ("mistralai",       "mistral"),
    ("mistral",         "mistral"),
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


# r47.35 (2026-05-26): the first backfill attempt found 0 reclassifiable
# rows because `mcp_call_log.user_agent` is always 'node' (the dchub-mcp-
# server's outbound UA) and `referrer` is empty for server-to-server
# JSON-RPC. The historical client identity is genuinely lost from those
# columns. But we still have ONE recoverable signal: `api_key`. Each MCP
# caller registered with a developer email + an api_key name, and those
# fields routinely encode the client ("claude-prod", "cursor-test",
# "azmartone+chatgpt@…"). This second backfill mines those tables to
# build a key → platform lookup and applies it across mcp_call_log.

def _classify_key_attribution(email: str, name: str, metadata) -> str:
    """Same rule set as _classify(), but applied to key-attribution
    fields instead of UA/referrer. Returns a platform tag or ''."""
    blobs = []
    for v in (email, name):
        if v:
            blobs.append(v.lower())
    # metadata can be jsonb dict or a JSON-encoded string
    if metadata:
        try:
            if isinstance(metadata, dict):
                for k, v in metadata.items():
                    if isinstance(v, str):
                        blobs.append(f"{k.lower()}:{v.lower()}")
            elif isinstance(metadata, str):
                blobs.append(metadata.lower())
        except Exception:
            pass
    if not blobs:
        return ""
    blob = " ".join(blobs)
    for pat, tag in _RULES:
        if pat in blob:
            return tag
    return ""


def _build_key_to_platform_lookup(cur) -> dict:
    """Build {api_key: platform_tag} from mcp_dev_keys + api_keys.

    mcp_dev_keys.email / .metadata + api_keys.name are the strongest
    signals. Returns only keys we could classify — anything else stays
    in the generic 'mcp' bucket truthfully.
    """
    lookup: dict = {}

    # Source 1: mcp_dev_keys.email + metadata
    try:
        cur.execute("""
            SELECT api_key, COALESCE(email, ''), metadata
              FROM mcp_dev_keys
             WHERE api_key IS NOT NULL AND api_key <> ''
        """)
        for api_key, email, metadata in cur.fetchall():
            tag = _classify_key_attribution(email, "", metadata)
            if tag:
                lookup[api_key] = tag
    except Exception:
        pass

    # Source 2: api_keys.name (the key's human-assigned label)
    # Join on key_hash → mcp_call_log.api_key fails if formats differ,
    # so we instead match by full key for keys whose `name` looks
    # platform-identifying. Skip if no overlap.
    try:
        cur.execute("""
            SELECT key_hash, key_prefix, COALESCE(name, '')
              FROM api_keys
             WHERE name IS NOT NULL AND name <> ''
        """)
        for key_hash, key_prefix, name in cur.fetchall():
            tag = _classify_key_attribution("", name, None)
            if not tag:
                continue
            # Two possible storage shapes for mcp_call_log.api_key —
            # raw vs hashed. Cover both.
            if key_hash and key_hash not in lookup:
                lookup[key_hash] = tag
            if key_prefix and key_prefix not in lookup:
                lookup[key_prefix] = tag
    except Exception:
        pass

    return lookup


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/backfill-via-keys",
                                 methods=["POST"], strict_slashes=False)
def backfill_via_keys():
    """JOIN-based reclassifier. Use after the UA-pattern backfill returns 0.

    Admin only (X-Admin-Key or X-Internal-Key). Mines mcp_dev_keys +
    api_keys for client-identifying attribution (email patterns, name
    labels, metadata.client) and applies it to mcp_call_log rows where
    platform is still generic. Returns updated counts per platform.

    Idempotent. Safe to re-run."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        max_rows = int(request.args.get("max", 100000))
    except (TypeError, ValueError):
        max_rows = 100000
    max_rows = max(1000, min(max_rows, 500000))

    updated_by_platform: dict = {}
    total_updated = 0
    keys_classified = 0
    keys_seen = 0

    try:
        with _conn() as c, c.cursor() as cur:
            lookup = _build_key_to_platform_lookup(cur)
            keys_classified = len(lookup)

            cur.execute("""
                SELECT COUNT(DISTINCT api_key)
                  FROM mcp_call_log
                 WHERE (platform IN ('mcp', '', 'unknown') OR platform IS NULL)
                   AND api_key IS NOT NULL AND api_key <> ''
            """)
            keys_seen = int((cur.fetchone() or [0])[0])

            if lookup:
                # Single-pass UPDATE per platform tag — much faster than
                # per-row updates. Bound by max_rows total via a CTE.
                running_total = 0
                for plat, keys_for_plat in _group_by_value(lookup).items():
                    if running_total >= max_rows:
                        break
                    remaining = max_rows - running_total
                    cur.execute("""
                        WITH targets AS (
                          SELECT id FROM mcp_call_log
                           WHERE (platform IN ('mcp','','unknown')
                                  OR platform IS NULL)
                             AND api_key = ANY(%s)
                           ORDER BY id DESC
                           LIMIT %s
                        )
                        UPDATE mcp_call_log m
                           SET platform = %s
                          FROM targets t
                         WHERE m.id = t.id
                        RETURNING m.id
                    """, (keys_for_plat, remaining, plat))
                    n = len(cur.fetchall())
                    if n:
                        updated_by_platform[plat] = n
                        total_updated += n
                        running_total += n
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    return jsonify({
        "ok":              True,
        "keys_classified": keys_classified,
        "distinct_keys_in_backlog": keys_seen,
        "updated":         total_updated,
        "by_platform":     dict(sorted(updated_by_platform.items(),
                                        key=lambda kv: -kv[1])),
        "hint":            ("Re-run if you raise ?max=. Keys not in the "
                             "lookup are genuinely anonymous — the original "
                             "developer didn't put a platform hint in their "
                             "email or key name. Future r47.30 platform "
                             "tagging covers those going forward."),
    }), 200


def _group_by_value(d: dict) -> dict:
    """Invert {key: tag} → {tag: [key, key, ...]}."""
    out: dict = {}
    for k, v in d.items():
        out.setdefault(v, []).append(k)
    return out


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/backfill-via-keys/diagnose",
                                 methods=["GET"], strict_slashes=False)
def diagnose_key_join():
    """r47.35.1: explain why backfill-via-keys returns 0.

    Compares the key formats in mcp_call_log vs mcp_dev_keys vs api_keys
    side-by-side so the operator can see whether the JOIN is failing on
    format mismatch (hashed vs raw vs prefix). Prefixes only — never
    returns full keys."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    out: dict = {
        "mcp_call_log_backlog": {"distinct_keys": 0, "sample": []},
        "mcp_dev_keys":         {"count": 0, "sample": []},
        "api_keys":             {"count": 0, "sample": []},
    }
    try:
        with _conn() as c, c.cursor() as cur:
            # mcp_call_log: the 27 unclassified keys + their call counts
            cur.execute("""
                SELECT api_key, COUNT(*) AS n
                  FROM mcp_call_log
                 WHERE (platform IN ('mcp','','unknown') OR platform IS NULL)
                   AND api_key IS NOT NULL AND api_key <> ''
                 GROUP BY api_key
                 ORDER BY n DESC
                 LIMIT 30
            """)
            rows = cur.fetchall() or []
            out["mcp_call_log_backlog"]["distinct_keys"] = len(rows)
            out["mcp_call_log_backlog"]["sample"] = [{
                "api_key_prefix": (r[0] or '')[:14] + '…',
                "api_key_len":    len(r[0] or ''),
                "call_count":     int(r[1]),
            } for r in rows[:15]]

            # mcp_dev_keys: format of api_key column
            cur.execute("""
                SELECT api_key, email, tier
                  FROM mcp_dev_keys
                 WHERE api_key IS NOT NULL AND api_key <> ''
                 ORDER BY last_used_at DESC NULLS LAST
                 LIMIT 15
            """)
            dev_rows = cur.fetchall() or []
            out["mcp_dev_keys"]["count"] = len(dev_rows)
            out["mcp_dev_keys"]["sample"] = [{
                "api_key_prefix": (r[0] or '')[:14] + '…',
                "api_key_len":    len(r[0] or ''),
                "email_domain":   (r[1] or '').split('@')[-1] if r[1] else '',
                "tier":           r[2],
            } for r in dev_rows]

            # api_keys: format of key_hash + key_prefix
            cur.execute("""
                SELECT key_hash, key_prefix, name, plan
                  FROM api_keys
                 WHERE is_active = 1 OR is_active_bool = TRUE
                 ORDER BY last_used DESC NULLS LAST
                 LIMIT 15
            """)
            ak_rows = cur.fetchall() or []
            out["api_keys"]["count"] = len(ak_rows)
            out["api_keys"]["sample"] = [{
                "hash_prefix":   (r[0] or '')[:14] + '…',
                "hash_len":      len(r[0] or ''),
                "key_prefix":    (r[1] or '')[:14] + '…',
                "name":          (r[2] or '')[:40],
                "plan":          r[3],
            } for r in ak_rows]
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    out["diagnosis_hint"] = (
        "Compare api_key_prefix + api_key_len across the three tables. "
        "If mcp_call_log stores 64-char hex hashes while mcp_dev_keys stores "
        "32-char tokens, the JOIN can't find overlaps. Fix: either (a) hash "
        "the dev key the same way before lookup, or (b) UPDATE individual "
        "rows via the top-N keys here with manual platform tagging via "
        "POST /api/v1/admin/mcp/tag-key (X-Admin-Key)."
    )
    return jsonify(out), 200


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/tag-key",
                                 methods=["POST"], strict_slashes=False)
def tag_key_manual():
    """r47.35.1: manual one-shot tag for a specific api_key (prefix or full).

    Body fields:
      api_key_prefix  — partial prefix match (api_key LIKE prefix%)
      api_key         — exact full-key match (faster, more precise)
      platform        — target tag (required)
      from_platform   — optional, comma-separated list of CURRENT platform
                        values to widen the UPDATE filter. Default:
                        'mcp,,unknown'. Pass an explicit list to re-tag
                        rows already tagged something else (fix typos).

    r47.35.2: chunked UPDATE. The original implementation issued ONE
    big UPDATE for the 109K-row key — the Pages worker's 5s subrequest
    timeout cancelled the request before Postgres acknowledged the
    commit. Now we loop in 5,000-row chunks so each commit is fast
    (<200ms) and the total round-trip stays comfortably under the
    worker's timeout window."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    data = request.get_json(silent=True) or {}
    prefix = (data.get("api_key_prefix") or "").strip()
    full   = (data.get("api_key") or "").strip()
    plat   = (data.get("platform") or "").strip().lower()
    if not plat:
        return jsonify({"error": "platform required"}), 400
    if not (prefix or full):
        return jsonify({"error": "api_key_prefix or api_key required"}), 400

    # Build the "current platform" filter list. Default = generic buckets.
    from_raw = (data.get("from_platform") or "").strip()
    if from_raw:
        from_platforms = [p.strip() for p in from_raw.split(',') if p.strip()]
    else:
        from_platforms = ['mcp', '', 'unknown']

    # Match condition
    if full:
        match_sql = "api_key = %s"
        match_param = full
    else:
        match_sql = "api_key LIKE %s"
        match_param = prefix + '%'

    CHUNK = 5000
    total_updated = 0
    chunks = 0
    try:
        with _conn() as c, c.cursor() as cur:
            while True:
                chunks += 1
                cur.execute(f"""
                    WITH targets AS (
                        SELECT id FROM mcp_call_log
                         WHERE {match_sql}
                           AND platform = ANY(%s)
                         LIMIT {CHUNK}
                    )
                    UPDATE mcp_call_log m SET platform = %s
                      FROM targets t WHERE m.id = t.id
                """, (match_param, from_platforms, plat))
                n = cur.rowcount or 0
                total_updated += n
                if n < CHUNK:
                    break
                # Safety: don't loop forever on a bad UPDATE
                if chunks > 200:
                    break
    except Exception as e:
        return jsonify({"error": str(e)[:300],
                         "partial_updated": total_updated}), 500

    return jsonify({
        "ok":       True,
        "updated":  total_updated,
        "chunks":   chunks,
        "platform": plat,
        "from_platform": from_platforms,
        "matched":  "full_key" if full else f"prefix:{prefix}",
    }), 200


@mcp_platform_backfill_bp.route("/api/v1/admin/mcp/backfill-via-keys/preview",
                                 methods=["GET"], strict_slashes=False)
def preview_backfill_via_keys():
    """Read-only preview: what the JOIN-based backfill WOULD reclassify.

    Same admin auth. Counts (a) how many keys we can classify, (b) how
    many of those keys actually appear in mcp_call_log's 'mcp'/'unknown'
    backlog, (c) how many rows would update. No writes."""
    if not _is_internal_key(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        with _conn() as c, c.cursor() as cur:
            lookup = _build_key_to_platform_lookup(cur)
            keys_classified = len(lookup)

            cur.execute("""
                SELECT COUNT(DISTINCT api_key)
                  FROM mcp_call_log
                 WHERE (platform IN ('mcp', '', 'unknown') OR platform IS NULL)
                   AND api_key IS NOT NULL AND api_key <> ''
            """)
            keys_in_backlog = int((cur.fetchone() or [0])[0])

            # Per-platform: how many rows would update
            would_by_platform: dict = {}
            for plat, keys_for_plat in _group_by_value(lookup).items():
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_call_log
                     WHERE (platform IN ('mcp','','unknown')
                            OR platform IS NULL)
                       AND api_key = ANY(%s)
                """, (keys_for_plat,))
                n = int((cur.fetchone() or [0])[0])
                if n:
                    would_by_platform[plat] = n
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    sample_keys = list(lookup.items())[:8]  # first 8 for sanity check
    return jsonify({
        "keys_classified":           keys_classified,
        "distinct_keys_in_backlog":  keys_in_backlog,
        "would_update_by_platform":  dict(sorted(would_by_platform.items(),
                                                  key=lambda kv: -kv[1])),
        "would_update_total":        sum(would_by_platform.values()),
        "sample_classifications":    [{"api_key_prefix": (k or '')[:8] + '…',
                                        "platform": v}
                                       for k, v in sample_keys],
        "hint":                      ("If would_update_total is 0 you have "
                                       "no email/name hints to mine. Add "
                                       "platform=X into mcp_dev_keys.metadata "
                                       "for known keys, then re-run."),
    }), 200


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
