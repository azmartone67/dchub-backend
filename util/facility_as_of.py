"""The data vintage of one facility record, for its provenance block (2026-09-26).

/api/v1/facility/<id> and /<slug> stamped a provenance block with no `as_of`,
so the MCP fetch tool — the ChatGPT connector's record view — had no date to
cite: fetch on Level 3 Ashburn (8484) showed none in the OpenAI demo.
routes/provenance.provenance_block already accepts `as_of` and normalises it;
the routes just never passed one.

`discovered_facilities.last_updated` is written by every discovery upsert
(routes/discovery_routes.py, `last_updated = EXCLUDED.last_updated`), so it is
when this record was last refreshed from its sources. It is TEXT; the value is
returned as stored and provenance_block._iso normalises it (an unparseable one
is dropped there). Any failure returns None and the block simply has no as_of,
exactly as before.
"""
import logging

logger = logging.getLogger(__name__)


def record_as_of(cur, facility_id):
    """last_updated of discovered_facilities.id = facility_id, or None."""
    try:
        fid = int(facility_id)
    except (TypeError, ValueError):
        return None
    try:
        cur.execute("SELECT last_updated FROM discovered_facilities WHERE id = %s LIMIT 1", (fid,))
        row = cur.fetchone()
        return row[0] if row and row[0] not in (None, '') else None
    except Exception as e:
        logger.info("record_as_of(%s) unavailable: %s", fid, e)
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None
