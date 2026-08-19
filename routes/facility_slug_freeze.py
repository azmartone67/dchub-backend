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
import unicodedata
import logging

from flask import Blueprint, request, jsonify
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)
slug_freeze_bp = Blueprint("slug_freeze", __name__)

# Tables that carry facility rows served under /facilities/<slug>.
_FACILITY_TABLES = ("discovered_facilities", "facilities")


# ─────────────────────────────────────────────────────────────────────────
# Canonical slug — BYTE-IDENTICAL to main.py _build_sitemap_sections + the
# live facility route. Do NOT "improve" the regex here without changing both;
# any drift re-mints duplicate URLs (the exact bug this file exists to kill).
# ─────────────────────────────────────────────────────────────────────────
# ── ASCII folding for non-Latin names (2026-07-28) ──────────────────────
# The old _slugify kept only [a-z0-9], so a name written in Chinese, Japanese
# or Cyrillic reduced to the EMPTY string and the facility got NO URL at all —
# 221 live facilities were unreachable and unindexable (measured), and the same
# stripping mangled accented Latin: "Bouygues Télécom" -> "bouygues-t-l-com"
# (that exact slug is in the GSC Not-found export).
#
# ★ Unidecode is imported OPTIONALLY and the chain degrades on its own:
#     1. unidecode      -> 联通云数据中心 = "lian-tong-yun-shu-ju-zhong-xin"
#     2. stdlib NFKD    -> accented Latin still folds (télécom -> telecom)
#     3. raw            -> unchanged behaviour
#   so if the dependency ever fails to install, slugging keeps working instead
#   of the whole ingest breaking on an ImportError.
try:                                     # pragma: no cover - import shape
    from unidecode import unidecode as _unidecode
except Exception:                        # pragma: no cover
    _unidecode = None


def _fold_to_ascii(text):
    """Best-effort ASCII rendering of any script. Never raises."""
    s = str(text)
    if _unidecode is not None:
        try:
            folded = _unidecode(s)
            if folded and folded.strip():
                return folded
        except Exception:
            pass
    try:
        s = unicodedata.normalize('NFKD', s)
        s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    except Exception:
        pass
    return s


def _slugify(text):
    if not text:
        return None
    s = _fold_to_ascii(text).lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    return s.strip('-')


def _stable_hash8(provider, name):
    # Mirror routes.facility_slug.stable_hash8 (import kept local so this module
    # never fails to load if that import path moves).
    return hashlib.md5(f"{provider or ''}|{name or ''}".encode("utf-8")).hexdigest()[:8]


def _dedupe_provider_prefix(provider_slug, name_slug):
    """Drop the provider prefix when the NAME already starts with it.

    ★ The bug this fixes (GSC 2026-07-28): the slug was an unconditional
      f"{provider}-{name}-{hash}", but operators name facilities with their own
      brand in front. That produced `ntt-ntt-frankfurt-...`,
      `pentech-pentech-...`, `equinix-equinix-sp3-so-paulo-...`. Measured on
      5,064 frozen rows: 45.7% carry the doubling.
    ★ TOKEN-BOUNDARY match only. A bare startswith() would mangle a provider
      that is a prefix of an unrelated word (provider "int" vs name "internap"),
      so the name must equal the provider or continue with "-".
    """
    if not provider_slug or not name_slug:
        return name_slug
    if name_slug == provider_slug or name_slug.startswith(provider_slug + '-'):
        return name_slug
    return f"{provider_slug}-{name_slug}"


def build_canonical_slug(provider, name):
    """Current canonical /facilities/<slug> segment, or None (name too short).
    Matches the sitemap's `{provider-slug}-{name-slug}-{stable_hash8}` exactly.

    ★★ FORWARD-ONLY, BY DESIGN. This changes the slug only for rows that have
    not been frozen yet. The ~6,800 already-frozen doubled slugs are LEFT ALONE
    on purpose: canonical_slug is set-once precisely so live URLs never move,
    and re-slugging them to prettier URLs would mint ~6,800 fresh redirects —
    the exact churn that put 9,819 pages in GSC's "Page with redirect" bucket.
    An ugly URL that is stable beats a pretty one that moves.
    ★ The HASH is unchanged (it keys on provider|name, not on the slug text), so
    a row's identity is untouched — only the human-readable part differs.
    """
    name_slug = _slugify(name) or ''
    if not name_slug or len(name_slug) < 3:
        return None
    provider_slug = _slugify(provider) or ''
    h = _stable_hash8(provider, name)
    if not provider_slug:
        return f"{name_slug}-{h}"
    return f"{_dedupe_provider_prefix(provider_slug, name_slug)}-{h}"


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


def backfill_canonical_slugs(conn, table, batch=5000, max_batches=50):
    """Set canonical_slug for rows where it IS NULL (set-once — the WHERE guard
    means re-ingestion / re-runs can never overwrite a frozen value). Slugs are
    computed in Python (byte-identical to the sitemap) but WRITTEN in bulk via
    execute_values — one round-trip per batch, not one per row — so 37k rows
    freeze in seconds and never hit the edge-worker timeout. Rows whose name is
    too short to slug get '' (a sentinel → they 404, correctly not indexable).
    Per-batch commit means a timeout still leaves committed progress.
    Returns (updated, remaining)."""
    cur = conn.cursor()
    updated = 0
    for _ in range(max_batches):
        cur.execute(f"""
            SELECT id, provider, name FROM {table}
            WHERE (canonical_slug IS NULL OR canonical_slug = '')
              AND name IS NOT NULL AND name <> ''
            LIMIT %s
        """, (batch,))
        rows = cur.fetchall()
        if not rows:
            break
        values = [(fid, build_canonical_slug(provider, name) or '')
                  for fid, provider, name in rows]
        # ★★ ALIAS THE PRE-DEDUPE FORM (2026-07-28). Until this row is frozen it
        # is SERVED from a live compute of build_canonical_slug(), so the
        # provider-prefix dedupe shipped today silently MOVES its URL:
        #   ntt-ntt-frankfurt-<h>  ->  ntt-frankfurt-<h>
        # Freezing the new form without an alias would turn every already-indexed
        # old URL into a 404 — re-creating the exact bucket this whole change set
        # is trying to drain. Mint old->new first; the hash is identical on both
        # sides, so this is a rename, not a re-identification.
        _legacy = []
        for fid, provider, name in rows:
            _ns = _slugify(name) or ''
            _ps = _slugify(provider) or ''
            if not _ns or len(_ns) < 3 or not _ps:
                continue
            _doubled = f"{_ps}-{_ns}-{_stable_hash8(provider, name)}"
            _clean = build_canonical_slug(provider, name)
            if _clean and _doubled != _clean:
                _legacy.append((_doubled, _clean, str(fid), 'provider-dedupe'))
        if _legacy:
            try:
                execute_values(cur, """
                    INSERT INTO facility_slug_aliases
                      (old_slug, canonical_slug, facility_id, source)
                    VALUES %s ON CONFLICT (old_slug) DO NOTHING
                """, _legacy, template="(%s, %s, %s, %s)")
            except Exception:
                conn.rollback()   # an alias failure must never block the freeze
        # id cast to text on both sides so the same statement works for the
        # SERIAL (int) discovered_facilities id and the TEXT facilities id.
        execute_values(cur, f"""
            UPDATE {table} AS t SET canonical_slug = v.slug
            FROM (VALUES %s) AS v(id, slug)
            -- ★ set-once is preserved for REAL slugs: a non-empty
            -- canonical_slug is never overwritten. The '' sentinel is
            -- re-openable because no URL was ever served for it, and
            -- v.slug <> '' stops an empty result re-writing an empty value.
            WHERE t.id::text = v.id::text
              AND (t.canonical_slug IS NULL OR t.canonical_slug = '')
              AND v.slug <> ''
        """, values, template="(%s, %s)")
        conn.commit()
        updated += sum(1 for _, s in values if s)
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
                pairs.append((old, canonical, str(fid), 'id-scheme'))
        if pairs:
            execute_values(cur, """
                INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
                VALUES %s
                ON CONFLICT (old_slug) DO NOTHING
            """, pairs, template="(%s, %s, %s, %s)")
            inserted += len(pairs)   # attempted (ON CONFLICT skips dupes silently)
        conn.commit()
        offset += len(rows)
        if len(rows) < batch:
            break
    return inserted


def stored_slug_alias_gap(conn, table):
    """(gap, total_stale) for the STORED `slug` column on `table`.

    ★ THE HOLE backfill_id_scheme_aliases DOES NOT COVER (measured 2026-08-19).

    That function aliases the slug it RECOMPUTES from (provider, name, id) —
    the pre-swap MD5(id) form. It never looks at the `slug` value actually
    sitting on the row, and after the 2026-06-16 scheme swap that column is
    stale almost everywhere: 26,112 of 26,239 rows carry a `slug` that differs
    from their own `canonical_slug`, and 9,822 of those are the pre-swap
    non-hash8 form.

    Measured live the day this was written: of 9,822 legacy stored slugs,
    **0 had an alias row** — the alias table's 54,178 rows are all id-scheme
    (50,120) and provider-dedupe (4,058), a disjoint population. A 30-URL probe
    of those legacy slugs returned **17 × 404**, and GSC was reporting 3,576
    "Not found (404)" against the property.

    The recovery path in render_facility_profile was never broken — it consults
    resolve_alias() before it 404s. It had nothing to find.

    gap = rows whose stored slug would 404 AND have no alias to rescue them.
    Returns (gap, total_stale) so a caller can tell "nothing to do" from
    "nothing measured".
    """
    cur = conn.cursor()
    cur.execute(f"""
        SELECT COUNT(*) FILTER (WHERE a.old_slug IS NULL),
               COUNT(*)
          FROM {table} f
          LEFT JOIN facility_slug_aliases a ON a.old_slug = f.slug
         WHERE f.slug IS NOT NULL AND f.slug <> ''
           AND f.canonical_slug IS NOT NULL AND f.canonical_slug <> ''
           AND f.slug IS DISTINCT FROM f.canonical_slug
    """)
    gap, total = cur.fetchone()
    return int(gap or 0), int(total or 0)


def backfill_stored_slug_aliases(conn, table, batch=2000, max_batches=50):
    """Alias every row's STORED slug → its canonical_slug. Set-once, idempotent.

    ★ SAFETY, verified before this shipped rather than argued:
      · A 301 is only ever emitted when the requested slug resolves to NOTHING
        (render_facility_profile calls resolve_alias only under `if not fac`),
        so aliasing a slug that still serves 200 cannot hijack a live URL.
      · ON CONFLICT DO NOTHING — an existing alias, whatever its source, wins.
        This adds rescue paths; it never repoints one.
      · The targets are real: a 40-pair live probe found **40/40 canonical
        targets returning 200** while 33/40 of the old slugs returned 404. A
        backfill that pointed 301s at 404s would be worse than the 404s, so
        this was measured first.

    Deliberately NOT restricted to the non-hash8 "legacy" shape: hash8 slugs
    churn too (re-ingestion moves provider or name), and the predicate that
    matters is "this stored slug is not the canonical one", not its format.
    """
    cur = conn.cursor()
    inserted = 0
    offset = 0
    for _ in range(max_batches):
        cur.execute(f"""
            SELECT id, slug, canonical_slug FROM {table}
             WHERE slug IS NOT NULL AND slug <> ''
               AND canonical_slug IS NOT NULL AND canonical_slug <> ''
               AND slug IS DISTINCT FROM canonical_slug
             ORDER BY id LIMIT %s OFFSET %s
        """, (batch, offset))
        rows = cur.fetchall()
        if not rows:
            break
        pairs = [(s, c, str(fid), 'stored-slug') for fid, s, c in rows if s and c]
        if pairs:
            execute_values(cur, """
                INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
                VALUES %s
                ON CONFLICT (old_slug) DO NOTHING
            """, pairs, template="(%s, %s, %s, %s)")
            inserted += len(pairs)   # attempted; ON CONFLICT skips silently
        conn.commit()
        offset += len(rows)
        if len(rows) < batch:
            break
    return inserted


def load_aliases(conn, rows, source="manual"):
    """Bulk-load [(old_slug, canonical_slug[, facility_id]), ...]. Explicit
    loads (e.g. GSC-export capture) win over programmatic ones — DO UPDATE."""
    cur = conn.cursor()
    vals = []
    for r in rows:
        old = (r[0] or "").strip().lstrip("/")
        canon = (r[1] or "").strip().lstrip("/")
        fid = str(r[2]) if len(r) > 2 and r[2] is not None else None
        if not old or not canon or old == canon:
            continue
        vals.append((old, canon, fid, source))
    if not vals:
        return 0
    execute_values(cur, """
        INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
        VALUES %s
        ON CONFLICT (old_slug) DO UPDATE
          SET canonical_slug = EXCLUDED.canonical_slug, source = EXCLUDED.source
    """, vals, template="(%s, %s, %s, %s)")
    conn.commit()
    return len(vals)


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
            # ★ THE NUMBER THAT WAS NEVER PUBLISHED. "frozen" counts rows with
            # a canonical slug — it reads 26,112/26,239 and looks finished,
            # while 9,822 stored slugs were 404ing with no alias to rescue
            # them. A completion metric that cannot express the gap is how
            # this stayed invisible; the gap now rides beside it.
            gap = stale = None
            if has_col:
                try:
                    gap, stale = stored_slug_alias_gap(conn, t)
                except Exception:
                    gap = stale = None   # UNMEASURED, never a reassuring 0
            out['tables'][t] = {'has_canonical_slug_col': has_col,
                                'frozen': frozen, 'pending': pending,
                                'stored_slug_stale': stale,
                                'stored_slug_no_alias_gap': gap}
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
            # The stored `slug` column is a SECOND stale URL per row and the
            # id-scheme pass never touches it — see stored_slug_alias_gap.
            ins2 = backfill_stored_slug_aliases(conn, t, max_batches=max_batches)
            gap, stale = stored_slug_alias_gap(conn, t)
            result['aliases'][t] = {'id_scheme_aliases_added': ins,
                                    'stored_slug_aliases_added': ins2,
                                    'stored_slug_gap_remaining': gap,
                                    'stored_slug_stale_total': stale}
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
    # Pagination — the fuzzy resolver is one DB round-trip PER slug, so a full
    # 3,485-slug payload would exceed the edge-worker timeout. Process a bounded
    # slice per call and report next_offset; the caller loops until remaining=0.
    try:
        limit = int(request.args.get('limit', body.get('limit', 1000)))
        offset = int(request.args.get('offset', body.get('offset', 0)))
    except Exception:
        limit, offset = 1000, 0
    limit = max(1, min(limit, 3000))
    offset = max(0, offset)
    window = old_slugs[offset:offset + limit]
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
        for raw in window:
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
        processed_to = offset + len(window)
        remaining = max(0, len(old_slugs) - processed_to)
        return jsonify(ok=True, total=len(old_slugs),
                       window={'offset': offset, 'processed': len(window)},
                       next_offset=(processed_to if remaining else None),
                       remaining=remaining, aliases_loaded=loaded, **stats)
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
