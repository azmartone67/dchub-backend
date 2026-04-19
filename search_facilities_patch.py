"""
search_facilities_patch.py — Dual-Table Search Patch for DC Hub

PROBLEM:
    search_facilities() in main.py (line ~8106) queries ONLY discovered_facilities.
    Manually seeded facilities in the `facilities` table are invisible to the API/MCP.

SOLUTION:
    Replace the single-table SELECT with a UNION that searches both tables,
    deduplicating by name+provider so facilities in both tables appear only once.

HOW TO APPLY:
    1. Open main.py in Railway shell
    2. Find the search_facilities() function (~line 8016)
    3. Find the query block (~line 8100-8115) that looks like:

        c.execute(f\"\"\"
            SELECT * FROM discovered_facilities
            {where}
            {RAILWAY_EXCLUSION...}
            ORDER BY confidence_score DESC, power_mw DESC
            LIMIT %s OFFSET %s
        \"\"\", params)

    4. Replace that entire c.execute(...) block with the function below.
    5. git add main.py && git commit -m "search_facilities: UNION both tables" && git push

ALTERNATIVE (if UNION causes type mismatches):
    Just keep seeding into discovered_facilities directly (which we're already doing).
    The UNION fix is the right long-term answer but the workaround is fine for now.
"""


def apply_search_patch():
    """
    This is the replacement query logic for the search_facilities() function.
    Copy the c.execute() block below into main.py, replacing the existing one.
    """
    pass  # This file is documentation + reference, not directly imported


# ─────────────────────────────────────────────────────────
# REPLACEMENT QUERY BLOCK
# ─────────────────────────────────────────────────────────
#
# Replace the existing c.execute() in search_facilities() with:
#
#     # Search both discovered_facilities and facilities tables
#     # Facilities-only rows (not in discovered) are included via UNION
#     combined_query = f"""
#         SELECT * FROM (
#             SELECT
#                 id::text as id, name, provider, address, city, state, country,
#                 latitude, longitude, power_mw, sqft, status,
#                 source, source_url, confidence_score,
#                 discovered_at, facility_type, notes,
#                 operational_year, is_duplicate, market,
#                 source_id, raw_data, merged_at, merged_facility_id,
#                 investment_usd, acreage, expected_completion,
#                 project_name, utility_provider
#             FROM discovered_facilities
#
#             UNION ALL
#
#             SELECT
#                 f.id::text as id, f.name, f.provider, f.address, f.city, f.state, f.country,
#                 f.latitude, f.longitude, f.power_mw, f.sqft::text, f.status,
#                 f.source, f.source_url, COALESCE(f.confidence_score, f.confidence)::real,
#                 f.first_seen as discovered_at, NULL as facility_type, NULL as notes,
#                 NULL as operational_year, 0 as is_duplicate, f.region as market,
#                 f.source_id, f.raw_data, NULL as merged_at, NULL as merged_facility_id,
#                 NULL::bigint as investment_usd, NULL::integer as acreage,
#                 NULL as expected_completion,
#                 NULL as project_name, NULL as utility_provider
#             FROM facilities f
#             WHERE NOT EXISTS (
#                 SELECT 1 FROM discovered_facilities df
#                 WHERE LOWER(df.name) = LOWER(f.name)
#                   AND LOWER(COALESCE(df.provider,'')) = LOWER(COALESCE(f.provider,''))
#             )
#         ) combined
#         {where}
#         ORDER BY confidence_score DESC NULLS LAST, power_mw DESC NULLS LAST
#         LIMIT %s OFFSET %s
#     """
#     c.execute(combined_query, params)
#
# ─────────────────────────────────────────────────────────
# NOTE: If the UNION causes column count/type mismatches,
# check that discovered_facilities has the new columns from
# 05_pipeline_tracking.sql (investment_usd, acreage, etc.).
# If not, remove those columns from the discovered_facilities
# side of the UNION as well.
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# LONG-TERM RECOMMENDATION
# ─────────────────────────────────────────────────────────
#
# Unify into ONE table. Options:
#
# A) Make discovered_facilities the single source of truth.
#    Add a 'verified' boolean column. The current facilities table
#    becomes a view: CREATE VIEW facilities_verified AS
#    SELECT * FROM discovered_facilities WHERE verified = true;
#
# B) Make facilities the single source of truth.
#    Merge all discovered_facilities into facilities.
#    Add a 'discovery_source' column to track provenance.
#    The discovery pipeline inserts directly into facilities
#    with verified=false, and a QA step promotes them.
#
# Either way, the API should query exactly one table.
# ─────────────────────────────────────────────────────────
