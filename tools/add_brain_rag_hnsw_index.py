#!/usr/bin/env python3
"""Add an HNSW index to brain_corpus_embeddings and measure the before/after.

Why: routes/brain_rag.py creates `brain_corpus_embeddings(embedding vector(1024))`
but NEVER builds an ANN index. Every recall query
(`ORDER BY embedding <=> $q LIMIT k`) is a brute-force sequential scan over the
whole corpus. That is the actual bottleneck — not "pgvector vs lakebase".

At our scale (tens of thousands of rows, not billions) the correct fix is a
single HNSW index: no extension install, no shared_preload_libraries change, no
compute restart, no query rewrite, and it survives scale-to-zero (the index is
persisted, not rebuilt on cold start). lakebase_ann only starts to pay off at
~1B+ vectors — ~4 orders of magnitude above this corpus.

This script:
  1. Reports corpus size (so the scale decision is on real numbers, not a guess).
  2. Times a representative recall query and prints its plan (seq scan today).
  3. Builds `brain_corpus_embeddings_hnsw` with CREATE INDEX CONCURRENTLY
     (online, non-blocking — safe to run against prod).
  4. Re-times the same query and prints the new plan (index scan) + speedup.

Idempotent: IF NOT EXISTS. Safe to re-run. CONCURRENTLY runs outside a txn.

Connection: NEON_DATABASE_URL or DATABASE_URL (same resolution as brain_rag._db()).
For a Neon branch benchmark, point that env var at the branch's connection string.

Usage:
    # Dry-run: size the corpus + show the current (seq-scan) plan, build nothing
    python tools/add_brain_rag_hnsw_index.py --dry-run

    # Real run: build the index and print before/after timing
    python tools/add_brain_rag_hnsw_index.py

    # Point at a Neon branch instead of prod
    NEON_DATABASE_URL='postgres://...branch...' python tools/add_brain_rag_hnsw_index.py
"""
import argparse
import os
import sys
import time

import psycopg2

INDEX_NAME = "brain_corpus_embeddings_hnsw"
TABLE = "brain_corpus_embeddings"
PROBES = 5          # how many timed recall queries to average
TOPK = 8            # LIMIT used by the real recall path


def _dsn() -> str:
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not du:
        sys.exit("ERROR: set NEON_DATABASE_URL or DATABASE_URL")
    return du


def _pgvector_version(cur) -> str:
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    return row[0] if row else ""


def _supports_hnsw(ver: str) -> bool:
    try:
        maj, minor, *_ = (int(x) for x in ver.split("."))
        return (maj, minor) >= (0, 5)   # HNSW landed in pgvector 0.5.0
    except Exception:
        return False


def _corpus_size(cur) -> int:
    cur.execute(f"SELECT count(*) FROM {TABLE}")
    return cur.fetchone()[0] or 0


def _has_index(cur) -> bool:
    cur.execute("SELECT 1 FROM pg_class WHERE relname = %s", (INDEX_NAME,))
    return cur.fetchone() is not None


# A representative recall query. Uses a stored embedding as the probe vector so
# we time the exact operator the app uses (<=>) with no round-trip of 1024 floats.
_RECALL_SQL = (
    f"SELECT id FROM {TABLE} "
    f"ORDER BY embedding <=> (SELECT embedding FROM {TABLE} "
    f"WHERE embedding IS NOT NULL LIMIT 1) LIMIT {TOPK}"
)


def _time_recall(cur) -> float:
    """Return median wall-clock ms over PROBES runs of the recall query."""
    samples = []
    for _ in range(PROBES):
        t0 = time.perf_counter()
        cur.execute(_RECALL_SQL)
        cur.fetchall()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2]


def _plan(cur) -> str:
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + _RECALL_SQL)
    return "\n".join(r[0] for r in cur.fetchall())


def _uses_index(plan: str) -> bool:
    return "Index Scan" in plan and INDEX_NAME in plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="size + show current plan, build nothing")
    args = ap.parse_args()

    conn = psycopg2.connect(_dsn(), connect_timeout=15)
    conn.autocommit = True  # CREATE INDEX CONCURRENTLY cannot run in a txn block
    cur = conn.cursor()

    ver = _pgvector_version(cur)
    n = _corpus_size(cur)
    print(f"pgvector: {ver or 'NOT INSTALLED'}   corpus rows: {n:,}")

    if n == 0:
        print("Corpus is empty — nothing to index. Run the reindex endpoint first.")
        return 0

    # ── BEFORE ────────────────────────────────────────────────────────────
    before_ms = _time_recall(cur)
    before_plan = _plan(cur)
    scan_kind = "Index Scan" if _uses_index(before_plan) else "Seq Scan (brute force)"
    print(f"\nBEFORE  median recall: {before_ms:7.2f} ms   ({scan_kind})")

    if _has_index(cur):
        print(f"Index {INDEX_NAME} already exists — nothing to build.")
        print("\n--- plan ---\n" + before_plan)
        return 0

    if args.dry_run:
        print("\n[dry-run] would build HNSW index — skipping. Current plan:\n")
        print(before_plan)
        return 0

    if not _supports_hnsw(ver):
        # Fallback: IVFFlat (pgvector < 0.5). lists ~= sqrt(rows), min 10.
        lists = max(10, int(n ** 0.5))
        ddl = (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
               f"ON {TABLE} USING ivfflat (embedding vector_cosine_ops) "
               f"WITH (lists = {lists})")
        print(f"\npgvector {ver} lacks HNSW — falling back to IVFFlat (lists={lists}).")
    else:
        ddl = (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
               f"ON {TABLE} USING hnsw (embedding vector_cosine_ops)")

    print(f"\nBuilding index (online, non-blocking)...\n  {ddl}")
    t0 = time.perf_counter()
    cur.execute(ddl)
    build_s = time.perf_counter() - t0
    print(f"Built in {build_s:.1f}s")

    # ── AFTER ─────────────────────────────────────────────────────────────
    cur.execute("SET hnsw.ef_search = 40")  # no-op for ivfflat; harmless
    after_ms = _time_recall(cur)
    after_plan = _plan(cur)
    scan_kind = "Index Scan" if _uses_index(after_plan) else "Seq Scan (planner declined index)"
    print(f"\nAFTER   median recall: {after_ms:7.2f} ms   ({scan_kind})")

    if before_ms > 0 and after_ms > 0:
        print(f"\nSpeedup: {before_ms / after_ms:.1f}x  "
              f"({before_ms:.2f} ms -> {after_ms:.2f} ms)")
    print("\n--- new plan ---\n" + after_plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
