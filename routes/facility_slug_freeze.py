"""
Facility slug FREEZE + persistent alias table (r-slug-freeze 2026-07-03).

ROOT CAUSE this closes (GSC "Page indexing" — ~8,300 not-indexed facility URLs):
    The canonical facility URL was `‹provider›-‹name›-MD5(provider|name)[:8]`,
    RECOMPUTED from live DB text on every request / sitemap build / internal
    link — never stored. So the URL is a hash of MUTABLE data. Every re-ingestion
    that cleans a name or provider string (17,028 of 21,861 rows are is_duplicate
    precisely BECAUSE provider strings vary) rewrites the whole slug, turning
    Google's already-indexed URL into a 301 or a hard 404. There was also no
    persistent old→new map, so heuristic recovery (_resolve_legacy_slug) could
    never hit 100% — the generic-name misses are the 3,206 hard-404 bucket.

THE FIX (two durable pieces):
  1. FREEZE — snapshot each facility's CURRENT canonical slug into a stored
     `canonical_slug` column ONCE, then serve / sitemap / link from the stored
     value. Re-ingestion updates name/power/etc. but NEVER touches canonical_slug
     (no code writes it except the fill-where-NULL backfill), so the URL can
     never move again. Frozen value == today's sitemap string byte-for-byte, so
     the freeze itself introduces ZERO new redirects.
  2. ALIAS TABLE — `facility_slug_aliases(old_slug → canonical_slug)`. Backfilled
     programmatically from the pre-2026-06-16 `MD5(id)[:8]` scheme (recovers the
     scheme-swap churn with no HTTP), and loadable from the GSC export (recovers
     name-change history the DB no longer holds). The live route consults it for
     a DETERMINISTIC single-hop 301 instead of a fuzzy guess.

Runs behind the existing fail-closed X-Admin-Key / DCHUB_ADMIN_KEY gate. All DDL
is idempotent; all backfills are set-once (WHERE canonical_slug IS NULL / ON
CONFLICT DO NOTHING) so re-running is always safe.
"""
import os
import re
import hashlib
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
slug_freeze_bp = Blueprint("slug_freeze", __name__)

# Tables that carry facility rows served under /facilities/<slug>.
_FACILITY_TABLES = ("discovered_facilities", "facilities")


# ─────────────────────────────────────────────────────────────────────────
# Canonical slug — BYTE-IDENTICAL to main.py _build_sitemap_sections + the
# live facility route. Do NOT "improve" the regex here without changing both;
# any drift re-mints duplicate URLs (the exact bug this file exists to kill).
# ─────────────────────────────────────────────────────────────────────────
def _slugify(text):
    if not text:
        return None
    s = str(text).lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    return s.strip('-')


def _stable_hash8(provider, name):
    # Mirror routes.facility_slug.stable_hash8 (import kept local so this module
    # never fails to load if that import path moves).
    return hashlib.md5(f"{provider or ''}|{name or ''}".encode("utf-8")).hexdigest()[:8]


def build_canonical_slug(provider, name):
    """Current canonical /facilities/<slug> segment, or None (name too short).
    Matches the sitemap's `{provider-slug}-{name-slug}-{stable_hash8}` exactly."""
    name_slug = _slugify(name) or ''
    if not name_slug or len(name_slug) < 3:
        return None
    provider_slug = _slugify(provider) or ''
    h = _stable_hash8(provider, name)
    return f"{provider_slug}-{name_slug}-{h}" if provider_slug else f"{name_slug}-{h}"


def build_id_scheme_slug(provider, name, fac_id):
    """The PRE-2026-06-16 slug: same name-part but hash keyed on MD5(id)[:8]
    (the old map/explorer scheme). This is the `old_slug` Google indexed before
    the r-stable-slug swap — we alias it → the current canonical."""
    if fac_id is None or str(fac_id) == '':
        return None
    name_slug = _slugify(name) or ''
    if not name_slug or len(name_slug) < 3:
        return None
    provider_slug = _slugify(provider) or ''
    h = hashlib.md5(str(fac_id).encode("utf-8")).hexdigest()[:8]
    return f"{provider_slug}-{name_slug}-{h}" if provider_slug else f"{name_slug}-{h}"


def frozen_slug_for_row(row):
    """Single source of truth for callers (sitemap, internal links).
    Prefer the STORED canonical_slug; fall back to a live compute only for rows
    not yet backfilled. `row` may be a dict or expose .get()."""
    try:
        stored = row.get("canonical_slug")
    except AttributeError:
        stored = None
    if stored:
        return stored
    prov = row.get("provider") if hasattr(row, "get") else None
    name = row.get("name") if hasattr(row, "get") else None
    return build_canonical_slug(prov, name)


# ─────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────
def _get_conn():
    from main import get_db
    return get_db()


def _column_exists(cur, table, col):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, col))
    return cur.fetchone() is not None


def ensure_freeze_schema(conn):
    """Idempotent DDL: add canonical_slug to both facility tables + create the
    alias table + indexes. Safe to call on every boot / every admin hit."""
    cur = conn.cursor()
    added = []
    for table in _FACILITY_TABLES:
        try:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if not cur.fetchone()[0]:
                continue  # table doesn't exist in this env — skip
            if not _column_exists(cur, table, "canonical_slug"):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN canonical_slug TEXT")
                added.append(f"{table}.canonical_slug")
            # Partial index — only backfilled rows, keeps it small + fast.
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_canonical_slug "
                f"ON {table} (canonical_slug) WHERE canonical_slug IS NOT NULL")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"freeze schema for {table}: {e}")
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facility_slug_aliases (
                old_slug       TEXT PRIMARY KEY,
                canonical_slug TEXT NOT NULL,
                facility_id    TEXT,
                source         TEXT,
                created_at     TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fac_alias_canonical "
            "ON facility_slug_aliases (canonical_slug)")
        conn.commit()
        added.append("facility_slug_aliases")
    except Exception as e:
        conn.rollback()
        logger.warning(f"freeze schema alias table: {e}")
    return added


def backfill_canonical_slugs(conn, table, batch=2000, max_batches=50):
    """Set canonical_slug for rows where it IS NULL (set-once — the WHERE guard
    means re-ingestion / re-runs can never overwrite a frozen value). Returns
    (updated, remaining_estimate)."""
    cur = conn.cursor()
    updated = 0
    for _ in range(max_batches):
        cur.execute(f"""
            SELECT id, provider, name FROM {table}
            WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''
            LIMIT %s
        """, (batch,))
        rows = cur.fetchall()
        if not rows:
            break
        for fid, provider, name in rows:
            slug = build_canonical_slug(provider, name)
            if not slug:
                # name too short to slug — park a sentinel so we don't re-scan
                # it forever. It will 404 (correct: not an indexable page).
                cur.execute(
                    f"UPDATE {table} SET canonical_slug = '' "
                    f"WHERE id = %s AND canonical_slug IS NULL", (fid,))
                continue
            cur.execute(
                f"UPDATE {table} SET canonical_slug = %s "
                f"WHERE id = %s AND canonical_slug IS NULL", (slug, fid))
            updated += 1
        conn.commit()
        if len(rows) < batch:
            break
    cur.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
    remaining = cur.fetchone()[0]
    return updated, remaining


def backfill_id_scheme_aliases(conn, table, batch=2000, max_batches=50):
    """For every facility, alias its PRE-swap MD5(id)[:8] slug → current
    canonical. Recovers the 2026-06-16 scheme-swap churn (the bulk of GSC's
    'Page with redirect' + a chunk of the 404s) with zero HTTP. Set-once."""
    cur = conn.cursor()
    inserted = 0
    offset = 0
    for _ in range(max_batches):
        cur.execute(f"""
            SELECT id, provider, name, canonical_slug FROM {table}
            WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''
            ORDER BY id LIMIT %s OFFSET %s
        """, (batch, offset))
        rows = cur.fetchall()
        if not rows:
            break
        pairs = []
        for fid, provider, name, canonical in rows:
            old = build_id_scheme_slug(provider, name, fid)
            if old and old != canonical:
                pairs.append((old, canonical, str(fid)))
        for old, canonical, fid in pairs:
            cur.execute("""
                INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
                VALUES (%s, %s, %s, 'id-scheme')
                ON CONFLICT (old_slug) DO NOTHING
            """, (old, canonical, fid))
            inserted += cur.rowcount
        conn.commit()
        offset += len(rows)
        if len(rows) < batch:
            break
    return inserted


def load_aliases(conn, rows, source="manual"):
    """Bulk-load [(old_slug, canonical_slug[, facility_id]), ...]. Explicit
    loads (e.g. GSC-export capture) win over programmatic ones — DO UPDATE."""
    cur = conn.cursor()
    loaded = 0
    for r in rows:
        old = (r[0] or "").strip().lstrip("/")
        canon = (r[1] or "").strip().lstrip("/")
        fid = str(r[2]) if len(r) > 2 and r[2] is not None else None
        if not old or not canon or old == canon:
            continue
        cur.execute("""
            INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (old_slug) DO UPDATE
              SET canonical_slug = EXCLUDED.canonical_slug, source = EXCLUDED.source
        """, (old, canon, fid, source))
        loaded += 1
    conn.commit()
    return loaded


def resolve_alias(slug):
    """old_slug → current canonical_slug (or None). Authoritative + fast (PK
    lookup). Called by the live route before the fuzzy fallback."""
    if not slug:
        return None
    s = slug[:-5] if slug.endswith(".html") else slug
    s = s.split("/")[0].strip().lstrip("/")
    conn = None
    try:
        conn = _get_conn()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute(
            "SELECT canonical_slug FROM facility_slug_aliases WHERE old_slug = %s LIMIT 1",
            (s,))
        row = cur.fetchone()
        return row[0] if row and row[0] and row[0] != s else None
    except Exception as e:
        logger.warning(f"resolve_alias failed: {e}")
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────
# Admin endpoints — fail-closed X-Admin-Key gate (same as sitemap purge)
# ─────────────────────────────────────────────────────────────────────────
def _admin_guard():
    """Returns (ok, error_response). Fail-closed: refuse if key unconfigured."""
    admin_key = (os.environ.get('DCHUB_ADMIN_KEY')
                 or os.environ.get('ADMIN_KEY') or '').strip()
    provided = (request.headers.get('X-Admin-Key') or '').strip()
    if not admin_key:
        return False, (jsonify(
            error='admin_endpoint_unconfigured',
            hint='Set DCHUB_ADMIN_KEY on the Railway service (fail-closed).'), 503)
    if provided != admin_key:
        return False, (jsonify(
            error='unauthorized',
            hint='set X-Admin-Key header to DCHUB_ADMIN_KEY'), 401)
    return True, None


@slug_freeze_bp.route('/api/v1/admin/slug/status', methods=['GET'])
def slug_freeze_status():
    ok, err = _admin_guard()
    if not ok:
        return err
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        out = {'tables': {}}
        for t in _FACILITY_TABLES:
            cur.execute("SELECT to_regclass(%s)", (t,))
            if not cur.fetchone()[0]:
                continue
            has_col = _column_exists(cur, t, 'canonical_slug')
            frozen = pending = None
            if has_col:
                cur.execute(f"SELECT COUNT(*) FROM {t} WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''")
                frozen = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {t} WHERE canonical_slug IS NULL AND name IS NOT NULL AND name <> ''")
                pending = cur.fetchone()[0]
            out['tables'][t] = {'has_canonical_slug_col': has_col,
                                'frozen': frozen, 'pending': pending}
        try:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM facility_slug_aliases")
            n, nsrc = cur.fetchone()
            cur.execute("SELECT source, COUNT(*) FROM facility_slug_aliases GROUP BY source")
            by_src = {r[0]: r[1] for r in cur.fetchall()}
            out['aliases'] = {'total': n, 'sources': by_src}
        except Exception:
            out['aliases'] = {'total': 0, 'sources': {}, 'note': 'alias table not created yet'}
        return jsonify(out)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


@slug_freeze_bp.route('/api/v1/admin/slug/freeze', methods=['POST'])
def slug_freeze_run():
    """Idempotent one-shot: ensure schema, freeze canonical_slug for both
    tables, then backfill the id-scheme aliases. Re-run until 'pending' is 0
    (each call is bounded by max_batches to stay under request timeouts)."""
    ok, err = _admin_guard()
    if not ok:
        return err
    max_batches = int(request.args.get('max_batches', 50))
    conn = None
    try:
        conn = _get_conn()
        if not conn:
            return jsonify(error='db_unavailable'), 503
        schema = ensure_freeze_schema(conn)
        result = {'schema_changes': schema, 'freeze': {}, 'aliases': {}}
        for t in _FACILITY_TABLES:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass(%s)", (t,))
            if not cur.fetchone()[0]:
                continue
            updated, remaining = backfill_canonical_slugs(conn, t, max_batches=max_batches)
            result['freeze'][t] = {'newly_frozen': updated, 'pending': remaining}
        for t in _FACILITY_TABLES:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass(%s)", (t,))
            if not cur.fetchone()[0]:
                continue
            ins = backfill_id_scheme_aliases(conn, t, max_batches=max_batches)
            result['aliases'][t] = {'id_scheme_aliases_added': ins}
        result['ok'] = True
        result['note'] = ('Re-POST until every table pending=0. Then the route + '
                          'sitemap serve the frozen slug; old MD5(id) URLs 301 via '
                          'the alias table. Purge the sitemap cache next: '
                          'POST /api/v1/admin/sitemap/purge')
        return jsonify(result)
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        logger.error(f"slug freeze run failed: {e}")
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


@slug_freeze_bp.route('/api/v1/admin/slug/alias-resolve', methods=['POST'])
def slug_alias_resolve():
    """Resolve a batch of OLD slugs (e.g. the exact URLs from a GSC coverage
    export) IN-PROD via the live fuzzy resolver, then persist the confident
    matches as permanent aliases. This recovers name-change history the DB no
    longer holds, with NO external HTTP (so it is never rate-limited, unlike a
    Googlebot-UA replay). Body: {"old_slugs": ["provider-name-oldhash", ...]}.
    Returns per-slug outcome counts."""
    ok, err = _admin_guard()
    if not ok:
        return err
    body = request.get_json(silent=True) or {}
    old_slugs = body.get('old_slugs') or []
    if not isinstance(old_slugs, list) or not old_slugs:
        return jsonify(error='no_old_slugs',
                       hint='POST {"old_slugs":["provider-name-hash", ...]}'), 400
    try:
        from routes.facility_profile_page import _resolve_legacy_slug, _fetch_facility_by_slug
    except Exception as e:
        return jsonify(error=f'resolver_unavailable: {e}'), 500
    conn = None
    try:
        conn = _get_conn()
        ensure_freeze_schema(conn)
        pairs = []
        stats = {'resolved': 0, 'already_canonical': 0, 'unresolvable': 0}
        for raw in old_slugs[:20000]:
            s = (raw or '').strip().lstrip('/')
            if s.startswith('http'):
                s = s.split('/facilities/', 1)[-1].split('?')[0].rstrip('/')
            if s.endswith('.html'):
                s = s[:-5]
            if not s:
                continue
            # Already a live canonical? then it needs no alias.
            if _fetch_facility_by_slug(s):
                stats['already_canonical'] += 1
                continue
            target = _resolve_legacy_slug(s)
            if target and target != s:
                pairs.append((s, target))
                stats['resolved'] += 1
            else:
                stats['unresolvable'] += 1
        loaded = load_aliases(conn, pairs, source='gsc') if pairs else 0
        return jsonify(ok=True, submitted=len(old_slugs),
                       aliases_loaded=loaded, **stats)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


@slug_freeze_bp.route('/api/v1/admin/slug/alias-load', methods=['POST'])
def slug_alias_load():
    """Load explicit old→canonical aliases (e.g. captured from the GSC export).
    Body: {"aliases": [["old-slug","canonical-slug"], ...], "source": "gsc"}"""
    ok, err = _admin_guard()
    if not ok:
        return err
    body = request.get_json(silent=True) or {}
    aliases = body.get('aliases') or []
    source = (body.get('source') or 'manual')[:40]
    if not isinstance(aliases, list) or not aliases:
        return jsonify(error='no_aliases', hint='POST {"aliases":[["old","canonical"],...]}'), 400
    conn = None
    try:
        conn = _get_conn()
        ensure_freeze_schema(conn)
        loaded = load_aliases(conn, aliases, source=source)
        return jsonify(ok=True, loaded=loaded, source=source)
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
