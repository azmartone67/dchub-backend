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
is idempotent; the canonical_slug backfills are set-once (WHERE canonical_slug
IS NULL). The ALIAS emitters are idempotent rather than set-once: each may
correct an alias IT wrote for THAT facility (_ALIAS_REPOINT) and may touch no
other, so re-running converges instead of freezing a stale 301 target.
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

# ★ r-slugblockers (2026-09-19): MEASURING a table is safe; RE-MINTING one is
# not. `facilities` has no is_duplicate, so its exact-canonical_slug arm raises
# and its rows are reachable ONLY through hash8(provider|name) — pinned by
# tests/test_served_slugs_sql_parity.py. build_disambiguated_slug tails on
# md5(provider|name|id), so a re-minted slug there matches nothing: a hard 404
# fed into the sitemap by main.py, with no alias and so no recovery hop.
_REMINTABLE_TABLES = ("discovered_facilities",)

# ★ r-slugcollide (2026-09-19) — WHICH row a shared frozen slug serves.
# One frozen canonical_slug is routinely worn by MANY rows (the builder is a
# pure function of provider+name and the freeze index is non-unique), and
# _fetch_facility_by_slug picks the single row the page renders with exactly
# this ORDER BY. The disambiguator below must keep the slug on THAT row and
# re-mint the others, so the ordering has to be the SAME STRING in both
# places — a second copy that drifts would hand the URL to a different
# facility, which is the churn the whole freeze exists to prevent.
# routes/facility_profile_page.py imports this; do not inline it there again.
# ★ r-slugblockers (2026-09-19): a TEMPLATE, because every CTE here names
# probe-substituted columns and so could not use the finished string. That is
# why FIVE hand-written copies had accumulated and why the guard claiming "the
# SAME STRING in both places" was a tautology that survived mutating the
# ranking to `ORDER BY id DESC`. Format it; never retype it.
SLUG_OWNER_ORDER_TMPL = (
    "COALESCE({isdup}, 0) ASC, COALESCE({power}, 0) DESC, id ASC")
SLUG_OWNER_ORDER_SQL = SLUG_OWNER_ORDER_TMPL.format(
    isdup="is_duplicate", power="power_mw")


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


def stored_slugs_by_id(cur, conn, ids):
    """{id: canonical_slug} for whichever of `ids` the freeze has reached.

    ★ STORED-FIRST, for every emitter. build_canonical_slug() has NOT equalled
    the frozen slug since 2026-07-28: the freeze (07-03) stored the DOUBLED
    body and _dedupe_provider_prefix (07-28) changed what the builder returns,
    so a row frozen before 07-28 rebuilds to a DIFFERENT body. An emitter that
    composes instead of reading emits a URL that 301s — measured 109 of 150
    sampled slugs (73%) on /api/v1/map, hash8 tail identical, body moved
    (be#4793). Re-freezing to match the builder is NOT the fix: it would move
    every already-indexed facility URL.

    Returns {} when the column does not exist yet — live DDL can lag the code,
    and callers must fall back to the builder rather than 500.
    """
    ids = [i for i in (ids or []) if i is not None]
    if not ids:
        return {}
    try:
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='discovered_facilities' "
                    "AND column_name='canonical_slug'")
        if cur.fetchone() is None:
            return {}
        cur.execute("SELECT id, canonical_slug FROM discovered_facilities "
                    "WHERE id = ANY(%s) AND canonical_slug IS NOT NULL "
                    "AND canonical_slug <> ''", (ids,))
        return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return {}


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
    # ★★★ 2026-09-05 — THE LENGTH TEST WAS ON THE WRONG STRING.
    # This used to read `if not name_slug or len(name_slug) < 3`, rejecting the
    # NAME FRAGMENT for being short. But the fragment is never the slug: the
    # returned value always carries an 8-char stable hash and usually a
    # provider prefix, so "SC" at Equinix becomes `equinix-sc-<hash8>` — 20
    # characters, unique, and a perfectly good URL. The guard measured a part
    # and rejected the whole.
    #
    # It cost 28 real facilities their only URL, for five months. Operators do
    # name buildings with one or two characters — measured in the stuck set:
    #   SC (US) · L7 (UA) · RZ (DE, Rechenzentrum) · Oi (BR, the telco)
    #   A / B / C (AT) · 1A / 1B / 2 / 3 / 4 (CN, HK) · B4 (FR)
    # first_seen 2026-03-18 to 2026-04-10, every one still Operational.
    #
    # An EMPTY name_slug is still rejected, and that guard is the real one: a
    # name that folds to nothing gives the URL no human-readable identity at
    # all, and `-<hash8>` alone is not a page anyone can read or cite.
    if not name_slug:
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

# ★★ ONE definition, three emitters (provider-dedupe, id-scheme, stored-slug).
# A re-mint moves a facility from slug S to S'. An alias old_X -> S written by
# an earlier freeze run then 301s a legacy, indexed URL to a page that now
# belongs to a DIFFERENT facility, and ON CONFLICT DO NOTHING made that
# permanent: the run recomputed the right target every time and threw it away.
# Repointing is scoped so it can only ever correct an alias THIS emitter wrote
# for THIS facility:
#   source = EXCLUDED.source   an emitter never rewrites another emitter's
#                              alias, so explicit gsc / manual loads still win
#                              (load_aliases' DO UPDATE keeps that precedence)
#   facility_id matched        never repoint an alias owned by another facility
#   canonical_slug IS DISTINCT a no-op stays a no-op, so RETURNING counts only
#                              the rows that actually moved
_ALIAS_REPOINT = """
                ON CONFLICT (old_slug) DO UPDATE
                   SET canonical_slug = EXCLUDED.canonical_slug
                 WHERE facility_slug_aliases.source = EXCLUDED.source
                   AND facility_slug_aliases.facility_id
                       IS NOT DISTINCT FROM EXCLUDED.facility_id
                   AND facility_slug_aliases.canonical_slug
                       IS DISTINCT FROM EXCLUDED.canonical_slug
                RETURNING 1
"""


def build_disambiguated_slug(provider, name, fac_id, city=None,
                            state=None, country=None):
    """A slug UNIQUE TO ONE ROW, for a row that lost its shared frozen slug.

    ★ THE BUG (measured live 2026-09-19, /api/v1/map, 5,000 rows):
      4,287 unique slugs for 5,000 rows; 466 slugs worn by more than one row;
      34 collision groups more than 2km across, spanning 260 rows (5.2 percent).
      Worst: amazon-web-services-amazon-web-services-7e958426 on 61 rows named
      exactly "Amazon Web Services", 17,218 km apart (IE, ID, US, CL, AE, AU).
      Every marker in a group linked to the ONE row the resolver picks.

    ★ THIS IS NOT THE MAP'S FALLBACK COMPOSER. discovered_facilities was
      30,621 frozen / 11 pending when this was written, so the colliding rows
      all read their slug from STORED canonical_slug. The collision was WRITTEN
      by backfill_canonical_slugs: build_canonical_slug is a pure function of
      (provider, name) and the freeze index is non-unique, so every row sharing
      provider+name got byte-identical output. Re-minting here is what fixes it.

    Keeps the trailing -‹8 hex› shape, because _fetch_facility_by_slug rejects
    any slug whose last dash-separated part is not exactly 8 characters before
    it ever reaches the database.

    The hash keys on provider|name|id, so it differs per ROW where the frozen
    one could not; the body carries the most specific location the row has, so
    the URL still reads like a place rather than a serial number.
    """
    base = build_canonical_slug(provider, name)
    if not base or fac_id is None or str(fac_id) == '':
        return None
    body = base.rsplit('-', 1)[0]          # drop the provider|name hash8 tail
    if not body:
        return None
    loc = _slugify(city) or _slugify(state) or _slugify(country) or ''
    # TOKEN-BOUNDARY, the _dedupe_provider_prefix rule: "san-jose" must not be
    # appended to "equinix-san-jose", but "jose" is a different token and may.
    if loc and body != loc and not body.endswith('-' + loc) \
            and not body.startswith(loc + '-'):
        body = body + '-' + loc
    h = hashlib.md5(
        f"{provider or ''}|{name or ''}|{fac_id}".encode("utf-8")).hexdigest()[:8]
    return f"{body}-{h}"


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
                    VALUES %s
                """ + _ALIAS_REPOINT, _legacy, template="(%s, %s, %s, %s)",
                    fetch=True)
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
    # ★★★ 2026-09-05 — THIS COUNTER READ A STRICT SUBSET OF WHAT THE WORKER
    # SELECTS. The loop above picks up `canonical_slug IS NULL OR = ''`; this
    # counted only `IS NULL`. So the 28 rows that reached the '' sentinel were
    # re-selected every run, produced no slug, wrote nothing — and were then
    # reported as pending=0. The workflow printed "pending=0" on every tick for
    # five months while those rows had no URL. A checker must read the same
    # population it publishes a verdict on.
    cur.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE (canonical_slug IS NULL OR canonical_slug = '') "
        f"  AND name IS NOT NULL AND name <> ''")
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
            # ★ RETURNING + fetch, not len(pairs): execute_values PAGES the
            # argslist, and a conflicting row that the scoped repoint declines
            # writes nothing. Counting what was ATTEMPTED reported success for
            # rows the statement skipped — the shape of the #4830 counter bug.
            got = execute_values(cur, """
                INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
                VALUES %s
            """ + _ALIAS_REPOINT, pairs, template="(%s, %s, %s, %s)", fetch=True)
            inserted += len(got or [])
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
      · ON CONFLICT repoints ONLY an alias this same emitter wrote for this
        same facility (_ALIAS_REPOINT). An alias from any other source — gsc,
        manual, another emitter — still wins, so this adds rescue paths and
        corrects its own stale ones; it never repoints somebody else's.
        It USED to be DO NOTHING, which also refused to correct its own: a
        re-mint moved the facility and left the alias 301ing a legacy URL to
        a page that had since become a different facility.
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
            got = execute_values(cur, """
                INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
                VALUES %s
            """ + _ALIAS_REPOINT, pairs, template="(%s, %s, %s, %s)", fetch=True)
            inserted += len(got or [])   # rows that actually landed or moved
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
            # ★ The collision shape rides beside the completion metric for the
            # same reason the alias gap does: "frozen: 30,621 / pending: 11"
            # reads finished while 466 of those frozen slugs were worn by more
            # than one facility. A completion number that cannot express the
            # collision is how this stayed invisible.
            out['tables'][t] = {'has_canonical_slug_col': has_col,
                                'frozen': frozen, 'pending': pending,
                                'stored_slug_stale': stale,
                                'stored_slug_no_alias_gap': gap,
                                'collisions': (slug_collision_stats(conn, t)
                                               if has_col else None),
                                'collision_breakdown': (
                                    slug_collision_breakdown(conn, t)
                                    if has_col else None),
                                'keeper_reachability': (
                                    keeper_slug_reachability(conn, t)
                                    if has_col else None)}
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


# ─────────────────────────────────────────────────────────────────────────
# r-slugcollide (2026-09-19) — one frozen slug, many facilities
# ─────────────────────────────────────────────────────────────────────────
def _dup_cols(cur, table):
    """(is_duplicate, duplicate_of_id, power_mw) as SQL expressions.

    `facilities` carries none of the duplicate columns — _fetch_facility_by_slug
    selects them there as literal NULLs — so naming one unprobed would raise and
    take out the whole admin route. Same probe-per-table pattern as
    ensure_freeze_schema and stored_slugs_by_id.
    """
    # ★ r-slugblockers (2026-09-19): TYPED. A bare NULL in a CTE output column
    # is typed `text` by Postgres, so the outer COALESCE(_isdup, 0) in
    # _INDEPENDENT raised "COALESCE types text and integer cannot be matched"
    # (reproduced on PG 18.6) and killed BOTH stats and the re-mint for
    # `facilities` — at the safe dry-run default, and swallowed into a
    # permanent `collisions: null` on the status route.
    typed = {"is_duplicate": "NULL::int", "duplicate_of_id": "NULL::text",
             "power_mw": "NULL::numeric"}
    return tuple(col if _column_exists(cur, table, col) else typed[col]
                 for col in ("is_duplicate", "duplicate_of_id", "power_mw"))


def _ranked_cte(cur, table):
    """Rows sharing a canonical_slug, ranked by WHO THE PAGE SERVES (rn=1).

    ★ The ranking runs over EVERY row on the slug, unfiltered. Filtering
      suppressed rows out of the window instead would promote a different row
      to rn=1 while the resolver still served the suppressed one — two rows
      would then keep the slug and the collision would survive the fix.
      (47 slugs are served ONLY by suppressed rows; see the ORDER note in
      _fetch_facility_by_slug.)
    """
    isdup, dupof, power = _dup_cols(cur, table)
    order = SLUG_OWNER_ORDER_TMPL.format(isdup=isdup, power=power)
    return f"""
        WITH ranked AS (
            SELECT id, provider, name, city, state, country, canonical_slug,
                   {isdup} AS _isdup, {dupof} AS _dupof,
                   ROW_NUMBER() OVER (
                       PARTITION BY canonical_slug
                       ORDER BY {order}) AS rn,
                   -- ★ the OWNER, from the SAME window that ranks rn. A
                   -- separately-ordered owner could name a different row than
                   -- the one rn=1 protects, and the rewrite would strip the
                   -- slug off the row the page actually serves.
                   FIRST_VALUE(id) OVER (
                       PARTITION BY canonical_slug
                       ORDER BY {order}) AS owner_id
              FROM {table}
             WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''
        )
    """


# A non-owner row is re-minted only when NOTHING already marks it as a twin of
# the row that keeps the slug. is_duplicate / duplicate_of_id are the row's OWN
# stored verdict — read, never recomputed. Re-deriving the four-condition
# same-physical-site predicate here would put a second composer beside
# _twin_redirect_target, which is the bug be#4793 fixed; a genuine duplicate
# must KEEP sharing its keeper's URL, which is what be#4808 collapses for.
_INDEPENDENT = "rn > 1 AND COALESCE(_isdup, 0) = 0 AND _dupof IS NULL"

# ★★ ONE definition of the buckets, read by the breakdown, the per-slug
# report AND the rewrite. Two copies would let the endpoint publish a
# population the rewrite does not act on — the shape of the bug that let
# be#4818 report success while 7,653 rows kept colliding.
#   points_at_owner    twin of THE ROW THAT KEEPS THE SLUG -> keep sharing
#   points_elsewhere   twin of a DIFFERENT keeper -> adopt THAT keeper's slug
#   marked_no_pointer  flagged, but nothing says twin-of-what -> own slug
#   unmarked           independent facility -> own slug
_BUCKET_CASE = """
        CASE
          WHEN _dupof IS NOT NULL AND _dupof::text = owner_id::text
               THEN 'points_at_owner'
          WHEN _dupof IS NOT NULL THEN 'points_elsewhere'
          WHEN COALESCE(_isdup, 0) <> 0 THEN 'marked_no_pointer'
          ELSE 'unmarked'
        END"""

# Buckets whose rows must STOP wearing the owner's slug. points_at_owner is
# deliberately absent: those rows are twins of the row the page serves, so
# sharing its URL is correct. Whether that pointer can be trusted at distance
# is what slug_collision_groups() exists to answer.
_REWRITE_BUCKETS = ('points_elsewhere', 'marked_no_pointer', 'unmarked')


def slug_collision_stats(conn, table):
    """{slugs, rows, independent_rows} for slugs worn by more than one row.

    Returns None (never a reassuring 0) if the shape cannot be measured.
    """
    try:
        cur = conn.cursor()
        cur.execute(_ranked_cte(cur, table) + f"""
            SELECT COUNT(DISTINCT canonical_slug) FILTER (WHERE rn > 1),
                   COUNT(*) FILTER (WHERE rn > 1),
                   COUNT(*) FILTER (WHERE {_INDEPENDENT})
              FROM ranked
        """)
        slugs, rows, indep = cur.fetchone()
        return {'shared_slugs': int(slugs or 0),
                'non_owner_rows': int(rows or 0),
                'independent_non_owner_rows': int(indep or 0)}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"slug collision stats for {table}: {e}")
        return None


def slug_collision_breakdown(conn, table):
    """WHY each non-owner row sits on somebody else's slug.

    ★ THE RULE THAT WAS WRONG (measured live 2026-09-19). The first pass
    skipped every non-owner row carrying is_duplicate / duplicate_of_id, on
    the reading that such a row is a twin already collapsed onto the row that
    keeps the slug. It is not: is_duplicate is a name/provider similarity
    verdict, not a same-site one — this module's own header records that
    "17,028 of 21,861 rows are is_duplicate precisely BECAUSE provider strings
    vary". 7,653 of 8,375 non-owner rows were skipped on that basis, including
    amazon-web-services-amazon-web-services-7e958426 (62 rows, 17,218 km,
    cities '', '', Ashburn, Dublin, Sterling). The defect survived the fix.

    The question the skip SHOULD have asked is not "is this row a twin of
    something" but "is it a twin of THE ROW THAT KEEPS THIS SLUG". That is
    pointer equality against the window's own owner — no distance, no
    coordinates, no second copy of _twin_redirect_target.

    Buckets, over rows with rn > 1 on a shared canonical_slug:
      points_at_owner   duplicate_of_id IS the owner -> correctly sharing
      points_elsewhere  duplicate_of_id is some OTHER row -> its marker
                        belongs on THAT keeper's page, not the owner's
      marked_no_pointer is_duplicate set, duplicate_of_id NULL -> nothing
                        says which row it is a twin OF
      unmarked          neither flag -> an independent facility (the bucket
                        the first pass rewrote)
    Returns None if it cannot be measured — never a reassuring zero.
    """
    try:
        cur = conn.cursor()
        isdup, dupof, power = _dup_cols(cur, table)
        order = SLUG_OWNER_ORDER_TMPL.format(isdup=isdup, power=power)
        cur.execute(_ranked_cte(cur, table) + f"""
            SELECT
              COUNT(*) FILTER (WHERE rn > 1),
              COUNT(*) FILTER (WHERE rn > 1
                                 AND {_BUCKET_CASE} = 'points_at_owner'),
              COUNT(*) FILTER (WHERE rn > 1
                                 AND {_BUCKET_CASE} = 'points_elsewhere'),
              COUNT(*) FILTER (WHERE rn > 1
                                 AND {_BUCKET_CASE} = 'marked_no_pointer'),
              COUNT(*) FILTER (WHERE rn > 1
                                 AND {_BUCKET_CASE} = 'unmarked'),
              COUNT(DISTINCT canonical_slug) FILTER (WHERE rn > 1)
              FROM ranked
        """)
        tot, at_owner, elsewhere, no_ptr, unmarked, slugs = cur.fetchone()
        return {'non_owner_rows': int(tot or 0),
                'points_at_owner': int(at_owner or 0),
                'points_elsewhere': int(elsewhere or 0),
                'marked_no_pointer': int(no_ptr or 0),
                'unmarked': int(unmarked or 0),
                'shared_slugs': int(slugs or 0)}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"slug collision breakdown for {table}: {e}")
        return None


def keeper_slug_reachability(conn, table):
    """For points_elsewhere rows: can their OWN keeper actually be linked?

    A row whose marker belongs on keeper K's page is only fixable by pointing
    it at K if K HAS a canonical_slug. Counting this before choosing a rule
    stops the next pass minting fresh pages for rows that already have a
    correct destination, and names the residue that has none.
    """
    try:
        cur = conn.cursor()
        isdup, dupof, power = _dup_cols(cur, table)
        order = SLUG_OWNER_ORDER_TMPL.format(isdup=isdup, power=power)
        if dupof == "NULL":
            return None                     # no pointer column on this table
        cur.execute(_ranked_cte(cur, table) + f"""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE k.canonical_slug IS NOT NULL
                                      AND k.canonical_slug <> '')
              FROM ranked r
              LEFT JOIN {table} k ON k.id::text = r._dupof::text
             WHERE r.rn > 1 AND r._dupof IS NOT NULL
               AND r._dupof::text <> r.owner_id::text
        """)
        tot, with_slug = cur.fetchone()
        return {'points_elsewhere': int(tot or 0),
                'keeper_has_slug': int(with_slug or 0),
                'keeper_has_no_slug': int((tot or 0) - (with_slug or 0))}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"keeper reachability for {table}: {e}")
        return None


def _dry_run_flag(body):
    """Is this a dry run? FAIL SAFE — anything unrecognised means YES.

    ★ r-slugblockers (2026-09-19): only an explicit bool, or a recognised
    string, can turn the dry run OFF. `{"dry_run": null}` — what a client that
    serialises unset fields emits — previously reached a bare `bool(None)` and
    armed a full rewrite of set-once canonical_slug values. So did 0, [] and {}.
    This removes an asymmetry where the string "off" was already safe while
    JSON null was not.
    """
    raw = body.get('dry_run', True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ('false', '0', 'no', 'off')
    return True


def slug_collision_groups(conn, table, limit=25):
    """Per-slug bucket composition for the worst collision groups.

    ★ NO COORDINATES, deliberately. Whether a group is really one building is
    _twin_redirect_target's question and it has exactly one owner; measuring
    distance here would put a second judge beside it. These counts are keyed
    by slug so they can be joined against the /api/v1/map payload and the span
    computed OUTSIDE this module, which is how the 2026-09-19 collisions were
    measured in the first place.

    This exists to answer ONE question: does points_at_owner contain groups
    that are plainly not one site? If it does, duplicate_of_id is not
    trustworthy at distance and that bucket needs a different rule.
    """
    try:
        cur = conn.cursor()
        isdup, dupof, power = _dup_cols(cur, table)
        cur.execute(_ranked_cte(cur, table) + f"""
            SELECT canonical_slug,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE rn > 1
                                      AND {_BUCKET_CASE} = 'points_at_owner'),
                   COUNT(*) FILTER (WHERE rn > 1
                                      AND {_BUCKET_CASE} = 'points_elsewhere'),
                   COUNT(*) FILTER (WHERE rn > 1
                                      AND {_BUCKET_CASE} = 'marked_no_pointer'),
                   COUNT(*) FILTER (WHERE rn > 1
                                      AND {_BUCKET_CASE} = 'unmarked')
              FROM ranked
             GROUP BY canonical_slug
            HAVING COUNT(*) > 1
             ORDER BY COUNT(*) DESC, canonical_slug
             LIMIT {int(limit)}
        """)
        return [{'slug': r[0], 'rows': int(r[1]),
                 'points_at_owner': int(r[2]), 'points_elsewhere': int(r[3]),
                 'marked_no_pointer': int(r[4]), 'unmarked': int(r[5])}
                for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"slug collision groups for {table}: {e}")
        return None


def disambiguate_slug_collisions(conn, table, batch=2000, max_batches=50,
                                 dry_run=True):
    """Give every INDEPENDENT row on a shared frozen slug a slug of its own.

    ★★ THE FREEZE CARVE-OUT, and it is narrow. The row that rn=1 picks — the
    one /facilities/‹slug› actually renders — is NEVER touched, so no URL that
    ever served a given facility moves. The rows this rewrites are rows whose
    stored canonical_slug points at SOMEBODY ELSE'S page: there is no URL to
    preserve for them, because they never had one. That is why this is not a
    violation of set-once, and why it mints NO alias: the old slug keeps
    serving its owner, so aliasing it would 301 the owner's live page away.

    Rows marked is_duplicate / duplicate_of_id are left sharing on purpose —
    collapsing a twin onto its keeper is correct behaviour, not the bug.

    Returns (rewritten, remaining). dry_run=True (the default) measures and
    writes nothing.
    """
    if not dry_run and table not in _REMINTABLE_TABLES:
        raise ValueError(
            f"{table} is measure-only: its rows resolve by hash8(provider|name), "
            f"so a re-minted canonical_slug would 404. Allowed: "
            f"{list(_REMINTABLE_TABLES)}")
    cur = conn.cursor()
    others = [t for t in _FACILITY_TABLES if t != table]
    taken = ["NOT EXISTS (SELECT 1 FROM facility_slug_aliases a "
             "WHERE a.old_slug = v.slug)",
             f"NOT EXISTS (SELECT 1 FROM {table} x WHERE x.canonical_slug = v.slug)"]
    for _o in others:
        try:
            cur.execute("SELECT to_regclass(%s)", (_o,))
            if cur.fetchone()[0] and _column_exists(cur, _o, "canonical_slug"):
                # A new slug that shadows the OTHER table's canonical would
                # steal that facility's page: discovered_facilities is probed
                # first in _fetch_facility_by_slug and wins on a tie.
                taken.append(f"NOT EXISTS (SELECT 1 FROM {_o} y "
                             f"WHERE y.canonical_slug = v.slug)")
        except Exception:
            conn.rollback()
    guard = "\n              AND ".join(taken)

    rewritten = 0
    # ★ r-slugblockers (2026-09-19): an ID CURSOR. #4830 fixed the COUNT; the
    # break below still read it as progress. A row whose new slug is already
    # taken stays selected by the same predicate forever, so `wrote == 0` ended
    # the whole run on the FIRST fully blocked batch and left every later
    # fixable row untouched. Walking id forward steps over blocked rows.
    last_id = ''
    for _ in range(max_batches):
        cur.execute(_ranked_cte(cur, table) + f"""
            SELECT r.id, r.provider, r.name, r.city, r.state, r.country,
                   -- the slug this row was MEASURED on, carried into the
                   -- write below as a compare-and-swap
                   r.canonical_slug AS cur_slug,
                   {_BUCKET_CASE} AS bucket,
                   -- the keeper this row actually points at, and the slug it
                   -- is served at. points_elsewhere rows ADOPT this rather
                   -- than minting a page for a row we chose to suppress.
                   k.canonical_slug AS keeper_slug
              FROM ranked r
              LEFT JOIN {table} k ON k.id::text = r._dupof::text
             WHERE r.rn > 1
               AND {_BUCKET_CASE} = ANY(%s)
               AND r.id::text > %s
             ORDER BY r.id::text
             LIMIT {int(batch)}
        """, (list(_REWRITE_BUCKETS), last_id))
        rows = cur.fetchall()
        if not rows:
            break
        last_id = str(rows[-1][0])
        # TWO write sets, and they need OPPOSITE guards.
        mint, adopt = [], []
        # ★ THE UNIQUENESS GUARD CANNOT SEE THIS PAGE. `guard` is a correlated
        # NOT EXISTS over the table being written, so it reads the snapshot the
        # statement started on and is blind to rows the SAME statement writes.
        # Two rows whose md5(provider|name|id) prefixes collide would both pass
        # it and both land — and the freeze index is NOT unique, so nothing
        # downstream would raise. Claiming each slug once per page closes the
        # only window the guard leaves open; a row dropped here is not lost,
        # the next run re-selects it (the id cursor just steps past it today).
        _claimed = set()
        for fid, prov, nm, city, st, ctry, cur_slug, bucket, keeper_slug in rows:
            if bucket == 'points_elsewhere':
                if keeper_slug:
                    adopt.append((fid, keeper_slug, cur_slug))
            else:
                ns = build_disambiguated_slug(prov, nm, fid, city, st, ctry)
                if ns and ns not in _claimed:
                    _claimed.add(ns)
                    mint.append((fid, ns, cur_slug))
        if dry_run or not (mint or adopt):
            break
        # ★ RETURNING + fetch, NOT cur.rowcount. execute_values PAGES the
        # argslist internally (page_size 100 by default) and issues one
        # statement per page, so cur.rowcount reports only the LAST page.
        # Measured live 2026-09-19: 722 rows were rewritten and the endpoint
        # reported rewritten=22 — 722 = 7 x 100 + 22. The real work was only
        # visible in the before/after collision counts. fetch=True collects
        # the RETURNING rows across every page.
        wrote = 0
        if mint:
            # ★ COMPARE-AND-SWAP on v.old_slug. Nothing in SQL said these
            # rows were the rn > 1 rows: the carve-out from set-once lived
            # entirely in the Python SELECT above, so any re-rank between the
            # SELECT and the UPDATE — a concurrent freeze run, or a second
            # owner election like repair_one_url_many_rows.py's — would have
            # let this statement strip the slug off the row the page SERVES.
            # Matching the slug the row was measured on makes that write a
            # no-op instead of a moved live URL.
            got = execute_values(cur, f"""
                UPDATE {table} AS t SET canonical_slug = v.slug
                FROM (VALUES %s) AS v(id, slug, old_slug)
                WHERE t.id::text = v.id::text
                  AND v.slug <> ''
                  AND t.canonical_slug = v.old_slug
                  AND t.canonical_slug IS DISTINCT FROM v.slug
                  AND {guard}
                RETURNING 1
            """, mint, template="(%s, %s, %s)", fetch=True)
            wrote += len(got or [])
        if adopt:
            # ★★ THE OPPOSITE GUARD, on purpose. The mint path refuses a slug
            # anything already wears; adopting REQUIRES that — the row is being
            # pointed at its keeper's existing page. Reusing `guard` here would
            # reject every adopt and write nothing while reporting success.
            got = execute_values(cur, f"""
                UPDATE {table} AS t SET canonical_slug = v.slug
                FROM (VALUES %s) AS v(id, slug, old_slug)
                WHERE t.id::text = v.id::text
                  AND v.slug <> ''
                  AND t.canonical_slug = v.old_slug
                  AND t.canonical_slug IS DISTINCT FROM v.slug
                  AND EXISTS (SELECT 1 FROM {table} k2
                               WHERE k2.canonical_slug = v.slug)
                RETURNING 1
            """, adopt, template="(%s, %s, %s)", fetch=True)
            wrote += len(got or [])
        conn.commit()
        rewritten += wrote
        if len(rows) < batch:
            break

    # ★ remaining counts the SAME population the loop selects. Reporting
    # independent_non_owner_rows here would read 0 while thousands of
    # points_elsewhere / marked_no_pointer rows were still waiting — the shape
    # of the bug that let be#4818 report success on a defect it never touched.
    bd = slug_collision_breakdown(conn, table)
    remaining = (sum(bd[b] for b in _REWRITE_BUCKETS) if bd else None)
    return rewritten, remaining


@slug_freeze_bp.route('/api/v1/admin/slug/groups', methods=['GET'])
def slug_collision_groups_view():
    """The worst collision groups, by slug, with their bucket composition.

    Read-only. Join against /api/v1/map to measure each group's geographic
    span outside this module — see slug_collision_groups().
    """
    ok, err = _admin_guard()
    if not ok:
        return err
    table = request.args.get('table') or 'discovered_facilities'
    if table not in _FACILITY_TABLES:
        return jsonify(error='bad_table', allowed=list(_FACILITY_TABLES)), 400
    try:
        limit = max(1, min(200, int(request.args.get('limit') or 25)))
    except ValueError:
        limit = 25
    conn = None
    try:
        conn = _get_conn()
        return jsonify(ok=True, table=table, limit=limit,
                       groups=slug_collision_groups(conn, table, limit=limit))
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


@slug_freeze_bp.route('/api/v1/admin/slug/disambiguate', methods=['POST'])
def slug_disambiguate_run():
    """Re-mint the shared frozen slugs. DRY RUN unless dry_run is false.

    POST {"table": "discovered_facilities", "dry_run": false}
    """
    ok, err = _admin_guard()
    if not ok:
        return err
    body = request.get_json(silent=True) or {}
    table = body.get('table') or 'discovered_facilities'
    if table not in _FACILITY_TABLES:
        return jsonify(error='bad_table', allowed=list(_FACILITY_TABLES)), 400
    dry_run = _dry_run_flag(body)
    conn = None
    try:
        conn = _get_conn()
        before = slug_collision_stats(conn, table)
        rewritten, remaining = disambiguate_slug_collisions(
            conn, table, dry_run=bool(dry_run),
            batch=int(body.get('batch') or 2000),
            max_batches=int(body.get('max_batches') or 50))
        return jsonify(ok=True, table=table, dry_run=bool(dry_run),
                       before=before, rewritten=rewritten,
                       remaining_independent=remaining,
                       after=slug_collision_stats(conn, table))
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
