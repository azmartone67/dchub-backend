"""One-shot: dedup brain_findings on (issue, url), then add the UNIQUE index.

WHY (2026-07-18): brain_findings has only PRIMARY KEY (id) — the UNIQUE
(issue, url) the radar's DDL claims never existed on live, so racing
inserts (and phantom resolve paths) piled up duplicate rows: 6 groups /
225 rows live, worst slo_hard_burn ×131. Dup open rows double-appear on
the chronic-episode leaderboard; dup resolved rows inflate MTTR counts.
The canonical writer (routes/brain_findings_writer.upsert_brain_finding)
is already safe under the index: its UPDATE-first path keys on
(issue, url) and its INSERT is savepoint-wrapped (a unique-violation race
rolls back to "skipped"). No other live brain_findings ON CONFLICT
writers exist (grepped 2026-07-18).

Survivor per group: open rows beat resolved, then latest last_seen, then
highest id. Merged onto the survivor: MIN(first_seen), MAX(last_seen),
GREATEST of count/seen_count/episode_count/episode_seen_count. Losers are
DELETEd (their brain_corpus_embeddings rows are GC'd by the rag orphan
sweep on the next reindex).

Run:  DATABASE_URL=postgres://... python3 tools/dedup_brain_findings_unique.py [--dry-run]
"""
import os
import sys

import psycopg2

DRY = "--dry-run" in sys.argv

INDEX_SQL = ("CREATE UNIQUE INDEX IF NOT EXISTS brain_findings_issue_url_uniq "
             "ON brain_findings (issue, url)")


def main():
    du = (os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not du:
        print("DATABASE_URL required"); sys.exit(1)
    c = psycopg2.connect(du, connect_timeout=10)
    cur = c.cursor()
    if DRY:
        cur.execute("SET default_transaction_read_only = on")

    # Writer coerces url to '' — normalize any NULLs so the unique index
    # matches its semantics (none live today; belt for other branches).
    if not DRY:
        cur.execute("UPDATE brain_findings SET url = '' WHERE url IS NULL")
        if cur.rowcount:
            print(f"normalized {cur.rowcount} NULL urls -> ''")

    cur.execute("""
        SELECT issue, url, array_agg(id ORDER BY
                   (coalesce(status,'open') NOT IN ('resolved','wont_fix')) DESC,
                   last_seen DESC NULLS LAST, id DESC)
          FROM brain_findings
         GROUP BY 1, 2
        HAVING count(*) > 1
    """)
    groups = cur.fetchall()
    deleted = 0
    for issue, url, ids in groups:
        survivor, losers = ids[0], ids[1:]
        print(f"[{issue[:60]} | {url[:60]}] keep id={survivor}, "
              f"drop {len(losers)}: {losers[:8]}{'...' if len(losers) > 8 else ''}")
        if DRY:
            deleted += len(losers)
            continue
        cur.execute("""
            UPDATE brain_findings s SET
                first_seen = LEAST(s.first_seen, g.min_first),
                last_seen  = GREATEST(s.last_seen, g.max_last),
                count      = GREATEST(coalesce(s.count,1), g.max_count),
                seen_count = GREATEST(coalesce(s.seen_count,1), g.max_seen),
                episode_count = GREATEST(coalesce(s.episode_count,1), g.max_ep),
                episode_seen_count = GREATEST(coalesce(s.episode_seen_count,1), g.max_eps)
            FROM (SELECT MIN(first_seen) AS min_first, MAX(last_seen) AS max_last,
                         MAX(coalesce(count,1)) AS max_count,
                         MAX(coalesce(seen_count,1)) AS max_seen,
                         MAX(coalesce(episode_count,1)) AS max_ep,
                         MAX(coalesce(episode_seen_count,1)) AS max_eps
                    FROM brain_findings WHERE id = ANY(%s)) g
            WHERE s.id = %s
        """, (ids, survivor))
        cur.execute("DELETE FROM brain_findings WHERE id = ANY(%s)", (losers,))
        deleted += cur.rowcount

    print(f"{'would delete' if DRY else 'deleted'} {deleted} duplicate rows "
          f"across {len(groups)} groups")
    if not DRY:
        c.commit()
        cur.execute(INDEX_SQL)
        c.commit()
        print("unique index ensured: brain_findings_issue_url_uniq")
    else:
        print(f"dry-run: skipped merge/delete + index ({INDEX_SQL})")
    c.close()


if __name__ == "__main__":
    main()
