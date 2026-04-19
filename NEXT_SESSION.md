# DC Hub — Next Session Priorities
_Generated: 2026-03-18_

## 1. BuildingPermit.io API key (15 min)
- Sign up at buildingpermit.io (free tier available)
- Add `BUILDING_PERMIT_API_KEY` to Railway environment variables
- Run smoke test: `PERMIT_MAX_FACILITIES=10 python3 ~/workspace/permit_scraper.py`
- Expected: permits found jumps from 0 to many for VA/TX/GA facilities

## 2. LinkedIn post (20 min)
- Announce operational date data launch
- Link to dchub.cloud/research
- Tag: data center, infrastructure, AI, MCP, research data

## 3. refresh_transactions() DB bug (30 min)
- Bug introduced Mar 17 marathon session
- `refresh_transactions()` has a DB-level bug (not yet diagnosed)
- Deals refresh gate was bypassed as workaround
- Fix: find the bug in deals_routes.py or main.py, test with psql

## 4. City/state backfill — 536 facilities (45 min)
- 536 facilities have permit_date but empty city/state
- These matched via Equinix metro name (e.g. "silicon valley", "ashburn")
- Strategy: JOIN discovered_facilities to get city/state, backfill facilities table
- SQL:
  UPDATE facilities f
  SET city = df.city, state = df.state
  FROM discovered_facilities df
  WHERE df.merged_facility_id = f.id
    AND f.permit_date IS NOT NULL
    AND (f.city IS NULL OR f.city = '')
    AND df.city IS NOT NULL AND df.city != '';

## 5. Phase 3 FOIA tracker (2 hrs)
- Build foia_tracker.py
- Target: MuckRock API + direct municipal FOIA portals
- Priority jurisdictions: Loudoun County VA, Dallas County TX, Maricopa County AZ
- Schema: facility_permits.source = 'foia'
- Confidence: 0.90-1.00 (highest tier)

## Quick wins to batch in:
- Wire permit-coverage count to homepage stat bar (live JS fetch)
- Add `permit_date` filter to /api/facilities search endpoint
- Glama ownership claim resolution (PR #1836 or Discord)
- Smithery bypass verification

## Current state:
- 567 facilities with permit_date (18.2% US coverage)
- 1,766 permit records in facility_permits
- permit_enrichment_queue: 0 (will grow as new facilities approved)
- Scheduler: permit_scraper weekly Sun 02:00, sec_parser monthly 1st 03:00
