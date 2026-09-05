"""
DC Hub — Public Endpoints (v2.0 - PostgreSQL)
===============================================
All queries use Neon PostgreSQL via db_utils.

Add ONE line to main.py:
    from public_endpoints import register_public_routes; register_public_routes(app)
"""

import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from db_utils import get_db, get_read_db
from util.deals import DEALS_OK

logger = logging.getLogger(__name__)

APP_VERSION = "v91"
APP_BUILD = 91
RELEASE_NOTES = "Dynamic version banner, public API endpoints, 401 fixes"
# r-founder99 (2026-06-26): limited founding-license campaign.
# r-founding-unify (2026-08-01): these constants are now FALLBACKS ONLY —
# the live numbers come from routes.founding_customers.founding_status()
# (cohort table + FOUNDING_CUSTOMERS_CAP env, default 25), the same source
# the homepage pill reads, so the two public counters can't drift apart.
FOUNDING_TOTAL = 25
FOUNDING_CLAIMED = 3

public_bp = Blueprint('public_endpoints', __name__)


# =============================================================================
# /api/v1/sitemap/ping — POST tells Google + Bing about updated content
# =============================================================================
# r41-sitemap-ping (2026-05-25): when LinkedIn quad, press publisher, or
# auto-press fires new content, call this endpoint to nudge search
# engines. Search bots usually find new sitemap content within 24-72h
# on their own crawl schedule; ping shortcuts that to minutes-to-hours.
#
# Idempotent + rate-limited (1 ping per minute per engine via in-process
# state) so an over-eager scheduler can't get us flagged as abusive.
# Returns immediately even if the ping fetch itself stalls — we don't
# block the caller on someone else's network.
_PING_STATE = {"last_google_ping": 0.0, "last_bing_ping": 0.0}

@public_bp.route('/api/v1/sitemap/ping', methods=['POST', 'GET'])
def sitemap_ping():
    import time as _ping_time
    import threading as _ping_thr
    import urllib.request as _ping_url
    sitemap = request.args.get('sitemap') or 'https://dchub.cloud/sitemap.xml'
    now = _ping_time.time()
    result = {'sitemap': sitemap, 'pinged': [], 'rate_limited': [], 'failed': []}

    def _fire(name, url, last_key, min_interval):
        if now - _PING_STATE.get(last_key, 0.0) < min_interval:
            result['rate_limited'].append(name)
            return
        _PING_STATE[last_key] = now
        def _bg():
            try:
                req = _ping_url.Request(url,
                    headers={'User-Agent': 'DCHub-SitemapPing/1.0'})
                with _ping_url.urlopen(req, timeout=10) as r:
                    if 200 <= r.status < 300:
                        return
                # non-2xx → quietly record in next pull; don't block
            except Exception as e:
                logger.warning(f"sitemap ping {name} failed: {e}")
        _ping_thr.Thread(target=_bg, daemon=True).start()
        result['pinged'].append(name)

    # Google: 1 ping per 5 min cap (their docs deprecate ping but it still
    # works — and we want to be polite). Bing: 1 per 5 min.
    _fire('google', f'https://www.google.com/ping?sitemap={sitemap}',
          'last_google_ping', 300)
    _fire('bing',   f'https://www.bing.com/ping?sitemap={sitemap}',
          'last_bing_ping',   300)
    return jsonify(result), 200, {'Cache-Control': 'no-store'}


# =============================================================================
# /api/v1/version — Public, no auth
# =============================================================================
_VERSION_CACHE = {"result": None, "ts": 0}

@public_bp.route('/api/v1/version', methods=['GET'])
def get_version():
    # r41-version-speed (2026-05-25): pre-fix this endpoint did 3 COUNT(*)
    # calls on facilities + deals on every request (4-5s cold, ~700ms
    # warm) AND had no Cache-Control so CF couldn't edge-cache it.
    # Hit by every monitor, dashboard, and AI-discovery crawler that
    # checks "what version is dchub on?". Fixes:
    #   (1) replace COUNT(*) with pg_class.reltuples (sub-ms planner
    #       statistic) — facilities is 21k rows, country DISTINCT is
    #       the slow one; keep that as-is since reltuples can't
    #       express DISTINCT.
    #   (2) 60s in-process cache so concurrent requests share work.
    #   (3) Cache-Control header so CF edge-caches 5min, with stale-
    #       while-revalidate so the first request after expiry never
    #       blocks waiting for our DB.
    import time as _t
    if _VERSION_CACHE["result"] and (_t.time() - _VERSION_CACHE["ts"]) < 60:
        return jsonify(_VERSION_CACHE["result"]), 200, {
            "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
        }
    result = {
        'version': APP_VERSION,
        'build': APP_BUILD,
        'release_notes': RELEASE_NOTES,
        # ★2026-08-01 DB-DOWN FALLBACK REBASE. These three were the PRE-DEDUP
        # over-claims and they ship verbatim whenever the DB read below raises
        # (the outer handler logs and returns `result` unchanged): 21,432 was
        # RAW discovered_facilities rows — a 1.36x over-claim on the 15,792
        # distinct buildings; 311 was the market literal retired from
        # ai_surface_canon.PINNED on 07-29 (+4 over live 307); 2,000 floored
        # deal ROWS, not the 1,662 distinct tracked deals. A fallback that
        # over-claims is worse than no fallback — it is served precisely when
        # nothing can correct it. Floors round DOWN, never up.
        'facilities': 15700,
        'markets': 300,
        'deals': 1600,
        'updated_at': datetime.utcnow().isoformat()
    }
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        # facilities count — DISTINCT sites after cross-source de-dup, to match
        # the honest /api/v1/stats headline, not the raw 22k. The 60s memo +
        # 5min CF cache mean it runs at most once a minute. Falls back to the
        # planner statistic if the deduped count fails.
        #
        # ★2026-08-01: was COUNT(*) WHERE duplicate_of_id IS NULL. That is the
        # `facilities_verified` field, and /api/v1/stats/canonical's own
        # provenance block says of it: "DE-DUPLICATION states, not source
        # verifications — do not publish either as 'verified'". It reads 14,062
        # live, which is BELOW the 15,000+ floor the old comment claimed to
        # match. The citable field is facilities_distinct = COUNT(DISTINCT
        # canonical_slug) = 15,792. Mirror that query exactly.
        try:
            cursor.execute(
                "SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                "WHERE canonical_slug IS NOT NULL")
            row = cursor.fetchone()
            if row and row[0]:
                result['facilities'] = int(row[0])
        except Exception:
            try:
                cursor.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = 'discovered_facilities'")
                row = cursor.fetchone()
                if row and row[0]:
                    result['facilities'] = int(row[0])
            except Exception:
                pass
        # markets — r73: this used COUNT(DISTINCT country) FROM facilities, which
        # is a COUNTRY count (~179) mislabeled as "markets", AND read the legacy
        # facilities table. Use the canonical DCPI market count (232) instead.
        try:
            from canonical_stats import get_canonical_stats as _gcs
            # ★2026-08-01: the `or 232` default made the retired "311" literal
            # reachable — .get('markets', 311) returns 311 when the key is
            # absent, 311 is truthy, so the `or` never fires and 311 ships.
            _m = int((_gcs() or {}).get('markets') or 0)
            if _m:
                result['markets'] = _m
        except Exception:
            pass   # keep the canon-aligned 300 floor set above, not a stale 232
        # deals count via planner statistic
        try:
            cursor.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE relname = 'deals'")
            row = cursor.fetchone()
            if row and row[0]:
                result['deals'] = int(row[0])
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"/api/v1/version DB error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    _VERSION_CACHE["result"] = result
    _VERSION_CACHE["ts"] = _t.time()
    return jsonify(result), 200, {
        "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
    }


# =============================================================================
# /api/founding-members — Public, no auth
# =============================================================================
@public_bp.route('/api/founding-members', methods=['GET'])
def founding_members_status():
    # r-founding-unify (2026-08-01): read the ONE source of truth shared with
    # /api/v1/founding-customers/count (the homepage pill). Before this, the
    # two public counters contradicted each other (homepage 14/25 vs pricing
    # 9/10) — this endpoint counted users.plan='founding' against a hardcoded
    # total of 10, so ONE more sale flipped the pricing card to "All founding
    # licenses claimed" and self-disabled the money CTA. Cohort + cap now both
    # come from routes.founding_customers (FOUNDING_CUSTOMERS_CAP env,
    # default 25 — the owner's scarcity knob).
    # 2026-09-02: that counter now counts the $99 founding SKU only (owner
    # decision) instead of the first 25 paid customers of any plan. Nothing
    # changes here — this route publishes founding_status()'s numbers and
    # computes none of its own, which is the property
    # tests/test_founding_counter_counts_the_sku.py pins.
    try:
        from routes.founding_customers import founding_status
        st = founding_status()
        total = st['cap']
        claimed = st['claimed']
        remaining = st['remaining']
        program_active = st['program_active']
    except Exception:
        # Legacy inline fallback so this endpoint never 500s if the import
        # breaks. Same shape, module-constant total.
        claimed = FOUNDING_CLAIMED
        conn = None
        try:
            conn = get_read_db()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT COUNT(*) FROM users WHERE plan = 'founding'")
                row = cursor.fetchone()
                if row and row[0] is not None:
                    claimed = row[0]
            except Exception:
                pass
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        total = FOUNDING_TOTAL
        remaining = max(0, total - claimed)
        program_active = remaining > 0
    # r-price-collapse (2026-09-05): the founding PROGRAM is retired — $99 is
    # simply the Pro list price now, so there is no longer a higher price to
    # strike through and no scarcity to count. `regular_price` used to be 299;
    # publishing it beside a $99 that anyone can buy is a fake anchor, so it is
    # now None. `program_active` is False so any surface still reading this
    # endpoint stops rendering a countdown. The claimed/remaining counters are
    # left in the payload (still truthfully computed) rather than removed, so
    # no consumer KeyErrors on deploy.
    from tier_registry import price as _canon_price
    return jsonify({
        'total': total,
        'claimed': claimed,
        'remaining': remaining,
        'price': _canon_price('pro'),
        'regular_price': None,
        'program_active': False,
        'retired': True,
        'note': 'Founding is retired — $99/mo is the Pro list price.',
    })


# =============================================================================
# /api/v1/map/public — Public facility markers, no auth
# =============================================================================
@public_bp.route('/api/v1/map/public', methods=['GET'])
def public_map_data():
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, city, state, country, region,
                   latitude, longitude, power_mw, status, tier
            FROM facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            LIMIT 5000
        """)
        facilities = []
        for row in cursor.fetchall():
            facilities.append({
                'id': row[0], 'name': row[1], 'provider': row[2],
                'city': row[3], 'state': row[4], 'country': row[5],
                'region': row[6], 'lat': row[7], 'lng': row[8],
                'power_mw': row[9], 'status': row[10], 'tier': row[11]
            })
        return jsonify({'success': True, 'count': len(facilities), 'facilities': facilities})
    except Exception as e:
        logger.error(f"/api/v1/map/public error: {e}")
        return jsonify({'success': False, 'error': str(e), 'facilities': []}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# Phase UU-3 (2026-05-15): removed @public_bp.route('/api/v1/map') below —
# main.py:1879 has a richer implementation (joins discovered_facilities
# + facilities, generates slugs, returns 19K rows). Tests confirmed the
# live endpoint serves main.py's version. The blueprint version was the
# shadow. Function body kept as `_unused_public_map_view` for reference;
# delete in next cleanup pass.
def _unused_public_map_view():
    conn = None
    try:
        from flask import request as req
        load_all = req.args.get('all', '').lower() in ('true', '1', 'yes')
        limit = min(int(req.args.get('limit', 2000)), 50000)
        offset = int(req.args.get('offset', 0))

        conn = get_read_db()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM facilities WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
        total = cursor.fetchone()[0] or 0

        if load_all:
            cursor.execute("""
                SELECT id, name, city, state, country, latitude, longitude,
                       provider, power_mw, tier, status
                FROM facilities
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (limit, offset))
        else:
            cursor.execute("""
                SELECT id, name, city, state, country, latitude, longitude,
                       provider, power_mw, tier, status
                FROM facilities
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (limit, offset))

        facilities = []
        for row in cursor.fetchall():
            facilities.append({
                'id': row[0], 'name': row[1], 'city': row[2],
                'state': row[3], 'country': row[4], 'latitude': row[5],
                'longitude': row[6], 'provider': row[7], 'power_mw': row[8],
                'tier': row[9], 'status': row[10]
            })
        return jsonify({'success': True, 'data': facilities, 'facilities': facilities, 'total': total, 'count': len(facilities), 'offset': offset, 'has_more': (offset + len(facilities)) < total})
    except Exception as e:
        logger.error(f"/api/v1/map error: {e}")
        return jsonify({'success': False, 'error': str(e), 'facilities': [], 'total': 0}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# /api/transactions/public — Public M&A deals, no auth
# =============================================================================
@public_bp.route('/api/transactions/public', methods=['GET'])
def public_transactions():
    limit = request.args.get('limit', 15, type=int)
    limit = min(limit, 25)
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        # Public, unauthenticated transactions list. The guard is
        # interpolated beside a params tuple — safe only because DEALS_OK is
        # %-free by construction (util/deals.py).
        cursor.execute(f"""
            SELECT id, buyer, seller, value, mw, type, region, market, date, notes
            FROM deals
            WHERE buyer IS NOT NULL AND buyer != '' AND buyer != 'TBD'
              AND value > 0 AND value < 1000000
              AND LENGTH(buyer) > 3
              AND {DEALS_OK}
            ORDER BY date DESC NULLS LAST, value DESC
            LIMIT %s
        """, (limit,))
        transactions = []
        for row in cursor.fetchall():
            rid, buyer, seller, value, mw, dtype, region, market, date, notes = row
            value = value or 0
            if value >= 1000:
                value_display = f"${value/1000:.1f}B"
            elif value > 0:
                value_display = f"${value:.0f}M"
            else:
                value_display = "Undisclosed"
            transactions.append({
                'id': rid,
                'title': f"{buyer} {'acquires' if dtype in ('Acquisition','ma') else 'invests in'} {seller or 'N/A'}",
                'buyer': buyer, 'seller': seller,
                'value_usd_millions': value, 'value_display': value_display,
                'power_mw': mw or 0, 'type': dtype,
                'region': region, 'market': market,
                'date': date, 'notes': notes,
                'source': 'DC Hub Intelligence',
            })
        # r-deals-gate (2026-07-10): this /public twin leaked deal $ value +
        # power_mw + seller to anonymous callers, bypassing the /api/v1/
        # transactions anon path (3 newest, value+MW masked). Gate to match:
        # anon → 3 newest with value/MW/seller masked; privileged callers
        # (key/internal/logged-in browser) get the full payload unchanged.
        try:
            from routes.tier_gate import caller_is_privileged
            _priv = caller_is_privileged("IDENTIFIED")
        except Exception:
            _priv = False
        if not _priv:
            transactions = transactions[:3]
            for _t in transactions:
                _t['seller'] = None
                _t['value_usd_millions'] = None
                _t['value_display'] = '🔒 upgrade'
                _t['power_mw'] = None
                _t['title'] = f"{_t.get('buyer')} — deal details gated"
            return jsonify({
                'success': True, 'data': transactions,
                'total': len(transactions), 'gated': True,
                'tier_required': 'starter',
                'message': ('Preview: 3 newest deals with value + MW masked. The '
                            'full M&A tracker requires Starter ($9/mo); a free dev '
                            'key unlocks the basics — https://dchub.cloud/pricing'),
                'upgrade_url': 'https://dchub.cloud/pricing?utm_source=transactions_public',
            })
        return jsonify({'success': True, 'data': transactions, 'total': len(transactions)})
    except Exception as e:
        logger.error(f"/api/transactions/public error: {e}")
        return jsonify({'success': False, 'error': str(e), 'data': [], 'total': 0}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# /api/v1/market-report — Public market summary, no auth
# =============================================================================
@public_bp.route('/api/v1/market-report', methods=['GET'])
def market_report():
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        # ★2026-07-30 — wrong-table pairing, same audit as the countries fix
        # (#1958/#1966): this counted the LEGACY `facilities` table while
        # /api/v1/version above serves the deduped discovered count. Same
        # query + same fallback shape as version: DISTINCT sites after
        # cross-source de-dup, planner statistic if the deduped count fails.
        total_facilities = 0
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM discovered_facilities WHERE duplicate_of_id IS NULL")
            row = cursor.fetchone()
            if row and row[0]:
                total_facilities = int(row[0])
        except Exception:
            try:
                cursor.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = 'discovered_facilities'")
                row = cursor.fetchone()
                if row and row[0]:
                    total_facilities = int(row[0])
            except Exception:
                pass
        total_transactions = 0
        try:
            cursor.execute(f"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}")
            total_transactions = cursor.fetchone()[0] or 0
        except Exception:
            pass
        # ★2026-07-30 — same defect r73 fixed on /api/v1/version above: a
        # COUNTRY count mislabeled as "markets", read from the LEGACY
        # `facilities` table (mixed name/ISO formats — "USA"+"US" — so even as
        # a country count it over-stated). Same fix: serve the canonical DCPI
        # market count so the two public 'markets' fields in this file agree.
        markets = 0
        try:
            from canonical_stats import get_canonical_stats as _gcs
            markets = int((_gcs() or {}).get('markets', 0) or 0)
        except Exception:
            pass
        return jsonify({
            'success': True,
            'report': {
                'total_facilities': total_facilities,
                'total_transactions': total_transactions,
                'markets': markets,
                'last_updated': datetime.utcnow().isoformat()
            }
        })
    except Exception as e:
        logger.error(f"/api/v1/market-report error: {e}")
        return jsonify({'success': False, 'error': str(e), 'report': {}}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# /api/v1/market-growth — Market growth data with projections
# =============================================================================
@public_bp.route('/api/v1/market-growth', methods=['GET'])
def market_growth():
    current_facilities = 0
    current_power = 0
    conn = None
    try:
        conn = get_read_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM facilities")
        current_facilities = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw > 0")
        current_power = cursor.fetchone()[0] or 0
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    growth_data = {
        'success': True,
        'years': [2020, 2021, 2022, 2023, 2024, 2025, 2026],
        'facilities': [4200, 5100, 6300, 7800, 9200, current_facilities or 10436, int((current_facilities or 10436) * 1.18)],
        'power_gw': [35.2, 42.1, 52.8, 68.5, 89.3, 118.0, 145.0],
        'investment_billions': [45, 58, 72, 95, 142, 220, 310],
        'market_size_billions': [59.3, 68.0, 79.2, 93.8, 114.5, 142.0, 178.0],
        'projection_note': '2026 values are projections based on current market trends',
        'sources': ['CBRE', 'JLL', 'Cushman & Wakefield', 'DC Hub Intelligence'],
        'generated_at': datetime.utcnow().isoformat()
    }

    return jsonify(growth_data)


# =============================================================================
# REGISTRATION
# =============================================================================
def register_public_routes(app):
    """Register all public endpoints on the Flask app."""
    app.register_blueprint(public_bp)
    logger.info("✅ Public Endpoints registered: /api/v1/version, /api/founding-members, /api/v1/map/public, /api/transactions/public")
    print("✅ Public Endpoints registered: /api/v1/version, /api/founding-members, /api/v1/map/public, /api/transactions/public")
