#!/usr/bin/env python3
"""Re-embed dchub facilities with grid/ISO/state context for higher semantic
precision. Reads from Neon, builds augmented embedding text, calls Cloudflare
AI, upserts to Vectorize index dchub-facilities.

Why: the v1 embeddings used raw `name + provider + city`. Queries like
"30 MW with PJM access" landed Brazilian "PJM Net" facilities at the top
because the model didn't know what PJM means as a grid. v2 augments each
embedding with grid territory + market name + status + power band so the
model learns these relationships at the embedding layer (not just the
post-vectorize filter).

Idempotent + resumable. Checkpoints to /tmp/reembed.checkpoint after every
batch. Re-runs pick up where the last left off. Safe to interrupt with Ctrl-C.

Usage:
    # Dry-run: print what would be embedded for first 5 rows, no API calls
    python reembed_facilities.py --dry-run --limit 5

    # Real run, all rows
    python reembed_facilities.py

    # Resume from a known offset
    python reembed_facilities.py --start-offset 1500
"""
from __future__ import annotations

import argparse, hashlib, json, os, sys, time
import urllib.request, urllib.error
from pathlib import Path

import psycopg2
import psycopg2.extras

# ============================================================
# Config
# ============================================================
ACC = (os.environ.get('CLOUDFLARE_ACCOUNT_ID')
       or os.environ.get('CF_ACCOUNT_ID')
       or os.environ.get('ACC'))
TOKEN = (os.environ.get('CLOUDFLARE_API_TOKEN')
         or os.environ.get('CF_API_TOKEN'))
INDEX = 'dchub-facilities'
EMBED_MODEL = '@cf/baai/bge-base-en-v1.5'
BATCH_SIZE = 50
CHECKPOINT = Path('/tmp/reembed.checkpoint')
RATE_LIMIT_SLEEP_SEC = 0.3   # ~3 req/sec — safe for CF AI free tier
SOFT_FAIL_THRESHOLD = 5      # consecutive failures → abort

GRID_TERRITORIES = {
    'PJM':    {'PA','NJ','MD','DC','DE','OH','KY','NC','TN','IL','IN','MI','VA','WV'},
    'ERCOT':  {'TX'},
    'CAISO':  {'CA'},
    'SPP':    {'KS','OK','NE','ND','SD','AR','LA'},
    'MISO':   {'IL','IN','IA','MI','MN','MO','MS','MT','ND','SD','WI','AR','KY','LA'},
    'SOCO':   {'GA','AL','MS','FL'},
    'NYISO':  {'NY'},
    'ISO-NE': {'CT','MA','ME','NH','RI','VT'},
    'NWPP':   {'WA','OR','ID','MT','UT','WY'},
}

# Major DC markets — extend freely.
MARKETS = {
    ('VA', 'ashburn'):       'northern virginia',
    ('VA', 'sterling'):      'northern virginia',
    ('VA', 'leesburg'):      'northern virginia',
    ('VA', 'manassas'):      'northern virginia',
    ('VA', 'reston'):        'northern virginia',
    ('TX', 'dallas'):        'dallas fort worth',
    ('TX', 'fort worth'):    'dallas fort worth',
    ('TX', 'plano'):         'dallas fort worth',
    ('TX', 'austin'):        'austin',
    ('TX', 'san antonio'):   'san antonio',
    ('AZ', 'phoenix'):       'phoenix',
    ('AZ', 'mesa'):          'phoenix',
    ('AZ', 'chandler'):      'phoenix',
    ('IL', 'chicago'):       'chicago',
    ('IL', 'aurora'):        'chicago',
    ('NY', 'new york'):      'new york metro',
    ('NJ', 'piscataway'):    'new york metro',
    ('NJ', 'newark'):        'new york metro',
    ('GA', 'atlanta'):       'atlanta',
    ('OR', 'hillsboro'):     'pacific northwest',
    ('OR', 'portland'):      'pacific northwest',
    ('WA', 'quincy'):        'central washington',
    ('CA', 'santa clara'):   'silicon valley',
    ('CA', 'san jose'):      'silicon valley',
    ('CA', 'los angeles'):   'los angeles',
}


def grid_for_state(state):
    s = (state or '').upper().strip()
    for grid, states in GRID_TERRITORIES.items():
        if s in states:
            return grid
    return None


def market_for(city, state):
    return MARKETS.get(((state or '').upper().strip(), (city or '').lower().strip()))


def power_band(mw):
    try:
        m = float(mw or 0)
    except (TypeError, ValueError):
        return None
    if m >= 500: return 'gigawatt-class hyperscale'
    if m >= 200: return 'large hyperscale'
    if m >= 50:  return 'wholesale'
    if m >= 10:  return 'mid-size'
    if m > 0:    return 'small or edge'
    return None


def build_embedding_text(row):
    """Augment row data with grid/market/power-band context for embedding."""
    parts = [row.get('name') or 'data center']
    if row.get('provider'):
        parts.append(f"operated by {row['provider']}")
    loc = ', '.join(filter(None, [row.get('city'), row.get('state'), row.get('country')]))
    if loc:
        parts.append(f"located in {loc}")
    pm = row.get('power_mw')
    if pm:
        try:
            parts.append(f"{float(pm):.0f} megawatts capacity")
        except (TypeError, ValueError):
            pass
        band = power_band(pm)
        if band:
            parts.append(band)
    if row.get('status'):
        parts.append((row['status'] or '').lower())
    grid = grid_for_state(row.get('state'))
    if grid:
        parts.append(f"on the {grid} grid")
    market = market_for(row.get('city'), row.get('state'))
    if market:
        parts.append(f"in the {market} data center market")
    facility_type = row.get('facility_type')
    if facility_type:
        parts.append(facility_type.lower())
    if row.get('certifications'):
        parts.append(f"certifications: {row['certifications']}")
    return '. '.join(parts)


# ============================================================
# Cloudflare API helpers
# ============================================================

def cf_post(path, payload, timeout=30):
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACC}/{path}"
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8')), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'error': str(e)}, e.code


def cf_post_ndjson(path, ndjson_lines, timeout=60):
    """Vectorize upsert wants application/x-ndjson body."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACC}/{path}"
    body = '\n'.join(ndjson_lines).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {TOKEN}',
            'Content-Type': 'application/x-ndjson',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8')), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'error': str(e)}, e.code


def embed_batch(texts):
    """Embed a list of texts; CF AI accepts up to 100 strings per call."""
    out, status = cf_post(f'ai/run/{EMBED_MODEL}', {'text': texts})
    if status != 200 or not out.get('success'):
        return None, out
    return out['result']['data'], None


def vectorize_id(row):
    """Stable Vectorize ID per facility. Mirrors the existing 32-hex shape."""
    src = (str(row.get('id') or '') + '|' + (row.get('name') or '')).encode('utf-8')
    return hashlib.md5(src).hexdigest()


def vectorize_metadata(row):
    """Compact metadata stored alongside each vector."""
    md = {}
    for k in ('name', 'provider', 'city', 'state', 'country', 'status'):
        v = row.get(k)
        if v: md[k] = v
    if row.get('power_mw') is not None:
        try:
            md['power_mw'] = float(row['power_mw'])
        except (TypeError, ValueError):
            pass
    if row.get('latitude') is not None and row.get('longitude') is not None:
        try:
            md['lat'] = float(row['latitude'])
            md['lng'] = float(row['longitude'])
        except (TypeError, ValueError):
            pass
    grid = grid_for_state(row.get('state'))
    if grid:
        md['grid'] = grid
    market = market_for(row.get('city'), row.get('state'))
    if market:
        md['market'] = market
    return md


# ============================================================
# Main loop
# ============================================================

def load_checkpoint():
    if not CHECKPOINT.exists():
        return 0
    try:
        return int(CHECKPOINT.read_text().strip())
    except Exception:
        return 0


def save_checkpoint(offset):
    CHECKPOINT.write_text(str(offset))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None,
                    help='Cap total rows processed (default: all)')
    ap.add_argument('--start-offset', type=int, default=None,
                    help='Override checkpoint and start from this offset')
    ap.add_argument('--dry-run', action='store_true',
                    help='Build embedding text + metadata, but do not call CF or upsert')
    args = ap.parse_args()

    if not ACC or not TOKEN:
        print('ERROR: CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID (or ACC) must be set.',
              file=sys.stderr)
        sys.exit(2)

    db_url = (os.environ.get('DATABASE_URL')
              or os.environ.get('NEON_DATABASE_URL'))
    if not db_url:
        print('ERROR: DATABASE_URL not set.', file=sys.stderr)
        sys.exit(2)

    start = (args.start_offset
             if args.start_offset is not None
             else load_checkpoint())

    print(f'==> reembed_facilities — starting at offset={start}'
          f'{" (dry-run)" if args.dry_run else ""}')

    conn = psycopg2.connect(db_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute('SELECT COUNT(*) AS c FROM discovered_facilities')
    total = cur.fetchone()['c']
    print(f'==> {total:,} facilities total in Neon')

    processed = 0
    upserted = 0
    failures = 0
    consecutive_fail = 0
    offset = start

    while True:
        cur.execute("""
            SELECT id, name, provider, city, state, country,
                   latitude, longitude,
                   power_mw, status,
                   COALESCE(facility_type, NULL) AS facility_type,
                   NULL::text AS certifications,
                   NULL::text AS slug
            FROM discovered_facilities
            ORDER BY id
            LIMIT %s OFFSET %s
        """, (BATCH_SIZE, offset))
        rows = cur.fetchall()
        if not rows:
            break

        texts = [build_embedding_text(dict(r)) for r in rows]

        if args.dry_run:
            for r, t in zip(rows[:3], texts[:3]):
                print(f'   [dry] id={r["id"]}  text={t!r}')
            offset += len(rows)
            processed += len(rows)
            if args.limit and processed >= args.limit:
                break
            continue

        # Embed
        vectors, err = embed_batch(texts)
        if vectors is None:
            consecutive_fail += 1
            failures += 1
            print(f'   batch@{offset:>6} embed FAILED: {err}', file=sys.stderr)
            if consecutive_fail >= SOFT_FAIL_THRESHOLD:
                print('==> too many consecutive failures, aborting', file=sys.stderr)
                break
            time.sleep(2 * consecutive_fail)
            continue
        consecutive_fail = 0

        # Upsert (NDJSON)
        ndjson = []
        for row, vec in zip(rows, vectors):
            rd = dict(row)
            ndjson.append(json.dumps({
                'id':       vectorize_id(rd),
                'values':   vec,
                'metadata': vectorize_metadata(rd),
            }))
        up, status = cf_post_ndjson(
            f'vectorize/v2/indexes/{INDEX}/upsert', ndjson)
        if status != 200 or not up.get('success'):
            consecutive_fail += 1
            failures += 1
            print(f'   batch@{offset:>6} upsert FAILED: {up}', file=sys.stderr)
            if consecutive_fail >= SOFT_FAIL_THRESHOLD:
                print('==> too many consecutive failures, aborting', file=sys.stderr)
                break
            time.sleep(2 * consecutive_fail)
            continue

        upserted += len(rows)
        offset += len(rows)
        processed += len(rows)
        save_checkpoint(offset)
        pct = processed * 100.0 / total if total else 0
        print(f'   batch@{offset - len(rows):>6} +{len(rows)} '
              f'(processed={processed:,}/{total:,}, {pct:.1f}%, '
              f'upserted={upserted:,}, failures={failures})')

        if args.limit and processed >= args.limit:
            break
        time.sleep(RATE_LIMIT_SLEEP_SEC)

    print(f'==> done: processed={processed:,} upserted={upserted:,} failures={failures}')
    print(f'==> checkpoint at /tmp/reembed.checkpoint = {load_checkpoint()}')
    conn.close()


if __name__ == '__main__':
    main()
