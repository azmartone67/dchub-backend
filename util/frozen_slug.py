"""Resolve a FROZEN facility slug for the API routes (2026-09-25).

The list endpoints hand out each row's frozen `canonical_slug`
(routes/facility_slug_freeze.py): set once, never rewritten, so a later
provider or name clean-up leaves its hash8 tail pointing at the OLD
provider|name. The profile page resolves it by exact `canonical_slug` match,
but /api/v1/facility/<slug>, /api/v1/facilities/<slug> and
/api/v1/facilities/slug/<slug> only recomputed MD5(provider|name)[:8] from the
live row, so every renamed row 404'd on the API while its page answered 200.

Measured 2026-09-25, id 8484 "Level 3 Ashburn" / provider "Lumen Technologies":
the list slug is lumen-technologies-level-3-ashburn-23a0d3a2, but
stable_hash8("Lumen Technologies", "Level 3 Ashburn") is 4a3f7afd.
/facilities/<slug> answered 200; the three API routes answered 404.

frozen_slug_hash8 returns the hash8 of the CURRENT provider|name of the row
that owns the slug, with the same owner ordering the page uses, so each route
keeps its own query (twins, carriers, gating) and just looks up the right
building. An alias (facility_slug_aliases.old_slug) resolves one hop to its
canonical slug first. Any failure returns None and the route falls back to
the slug's own hash8, exactly as before.
"""
import logging

from routes.facility_slug import stable_hash8
from routes.facility_slug_freeze import SLUG_OWNER_ORDER_SQL

logger = logging.getLogger(__name__)


def _owner_hash8(cur, slug):
    cur.execute(
        "SELECT provider, name FROM discovered_facilities "
        "WHERE canonical_slug = %s ORDER BY " + SLUG_OWNER_ORDER_SQL + " LIMIT 1",
        (slug,))
    row = cur.fetchone()
    return stable_hash8(row[0], row[1]) if row else None


def frozen_slug_hash8(cur, slug):
    """hash8 of the live provider|name behind a frozen slug or alias, or None."""
    slug = str(slug or '').strip().rstrip('/')
    if not slug or slug.isdigit():
        return None
    try:
        h = _owner_hash8(cur, slug)
        if h:
            return h
        cur.execute("SELECT canonical_slug FROM facility_slug_aliases WHERE old_slug = %s LIMIT 1",
                    (slug,))
        row = cur.fetchone()
        if row and row[0] and row[0] != slug:
            return _owner_hash8(cur, row[0])
    except Exception as e:  # missing column/table, replica hiccup: fall back
        logger.info("frozen_slug_hash8(%s) fell back: %s", slug[:80], e)
        try:
            cur.connection.rollback()
        except Exception:
            pass
    return None
