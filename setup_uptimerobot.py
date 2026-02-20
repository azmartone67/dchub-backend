#!/usr/bin/env python3
"""
UptimeRobot Monitor Setup for DC Hub
Run in Replit shell: python3 setup_uptimerobot.py
"""

import requests
import json
import time

API_KEY = "u3231139-c4a597d3c12d26c248e91414"
BASE_URL = "https://api.uptimerobot.com/v2"

MONITORS = [
    {
        "friendly_name": "DC Hub - Backend Health",
        "url": "https://dchub.cloud/api/health",
        "type": 1,  # HTTP(s)
        "interval": 300,  # 5 minutes
        "keyword_type": 1,  # exists
        "keyword_value": "healthy",
        "alert_contacts": ""  # will use default
    },
    {
        "friendly_name": "DC Hub - News API",
        "url": "https://dchub.cloud/api/news/live?limit=1",
        "type": 1,
        "interval": 300,
        "keyword_type": 1,
        "keyword_value": "articles",
        "alert_contacts": ""
    },
    {
        "friendly_name": "DC Hub - Homepage",
        "url": "https://dchub.cloud",
        "type": 1,
        "interval": 300,
        "keyword_type": 1,
        "keyword_value": "DC Hub",
        "alert_contacts": ""
    },
    {
        "friendly_name": "DC Hub - Facilities API",
        "url": "https://dchub.cloud/api/agent/facilities?limit=1",
        "type": 1,
        "interval": 300,
        "keyword_type": 1,
        "keyword_value": "facilities",
        "alert_contacts": ""
    },
    {
        "friendly_name": "DC Hub - Replit Direct",
        "url": "https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/health",
        "type": 1,
        "interval": 300,
        "keyword_type": 1,
        "keyword_value": "healthy",
        "alert_contacts": ""
    },
]

def get_alert_contacts():
    """Get existing alert contacts to attach to monitors"""
    r = requests.post(f"{BASE_URL}/getAlertContacts", json={
        "api_key": API_KEY,
        "format": "json"
    })
    data = r.json()
    if data.get("stat") == "ok" and data.get("alert_contacts"):
        contacts = data["alert_contacts"]
        # Format: id_threshold_recurrence-id_threshold_recurrence
        contact_str = "-".join([f"{c['id']}_0_0" for c in contacts])
        print(f"  Found {len(contacts)} alert contact(s)")
        for c in contacts:
            print(f"    → {c.get('friendly_name', 'Unknown')} ({c.get('type_friendly', '?')})")
        return contact_str
    return ""

def get_existing_monitors():
    """Get list of existing monitors to avoid duplicates"""
    r = requests.post(f"{BASE_URL}/getMonitors", json={
        "api_key": API_KEY,
        "format": "json"
    })
    data = r.json()
    if data.get("stat") == "ok":
        monitors = data.get("monitors", [])
        print(f"\n  Existing monitors: {len(monitors)}")
        for m in monitors:
            status = "✅ UP" if m.get("status") == 2 else "🔴 DOWN" if m.get("status") == 9 else "⏸️ PAUSED"
            print(f"    {status} {m['friendly_name']} → {m['url']}")
        return {m["url"]: m for m in monitors}
    return {}

def create_monitor(monitor, alert_contacts, existing):
    """Create a single monitor if it doesn't already exist"""
    if monitor["url"] in existing:
        print(f"  ⏭️  Already exists: {monitor['friendly_name']}")
        return True
    
    payload = {
        "api_key": API_KEY,
        "format": "json",
        "friendly_name": monitor["friendly_name"],
        "url": monitor["url"],
        "type": monitor["type"],
        "interval": monitor["interval"],
    }
    
    # Add keyword monitoring if specified
    if monitor.get("keyword_value"):
        payload["keyword_type"] = monitor["keyword_type"]
        payload["keyword_value"] = monitor["keyword_value"]
    
    # Attach alert contacts
    if alert_contacts:
        payload["alert_contacts"] = alert_contacts
    
    r = requests.post(f"{BASE_URL}/newMonitor", json=payload)
    data = r.json()
    
    if data.get("stat") == "ok":
        print(f"  ✅ Created: {monitor['friendly_name']}")
        return True
    else:
        error = data.get("error", {}).get("message", "Unknown error")
        print(f"  ❌ Failed: {monitor['friendly_name']} — {error}")
        return False

def main():
    print("=" * 50)
    print("DC Hub — UptimeRobot Monitor Setup")
    print("=" * 50)
    
    # Get alert contacts
    print("\n📧 Checking alert contacts...")
    alert_contacts = get_alert_contacts()
    
    if not alert_contacts:
        print("  ⚠️  No alert contacts found!")
        print("  Go to UptimeRobot → My Settings → Alert Contacts")
        print("  Add your email/SMS/Slack, then re-run this script.")
        print("  Continuing without alerts for now...\n")
    
    # Get existing monitors
    print("\n📊 Checking existing monitors...")
    existing = get_existing_monitors()
    
    # Create new monitors
    print(f"\n🔧 Setting up {len(MONITORS)} monitors...")
    print("-" * 50)
    
    created = 0
    for monitor in MONITORS:
        if create_monitor(monitor, alert_contacts, existing):
            created += 1
        time.sleep(1)  # Rate limit
    
    print("-" * 50)
    print(f"\n✅ Done! {created}/{len(MONITORS)} monitors active")
    print("\n📱 Dashboard: https://dashboard.uptimerobot.com/")
    print("   Check interval: Every 5 minutes")
    print("   Keyword monitoring: Enabled (checks for actual data, not just HTTP 200)")

if __name__ == "__main__":
    main()
