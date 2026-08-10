#!/usr/bin/env python3
"""
DC Hub Announcement & LinkedIn Publisher
========================================
Automates posting press releases to dchub.cloud/press
and announcements to the DC Hub LinkedIn company page.

Usage:
    python post_announcement.py

Requirements:
    pip install requests python-dotenv

Setup:
    Create a .env file with your credentials (see .env.example below).
"""

import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

DCHUB_BASE_URL   = "https://dchub.cloud"
DCHUB_API_KEY    = os.getenv("DCHUB_API_KEY")          # From DC Hub Dashboard → API Keys
LINKEDIN_TOKEN   = os.getenv("LINKEDIN_ACCESS_TOKEN")  # From LinkedIn Developer App
LINKEDIN_COMPANY = os.getenv("LINKEDIN_COMPANY_ID", "110894959")  # DC Hub company ID


# ── Press Release Data ───────────────────────────────────────────────────────
# Edit this section for each new announcement

PRESS_RELEASE = {
    "title": ("DC Hub Extends Its Live Grid Scoreboard to Five Continents as Japan, "
              "South Korea and Brazil Join the Real-Time Renewable-Share Ranking"),
    "slug": "2026-08-10-global-grid-scoreboard-japan-korea-brazil",
    "category": "Press Release",
    "date": "2026-08-10",
    "subheadline": (
        "Japan (OCCTO), South Korea (KPX) and Brazil's national grid (ONS) now rank "
        "beside the US ISOs, the European bidding zones, Great Britain and Taiwan on "
        "one keyless, real-time renewable-share scale — bringing LatAm and APAC grid "
        "comparison into the same free tool data-center siting teams already use for "
        "PJM and ERCOT."
    ),
    "body": """
DC Hub (dchub.cloud), the neutral live-data layer for data-center infrastructure, today
extended its real-time global grid scoreboard to five continents. Japan (via the OCCTO
area TSO feeds), South Korea (via KPX) and Brazil's national interconnected system (via
ONS) now rank side by side with the nine US grid operators, the European bidding zones,
Great Britain (NESO) and Taiwan (Taipower) — every grid scored on the same renewable-share
definition, so the ordering is apples-to-apples across the world.

The scoreboard answers one question for anyone siting compute: which grid, right now, is
greenest — or most gas-reliant — for a data center? It ranks grids from seven independent
upstream feeds, each row carrying its own freshness stamp. Brazil's SIN enters as one of
the greenest large grids on the board, its hydro-heavy mix putting renewable share near the
top; Korea and Japan enter at the thermal-heavy end — exactly the spread a siting team needs
to see. Australia (AEMO) and Singapore (EMA) are published as live-partial, listed honestly
as unranked because their public feeds do not carry a full fuel split.

Crucially, the whole scoreboard is keyless. A developer, an analyst, or an autonomous AI
agent can call get_grid_scoreboard — or the REST endpoint behind it — with no account and no
key, at free-tier depth, and compare grids for LatAm and APAC siting the same way they
already compare US ISOs. Every row is machine-readable and every figure is cited: the data
is published under CC-BY-4.0, and each grid reports its own mix period and age so a reading
is never narrated as "right now" when the upstream feed is hours behind.

The addition continues DC Hub's core thesis — that the physical infrastructure behind AI
should be queried live and cited, not guessed at from stale training data or gated behind
analyst PDFs. The grid scoreboard sits alongside DC Hub's 17,000+ tracked facilities, 300+
DCPI-scored markets, and 320,000+ mapped power, grid, gas and fiber assets.

Try it live at dchub.cloud/playground, or call get_grid_scoreboard from any MCP client.
""".strip(),
    "meta_description": (
        "Japan (OCCTO), South Korea (KPX) and Brazil (ONS) join DC Hub's keyless, real-time "
        "grid scoreboard — now ranking renewable share across five continents beside the US "
        "ISOs, EU zones, Great Britain and Taiwan."
    ),
}

LINKEDIN_POST = """🌎 The DC Hub grid scoreboard now spans five continents.

Japan (OCCTO), South Korea (KPX) and Brazil's national grid (ONS) just joined our live, keyless renewable-share ranking — now side by side with the US ISOs, the EU bidding zones, Great Britain and Taiwan.

One real-time scale. One definition of renewable share. Apples-to-apples from ERCOT to São Paulo to Seoul.

Compare grids for LatAm and APAC siting the same way you compare US ISOs — no account, no key, free tier. Every figure cited (CC-BY-4.0), and every row is stamped with its own freshness, so a reading is never dressed up as "right now" when the upstream feed is hours behind.

Brazil's hydro-heavy grid enters near the top of the renewable ranking; Korea and Japan anchor the thermal-heavy end — exactly the spread a siting team needs.

👉 Try it live: dchub.cloud/playground

#DataCenters #Grid #Renewables #AIInfrastructure #DCPI #SiteSelection"""


# ── DC Hub Press Release Poster ──────────────────────────────────────────────

def post_to_dchub(release: dict) -> bool:
    """
    POST a new press release to the DC Hub backend.

    This calls the DC Hub admin API endpoint that you'll add to your Flask app.
    See the Flask snippet at the bottom of this file.
    """
    if not DCHUB_API_KEY:
        print("❌  DCHUB_API_KEY not set in .env")
        return False

    endpoint = f"{DCHUB_BASE_URL}/api/admin/press-releases"
    headers  = {
        "Authorization": f"Bearer {DCHUB_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "title":            release["title"],
        "slug":             release["slug"],
        "category":         release["category"],
        "date":             release["date"],
        "subheadline":      release["subheadline"],
        "body":             release["body"],
        "meta_description": release["meta_description"],
        "published":        True,
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            print(f"✅  DC Hub press release posted → {DCHUB_BASE_URL}/press/{release['slug']}")
            print(f"    ID: {data.get('id', 'n/a')}")
            return True
        else:
            print(f"❌  DC Hub API error {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"❌  DC Hub request failed: {e}")
        return False


# ── LinkedIn Company Page Poster ─────────────────────────────────────────────

def post_to_linkedin(text: str, article_url: str = None) -> bool:
    endpoint = "https://dchub-backend-production.up.railway.app/api/linkedin/post"
    headers  = {"Content-Type": "application/json", "X-Admin-Key": os.getenv("DCHUB_ADMIN_KEY", "")}
    payload  = {"content": text}
    if article_url:
        payload["link_url"] = article_url
    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            print(f"✅  LinkedIn posted → {resp.json().get('post_urn','ok')}")
            return True
        print(f"❌  LinkedIn error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"❌  LinkedIn failed: {e}")
        return False

    endpoint = "https://dchub-backend-production.up.railway.app/api/linkedin/post"
    headers  = {
        "Authorization":   f"Bearer {LINKEDIN_TOKEN}",
        "Content-Type":    "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    author = f"urn:li:organization:{LINKEDIN_COMPANY}"

    if article_url:
        # Rich post with article preview
        payload = {
            "author":         author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "ARTICLE",
                    "media": [{
                        "status":      "READY",
                        "originalUrl": article_url,
                    }],
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
    else:
        # Plain text post
        payload = {
            "author":         author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary":    {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            data   = resp.json()
            post_id = data.get("id", "n/a")
            print(f"✅  LinkedIn post published → https://www.linkedin.com/feed/update/{post_id}")
            return True
        else:
            print(f"❌  LinkedIn API error {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"❌  LinkedIn request failed: {e}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n🚀  DC Hub Announcement Publisher")
    print("=" * 45)
    print(f"   Title : {PRESS_RELEASE['title'][:60]}...")
    print(f"   Date  : {PRESS_RELEASE['date']}")
    print(f"   Slug  : {PRESS_RELEASE['slug']}")
    print()

    # 1. Post to DC Hub /press
    print("📄  Posting to DC Hub press section...")
    dchub_ok = post_to_dchub(PRESS_RELEASE)

    print()

    # 2. Post to LinkedIn
    print("🔗  Posting to LinkedIn company page...")
    article_url = f"{DCHUB_BASE_URL}/news/{PRESS_RELEASE['slug']}"
    linkedin_ok = post_to_linkedin(LINKEDIN_POST, article_url=article_url)

    print()
    print("─" * 45)
    status = "✅  All done!" if (dchub_ok and linkedin_ok) else "⚠️   Completed with errors — check above."
    print(status)


if __name__ == "__main__":
    main()
