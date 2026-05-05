#!/usr/bin/env python3
"""
DC Hub Press Release Publisher
Global Infrastructure: 1.29M Records Live
"""
import os, subprocess, json, sys

# ── Press Release Content ────────────────────────────────────────────────────

TITLE       = "DC Hub Global Infrastructure: 1.29 Million Records Live Across US, EMEA, and APAC"
SLUG        = "dc-hub-global-infrastructure-1-29m-records-live"
CATEGORY    = "INFRASTRUCTURE"
DATE        = "2026-04-09"
SUBHEADLINE = "Complete global view of transmission lines, substations, and gas pipelines now unified in Neon with PostGIS geometry — covering three continents and refreshed quarterly"
META        = "DC Hub has unified 1.29 million energy infrastructure records across the US, EMEA, and APAC into a single queryable global_infrastructure view powered by Neon PostgreSQL with PostGIS geometry."

BODY = """DC Hub has launched its global_infrastructure unified database view — a single, production-ready dataset covering more than 1.29 million energy infrastructure records across the United States, Europe, the Middle East and Africa (EMEA), and the Asia-Pacific region (APAC). The dataset consolidates transmission lines, substations, and natural gas pipelines into one queryable layer, giving AI platforms, developers, and enterprise customers access to the most comprehensive global power infrastructure dataset available today.

The United States layer provides voltage-level detail, operator attribution, and state-level classification across 52,244 transmission lines, 79,754 substations, and 917 gas pipelines. Site selectors and grid analysts can query transmission access, voltage class, and operator ownership across all major ISO territories in milliseconds.

The EMEA layer — DC Hub's largest regional dataset to date — spans 286,907 transmission lines, 563,727 substations, and 32,077 gas pipelines across Europe, the Middle East, and Africa, with PostGIS geometry enabling precise proximity queries across dozens of countries. The APAC layer extends coverage to 188,185 transmission lines, 80,950 substations, and 3,513 gas pipelines across fast-growing markets including Japan, South Korea, Australia, and Southeast Asia.

All three regions are accessible through a single global_infrastructure view, built on Neon PostgreSQL with PostGIS geometry. The first quarterly refresh is scheduled for July 1, 2026.

Quote from Tony Bishop, Strategic Advisor, DC Hub:
"Global infrastructure intelligence has been the missing layer for hyperscale site selection. When you're evaluating a 200MW campus across three continents, you need the grid story everywhere at once — not three separate datasets that don't talk to each other. The global_infrastructure view changes that. It's the foundation for the next generation of data center decision-making."

The global_infrastructure view is live now and available to DC Hub API subscribers at all tier levels, accessible through the Land & Power Map and Site Planner tools at dchub.cloud."""

LINKEDIN_TEXT = """DC Hub just launched its global infrastructure database — 1.29 million energy infrastructure records across the US, EMEA, and APAC in a single unified view.

527,336 transmission lines. 724,431 substations. 36,507 gas pipelines. One query.

Built on Neon PostgreSQL with PostGIS geometry. Quarterly refresh starting July 1.

This is the foundation for global data center site selection at scale. Available now to all DC Hub API subscribers.

#DCHub #DataCenter #Infrastructure #GridIntelligence #SiteSelection #AI"""

# ── Step 1: Post to DC Hub ───────────────────────────────────────────────────

print("Step 1 — Publishing to DC Hub...")

payload = json.dumps({
    "title": TITLE,
    "slug": SLUG,
    "category": CATEGORY,
    "date": DATE,
    "subheadline": SUBHEADLINE,
    "body": BODY,
    "meta_description": META,
    "published": True
})

result = subprocess.run([
    "curl", "-s", "-w", "\n%{http_code}",
    "-X", "POST", "https://dchub.cloud/api/admin/press-releases",
    "-H", "Authorization: Bearer dchub-admin-secret-2026",
    "-H", "Content-Type: application/json",
    "-d", payload
], capture_output=True, text=True, timeout=30)

lines = result.stdout.strip().split("\n")
http_code = lines[-1]
body_resp = "\n".join(lines[:-1])

print(f"  HTTP {http_code}")
print(f"  Response: {body_resp[:300]}")

if http_code not in ("200", "201"):
    print(f"\n❌ DC Hub publish failed (HTTP {http_code}). See response above.")
    print("\nFull curl error:", result.stderr[:300] if result.stderr else "none")
    sys.exit(1)

print(f"  ✅ Press release live at: https://dchub.cloud/news/{SLUG}")

# ── Step 2: Post to LinkedIn ─────────────────────────────────────────────────

print("\nStep 2 — Posting to LinkedIn...")

li_payload = json.dumps({
    "content": LINKEDIN_TEXT,
    "link_url": f"https://dchub.cloud/news/{SLUG}"
})

li_result = subprocess.run([
    "curl", "-s", "-w", "\n%{http_code}",
    "-X", "POST", "https://dchub-backend-production.up.railway.app/api/linkedin/post",
    "-H", f"X-Admin-Key: {os.environ.get('DCHUB_ADMIN_KEY', '')}",
    "-H", "Content-Type: application/json",
    "-d", li_payload
], capture_output=True, text=True, timeout=30)

li_lines = li_result.stdout.strip().split("\n")
li_code = li_lines[-1]
li_body = "\n".join(li_lines[:-1])

print(f"  HTTP {li_code}")
print(f"  Response: {li_body[:300]}")

if li_code in ("200", "201"):
    print("  ✅ LinkedIn post published.")
else:
    print(f"  ⚠️  LinkedIn post may have failed (HTTP {li_code}) — check manually.")

print(f"""
─────────────────────────────────────────
✅ DC Hub:   https://dchub.cloud/news/{SLUG}
📋 Title:    {TITLE}
📅 Date:     {DATE}
─────────────────────────────────────────
""")
