"""
Deal Ingestion Scheduler — Background thread for Railway
=========================================================
Scrapes Google News RSS for data center M&A deals every 6 hours
and inserts them into the Neon PostgreSQL ai_deals table.

Starts automatically when Flask boots via:
    from deal_ingestion_scheduler import start_deal_scheduler
    start_deal_scheduler(get_db)
"""

import hashlib
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, date

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
INTERVAL_HOURS = int(os.getenv('DEAL_INGEST_INTERVAL_HOURS', '6'))
INTERVAL_SECONDS = INTERVAL_HOURS * 3600

RSS_FEEDS = [
    'https://news.google.com/rss/search?q=data+center+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=data+center+merger&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=AI+infrastructure+deal&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=hyperscale+data+center+investment&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=data+center+land+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
    'https://news.google.com/rss/search?q=colocation+acquisition&hl=en-US&gl=US&ceid=US%3Aen',
]

DEAL_TYPE_PATTERNS = {
    'acquisition':   r'\b(acquire[ds]?|acquisition|acquiring|bought|purchase[ds]?)\b',
    'merger':        r'\b(merge[drs]?|merger|combining)\b',
    'joint_venture': r'\b(joint.%sventure|jv|partnership|teaming)\b',
    'investment':    r'\b(invest(?:s|ed|ing|ment)?|fund(?:s|ed|ing)?|stake|raise[ds]?)\b',
    # r68 (Nico audit #1): REMOVED 'land_acquisition' (land|site|campus|acre|
    # parcel|property) — it matched nearly EVERY data-center article, turning
    # routine news into fake "deals". Real land/site deals still classify under
    # 'acquisition' when an explicit acquisition verb is present.
    'divestiture':   r'\b(divest(?:s|ed|ing|iture)?|spin.?off)\b',
}

MONEY_RE = re.compile(r'\$\s*(\d+(?:\.\d+)?)\s*(billion|million|thousand|[KMB])\b', re.I)


# ── Helpers ───────────────────────────────────────────────────────────
def _money_to_usd(text):
    """Extract first monetary value from text, return as USD float or None."""
    m = MONEY_RE.search(text)
    if not m:
        return None, None
    val, unit = float(m.group(1)), m.group(2).upper()
    if unit in ('B', 'BILLION'):
        usd = val * 1_000_000_000
        display = f"${val}B"
    elif unit in ('M', 'MILLION'):
        usd = val * 1_000_000
        display = f"${val}M"
    elif unit in ('K', 'THOUSAND'):
        usd = val * 1_000
        display = f"${val}K"
    else:
        usd = val
        display = f"${val}"
    return usd, display


def _deal_type(text):
    low = text.lower()
    for dtype, pat in DEAL_TYPE_PATTERNS.items():
        if re.search(pat, low):
            return dtype
    return 'unknown'


def _extract_companies(text):
    """Heuristic: sequences of capitalised words (≥2 chars each)."""
    stop = {'The', 'And', 'For', 'With', 'From', 'Data', 'Center', 'New', 'Report',
            'Global', 'North', 'South', 'East', 'West', 'United', 'States', 'Google',
            'News', 'Reuters', 'Bloomberg', 'According', 'Monday', 'Tuesday',
            'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'January',
            'February', 'March', 'April', 'May', 'June', 'July', 'August',
            'September', 'October', 'November', 'December'}
    matches = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', text)
    seen, out = set(), []
    for m in matches:
        if m not in seen and m not in stop and len(m) > 3:
            seen.add(m)
            out.append(m)
    return out[:8]


def _deal_hash(buyer, seller, deal_date, value_str):
    # r68 (Nico audit #2): EXCLUDE deal_date. The same event re-appearing in RSS
    # on a later day (or parsed to a slightly different date) used to produce a
    # NEW hash → a new AUTO-<date> row → the same deal multiplied into dozens of
    # rows, inflating the count. Hashing on parties + value only makes ON CONFLICT
    # collapse re-sightings into ONE row. (deal_date kept in the signature so call
    # sites don't change.)
    raw = f"{(buyer or '').strip().lower()}|{(seller or '').strip().lower()}|{(value_str or '').strip().lower()}"
    return hashlib.md5(raw.encode()).hexdigest()


# r68 (Nico audit #1): directional buyer→seller from an EXPLICIT relationship
# verb only. Replaces the old "first two capitalised words in the headline"
# heuristic that manufactured fake deals ("Anthropic → xAI", "Nvidia IPO 2026")
# from any two company-ish names. No verb linking two named entities → not a
# deal we can stand behind → skipped.
_DEAL_REL_RE = re.compile(
    r"\b([A-Z][\w.&'\-]+(?:\s+[A-Z][\w.&'\-]+){0,3})\s+"
    r"(?:has\s+)?(?:agreed\s+to\s+)?"
    r"(acquires?|acquired|buys?|bought|to\s+acquire|to\s+buy|merges?\s+with|"
    r"purchases?|takes?\s+over|snaps?\s+up|invests?\s+in|to\s+invest\s+in|backs?)\s+"
    r"([A-Z][\w.&'\-]+(?:\s+[A-Z][\w.&'\-]+){0,3})\b"
)
_REL_STOP = {'The', 'Data', 'Center', 'Centre', 'New', 'Global', 'AI', 'US',
             'Report', 'News', 'A', 'An', 'Its', 'Their', 'This', 'That'}


def _extract_acquisition(title):
    """Return (buyer, seller) ONLY from an explicit relationship verb, else
    (None, None). Directional: 'X acquires/buys/invests in Y' → buyer=X, seller=Y."""
    m = _DEAL_REL_RE.search(title or '')
    if not m:
        return None, None
    buyer, seller = m.group(1).strip(), m.group(3).strip()
    if (buyer in _REL_STOP or seller in _REL_STOP
            or len(buyer) < 3 or len(seller) < 3
            or buyer.lower() == seller.lower()):
        return None, None
    return buyer, seller


def _parse_date(published_str):
    """Best-effort date parse from RSS published field."""
    if not published_str:
        return date.today()
    for fmt in ('%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S %z',
                '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d'):
        try:
            return datetime.strptime(published_str.strip(), fmt).date()
        except ValueError:
            continue
    return date.today()


# ── Semantic near-duplicate gate (r-rag-dealdedup 2026-07-04) ─────────
# The md5 deal_hash only collapses EXACT buyer|seller|value strings —
# "Core Weave Inc." vs "CoreWeave" (or a re-worded headline) still produced
# two rows, which a hand-maintained normalize dict can never keep up with.
# For hash-NEW deals only, ask brain RAG for the closest known deals and
# decide with a LOCAL embedding cosine.
#
# CRITICAL: retrieve_context() 'score' is a cross-encoder RERANK relevance
# (absolute values often 0.05-0.3 even for excellent hits) — NEVER threshold
# on it for dedup. Instead we re-embed [query]+candidate_texts in ONE _embed
# call (input_type=search_document for both sides — symmetric comparison)
# and compute cosine locally (0-1 scale).
#
# Conservative (r-rag-dealdedup blocker fix 2026-07-04): the gate may DROP a
# deal ONLY when BOTH values are disclosed AND within DEAL_DUP_VALUE_TOL.
# Same parties but CLEARLY different value (> tolerance) is a follow-on
# transaction → insert. One/both values undisclosed → ALWAYS KEEP: repeat
# counterparties do multiple deals, and with no value to disambiguate, a
# duplicate row is recoverable (merge later) while a silently dropped real
# deal is PERMANENT LOSS. Everything fail-soft: any embed/RAG/DB failure
# keeps the deal(s) → EXACT pre-existing md5-only behavior.

def _env_float(name, default):
    try:
        return float((os.getenv(name) or '').strip() or default)
    except Exception:
        return float(default)


def _env_int(name, default):
    try:
        return int((os.getenv(name) or '').strip() or default)
    except Exception:
        return int(default)


def _cosine(a, b):
    """Local cosine similarity between two vectors (0-1 for Cohere embeds)."""
    try:
        dot = na = nb = 0.0
        for x, y in zip(a, b):
            x = float(x); y = float(y)
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / math.sqrt(na * nb)
    except Exception:
        return 0.0


def _values_confirm_dup(new_usd, cand_usd, tol):
    """True ONLY when BOTH deal values are disclosed AND within tol —
    the sole case where a high cosine may drop the row as a duplicate.

    Blocker fix (2026-07-04): the extracted dicts carry no market/region,
    so buyer+seller repeats look near-identical to the embedder. If either
    value is undisclosed (None/0/unparseable) we can NOT distinguish a
    re-sighting from a genuinely new follow-on deal between the same
    counterparties → return False (KEEP). A duplicate row is recoverable;
    a dropped real deal is permanent loss."""
    try:
        a = float(new_usd) if new_usd else 0.0
        b = float(cand_usd) if cand_usd else 0.0
    except Exception:
        return False
    if a <= 0.0 or b <= 0.0:
        return False  # one/both undisclosed → never enough evidence to drop
    hi, lo = (a, b) if a >= b else (b, a)
    return (hi - lo) / hi <= tol


def _candidate_value_usd(cur, source_table, source_id):
    """Deal value in USD for a RAG candidate row, or None if undisclosed or
    unreadable (treated as undisclosed → the new deal is KEPT, never dropped).
    NOTE units: deals.value is stored in $MILLIONS (see
    seed_comprehensive_deals.py — Stargate=500000 == $500B);
    ai_deals.deal_value_usd is raw USD."""
    if cur is None or not source_id:
        return None
    try:
        if source_table == 'deals':
            cur.execute("SELECT value FROM deals WHERE id = %s", (str(source_id),))
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0]) * 1_000_000.0
        elif source_table == 'ai_deals':
            cur.execute("SELECT deal_value_usd FROM ai_deals WHERE id::text = %s",
                        (str(source_id),))
            row = cur.fetchone()
            if row and row[0]:
                return float(row[0])
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
    return None


def _semantic_dup_check(deal, cur, retrieve_fn, embed_fn):
    """Check ONE hash-new deal against the RAG 'deals' corpus.
    Returns ('dup', info) | ('keep', None) | ('error', reason).
    'error' = embed/RAG infra failure → caller degrades to md5-only."""
    buyer = (deal.get('buyer') or '').strip()
    seller = (deal.get('seller') or '').strip()
    if not (buyer or seller):
        return 'keep', None
    # Blocker fix (2026-07-04): run_ingestion's extracted dicts carry NO
    # market/region keys, so the old query degenerated to buyer+seller only
    # and repeat counterparties all looked alike. Enrich with the
    # discriminators that DO exist on the dict — deal_type + deal_date —
    # mirroring the corpus text shape "buyer → seller (type, market) notes"
    # so distinct follow-on deals between the same parties separate better.
    # (market/region kept as a fallback for future extractor enrichment.)
    query = " ".join(p for p in (
        buyer, seller,
        (deal.get('deal_type') or '').strip(),
        (deal.get('market') or deal.get('region') or '').strip(),
        (deal.get('deal_date') or '').strip()) if p)
    try:
        hits = retrieve_fn(query, k=5, corpus="deals") or []
    except Exception as e:
        return 'error', f"retrieve_context: {e}"
    hits = [h for h in hits if (h.get('text') or '').strip()]
    if not hits:
        # No candidates ≠ infra failure (retrieve_context is fail-soft → []).
        return 'keep', None
    texts = [h['text'] for h in hits]
    try:
        vecs = embed_fn([query] + texts, input_type="search_document")
    except Exception as e:
        return 'error', f"_embed: {e}"
    if not vecs or len(vecs) != len(texts) + 1:
        return 'error', '_embed returned no/short embeddings'
    qv = vecs[0]
    best_ix, best_cos = -1, -1.0
    for i in range(len(texts)):
        cs = _cosine(qv, vecs[i + 1])
        if cs > best_cos:
            best_cos, best_ix = cs, i
    # brain-ascension #28 wave 2 (2026-07-25): 0.92 VALIDATED for
    # mistral-embed by live measurement — near-dup paraphrases score
    # 0.925-0.93, distinct same-topic docs <=0.86. Registered as
    # PROVIDER_COSINE_GATES['mistral']['dup_strict'] in routes/brain_rag.py.
    if best_cos < _env_float('DEAL_DUP_COSINE', 0.92):
        return 'keep', None
    hit = hits[best_ix]
    cand_usd = _candidate_value_usd(cur, hit.get('source_table'), hit.get('source_id'))
    if not _values_confirm_dup(deal.get('deal_value_usd'), cand_usd,
                               _env_float('DEAL_DUP_VALUE_TOL', 0.15)):
        # Undisclosed value(s) OR clearly different value → KEEP. Only two
        # DISCLOSED values within tolerance confirm a duplicate; anything
        # less could be a genuinely new follow-on deal (permanent loss if
        # dropped — a duplicate row is merely recoverable noise).
        return 'keep', None
    return 'dup', {'match_table': hit.get('source_table'),
                   'match_id': hit.get('source_id'),
                   'cosine': round(best_cos, 4)}


def _semantic_dedup_pass(deals, get_db):
    """Drop semantic near-duplicates from `deals` before the upsert — the
    same mark-as-duplicate mechanism the in-batch md5 path uses (skip + log).
    md5 FAST PATH unchanged: hashes already in ai_deals just re-upsert via
    ON CONFLICT (confidence bump) and are never semantically checked.
    Fail-soft at EVERY layer: on any failure, returns `deals` unchanged."""
    if not deals:
        return deals
    if (os.getenv('DEAL_SEMANTIC_DEDUP', '1') or '1').strip().lower() in (
            '0', 'false', 'off', 'no'):
        return deals
    try:
        from routes.brain_rag import retrieve_context, _embed
    except Exception as e:
        logger.debug(f"Semantic dedup unavailable (brain_rag import): {e}")
        return deals
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT deal_hash FROM ai_deals WHERE deal_hash = ANY(%s)",
                        ([d.get('deal_hash') for d in deals],))
            known = {r[0] for r in cur.fetchall()}
        except Exception as e:
            logger.warning(f"  Semantic dedup: hash lookup failed (fail-soft, md5-only): {e}")
            return deals
        # Cap Cohere spend per run: each check = 1 retrieve + 1 embed call.
        max_checks = _env_int('DEAL_DUP_MAX_CHECKS', 25)
        kept, dropped, checks, aborted = [], 0, 0, False
        for d in deals:
            if aborted or d.get('deal_hash') in known or checks >= max_checks:
                kept.append(d)
                continue
            checks += 1
            verdict, info = _semantic_dup_check(d, cur, retrieve_context, _embed)
            if verdict == 'error':
                # Infra failure → EXACT current behavior for this deal AND the
                # rest of the batch (backend is down — don't hammer it).
                logger.warning(f"  Semantic dedup degraded to md5-only: {info}")
                kept.append(d)
                aborted = True
                continue
            if verdict == 'dup':
                dropped += 1
                logger.info(
                    f"  🔁 Semantic dedup: '{d.get('buyer')} → {d.get('seller')}' "
                    f"({d.get('deal_value_str') or 'undisclosed'}) ≈ existing "
                    f"{info['match_table']}#{info['match_id']} "
                    f"cos={info['cosine']} — skipping insert")
                continue
            kept.append(d)
        if dropped:
            logger.info(f"  Semantic dedup: dropped {dropped} near-duplicate(s) "
                        f"of {checks} checked "
                        f"(cosine>={_env_float('DEAL_DUP_COSINE', 0.92)})")
        return kept
    except Exception as e:
        logger.warning(f"  Semantic dedup pass failed (fail-soft, md5-only): {e}")
        return deals
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


# ── Core ingestion ────────────────────────────────────────────────────
def run_ingestion(get_db):
    """Single ingestion run: fetch RSS → extract deals → upsert into Neon."""
    try:
        import feedparser  # noqa: F401  (imported by feed_fetch.parse_feed)
    except ImportError:
        logger.error("feedparser not installed — pip install feedparser")
        return
    # bounded (connect, read) feed I/O — feedparser.parse(url) has NO timeout
    from util import feed_fetch

    logger.info("🔄 Deal ingestion starting...")
    articles = []

    # 1. Fetch RSS
    for url in RSS_FEEDS:
        try:
            feed = feed_fetch.parse_feed(url)
            for entry in getattr(feed, 'entries', []):
                articles.append({
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', ''),
                    'link': entry.get('link', ''),
                    'published': entry.get('published', ''),
                    'source': entry.get('source', {}).get('title', 'Google News'),
                })
        except Exception as e:
            logger.warning(f"RSS fetch error ({url}): {e}")

    logger.info(f"  Fetched {len(articles)} articles from {len(RSS_FEEDS)} feeds")

    # 2. Extract deals
    deals = []
    for art in articles:
        text = f"{art['title']} {art['summary']}"
        dtype = _deal_type(text)
        if dtype == 'unknown':
            continue
        buyer, seller = _extract_acquisition(art['title'])
        # r68 (Nico audit #1): publish ONLY a real directional deal — a verb-linked
        # buyer→seller AND a source link. No explicit relationship verb → skip
        # (this is what kills the fabricated "first two capitalised words" deals).
        if not buyer or not seller:
            continue
        if not art.get('link'):
            continue
        usd, display = _money_to_usd(text)
        deal_date = _parse_date(art['published'])
        deals.append({
            'buyer': buyer,
            'seller': seller or 'Undisclosed',
            'deal_type': dtype,
            'deal_value_usd': usd,
            'deal_value_str': display,
            'deal_date': str(deal_date),
            'source_url': art['link'],
            'source_name': art['source'],
            'description': f"{dtype}: {buyer} → {seller or 'Undisclosed'}",
            'deal_hash': _deal_hash(buyer, seller, str(deal_date), display),
        })

    logger.info(f"  Extracted {len(deals)} deals")
    if not deals:
        logger.info("  No new deals to insert")
        return

    # r43-H (2026-05-28): dedup by deal_hash BEFORE the batched upsert. The
    # extractor can emit the same deal twice in one run (one article matched
    # by two search queries), and `ON CONFLICT (deal_hash) DO UPDATE` raises
    # "cannot affect row a second time" when two rows in ONE command share the
    # conflict key — which failed the WHOLE batch (logs: 299 extracted, 0
    # inserted, 299 errors). Keep the first occurrence of each deal_hash.
    _seen, _deduped = set(), []
    for _d in deals:
        _h = _d.get("deal_hash")
        if _h in _seen:
            continue
        _seen.add(_h)
        _deduped.append(_d)
    if len(_deduped) != len(deals):
        logger.info(f"  Deduped {len(deals) - len(_deduped)} same-batch duplicate deal_hash(es)")
    deals = _deduped

    # r-rag-dealdedup (2026-07-04): semantic near-dup gate for hash-NEW deals
    # (md5 stays the fast path; ON CONFLICT still collapses exact re-sightings).
    # Fail-soft: any failure inside leaves `deals` unchanged (md5-only).
    try:
        deals = _semantic_dedup_pass(deals, get_db)
    except Exception as _e:
        logger.warning(f"  Semantic dedup crashed (fail-soft, md5-only): {_e}")

    # 3. Upsert into Neon
    # Phase ZZZZ-conn-fix (2026-05-18): pool watchdog was force-reclaiming
    # this connection after 76s because we never called conn.close(). Add
    # try/finally so the connection always returns to pool — even on
    # exception. Prevents pool-exhaustion cascade under load.
    # r42ai (2026-05-27): batch INSERT via execute_values. Pre-fix this
    # looped 314 sequential INSERTs which collectively exceeded Neon's
    # statement_timeout (~30s under load) → "canceling statement due
    # to statement timeout" silently dropping all new deal data. Batch
    # insert collapses 314 round-trips into 1; ON CONFLICT still
    # upserts the confidence-bump for re-seen deals.
    inserted, errors = 0, 0
    conn = None
    try:
        from psycopg2.extras import execute_values
        conn = get_db()
        cur = conn.cursor()

        # Convert dict rows to positional tuples in stable column order
        _cols = ("deal_hash", "buyer", "seller", "deal_type",
                  "deal_value_usd", "deal_value_str", "deal_date",
                  "source_url", "source_name", "description")
        rows = [tuple(d.get(c) for c in _cols) for d in deals]

        # 200-row chunks keep each round-trip well under statement_timeout
        # even on slow Neon-warm states. 314 deals → 2 chunks.
        CHUNK = 200
        for i in range(0, len(rows), CHUNK):
            batch = rows[i:i + CHUNK]
            try:
                execute_values(
                    cur,
                    """
                    INSERT INTO ai_deals (
                        deal_hash, buyer, seller, deal_type,
                        deal_value_usd, deal_value_str, deal_date,
                        source_url, source_name, description,
                        ai_detected, confidence, status
                    ) VALUES %s
                    ON CONFLICT (deal_hash) DO UPDATE SET
                        updated_at = NOW(),
                        confidence = GREATEST(ai_deals.confidence, EXCLUDED.confidence)
                    """,
                    batch,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, 70, 'active')",
                )
                inserted += len(batch)
                conn.commit()
            except Exception as e:
                errors += len(batch)
                logger.warning(f"  Batch insert error ({len(batch)} rows): {e}")
                try: conn.rollback()
                except Exception: pass
                cur = conn.cursor()
        cur.close()
    except Exception as e:
        logger.error(f"  DB error: {e}")
        errors = len(deals)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

    logger.info(f"  ✅ Ingestion complete: {inserted} inserted/updated, {errors} errors")
    # 2026-06-12: report to the autonomous-intelligence extraction ledger.
    # This job ran for months without the observatory ever seeing it — the
    # dashboard showed "1 sources" because only the data-pulse workflow wrote
    # to extraction_intelligence. Direct in-process write (no HTTP self-call).
    try:
        from routes.extractor_brain import record_extraction
        record_extraction("deal-ingestion",
                          "success" if not errors else "partial",
                          rows_inserted=inserted,
                          observations={"errors": errors})
    except Exception:
        pass  # ledger telemetry must never break ingestion


# ── Scheduler thread ──────────────────────────────────────────────────
_scheduler_running = False


def _scheduler_loop(get_db):
    """Runs in a daemon thread: ingest → sleep → repeat."""
    global _scheduler_running
    logger.info(f"📅 Deal ingestion scheduler started (every {INTERVAL_HOURS}h)")

    # Initial delay: let Flask finish booting (30 seconds)
    time.sleep(30)

    while _scheduler_running:
        try:
            # r78: leader-only — both replicas ran the full RSS fetch +
            # classify every 6h (deal_hash upserts made it safe but 2×
            # wasteful on external fetches and Neon writes).
            _lead = True
            try:
                from main import is_current_leader
                _lead = bool(is_current_leader())
            except Exception:
                pass
            if _lead:
                run_ingestion(get_db)
            else:
                logger.debug("Deal ingestion: not leader — skipping cycle")
        except Exception as e:
            logger.error(f"Ingestion error (will retry next cycle): {e}")
        # Sleep in 60-second chunks so we can stop cleanly
        for _ in range(INTERVAL_SECONDS // 60):
            if not _scheduler_running:
                break
            time.sleep(60)


def start_deal_scheduler(get_db):
    """Call from main.py after Flask app is configured.
    Starts a background daemon thread — won't block Flask."""
    global _scheduler_running
    if _scheduler_running:
        logger.info("Deal scheduler already running")
        return
    _scheduler_running = True
    t = threading.Thread(target=_scheduler_loop, args=(get_db,), daemon=True)
    t.name = "DealIngestionScheduler"
    t.start()
    logger.info("✅ Deal ingestion scheduler thread launched")


def stop_deal_scheduler():
    """Graceful stop (optional)."""
    global _scheduler_running
    _scheduler_running = False
    logger.info("Deal ingestion scheduler stopped")
