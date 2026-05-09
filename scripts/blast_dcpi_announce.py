#!/usr/bin/env python3
"""One-shot DCPI launch announcement email to all dev-key holders.
Run once: python3 scripts/blast_dcpi_announce.py"""
import os, json, urllib.request

API = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud")
ADMIN = os.environ.get("DCHUB_ADMIN_KEY", "")

if not ADMIN:
    print("ERROR: DCHUB_ADMIN_KEY env var required")
    raise SystemExit(1)

SUBJECT = "DCPI is live — the Data Center Power Index nobody else publishes"

HTML = """<div style="font-family:-apple-system,system-ui,sans-serif;max-width:580px;margin:0 auto;color:#222;line-height:1.6;">
<h2 style="margin:0 0 0.4rem;">DCPI is live.</h2>
<p style="color:#555;margin:0 0 1.5rem;">The Data Center Power Index. Daily-updated power availability scores across 30 U.S. data center markets. Free for citation.</p>

<p>You signed up for a DC Hub dev key in the last few weeks. You're seeing this because you're one of the first 5 people who get DCPI before LinkedIn does.</p>

<h3 style="margin-top:1.5rem;">Today's top 3 BUILD-verdict markets:</h3>
<ul>
<li><strong>Cheyenne, WY</strong> — Excess Power 69.5 / Constraint 22.5 / ~11mo to power</li>
<li><strong>Rural SPP, KS</strong> — Excess Power 67.2 / Constraint 22.5 / ~11mo to power</li>
<li><strong>Williston, ND</strong> — Excess Power 65.0 / Constraint 17.4 / ~8mo to power</li>
</ul>

<p>Bottom of the Constraint list: Northern Virginia (60-month queue), Phoenix (42-month queue), Atlanta (36-month queue). The names the incumbents won't recommend.</p>

<p style="margin:2rem 0;">
<a href="https://dchub.cloud/dcpi" style="background:linear-gradient(135deg,#6366f1,#a855f7);color:white;padding:0.7rem 1.4rem;border-radius:6px;text-decoration:none;font-weight:700;">Open DCPI →</a>
</p>

<h3>What you can do:</h3>
<ul>
<li>Ask the chat box any natural-language question about U.S. markets</li>
<li>Drill into any market for top opportunities + risks</li>
<li>Share <a href="https://dchub.cloud/dcpi/williston-nd">a market URL</a> on LinkedIn — the OG card auto-renders</li>
<li>Cite us in your next memo — every research page has a citation block</li>
</ul>

<p>If you want the daily morning brief, the subscribe form is on the DCPI page. Free, Mon–Fri at 14:00 UTC.</p>

<p>Built so you can stop paying CBRE for slide decks. Tell me what we got wrong: jonathan@dchub.cloud</p>

<p style="margin-top:2rem;color:#888;font-size:0.85rem;">— Jonathan, DC Hub</p>
</div>"""

# Get all subscribers + dev key holders
import urllib.request as _ur
req = _ur.Request(API + "/api/v1/outreach/recent",
                  headers={"Accept": "application/json",
                           "User-Agent": "Mozilla/5.0 (DCHub-Blast/1.0)"})
with _ur.urlopen(req, timeout=15) as r:
    data = json.loads(r.read())

# Pull emails
emails = set()
rows = data if isinstance(data, list) else data.get("rows", [])
for r in rows:
    em = r.get("email") or r.get("email_masked", "")
    if em and "@" in em and not em.startswith("***"):
        emails.add(em)

print(f"recipients: {len(emails)}")
for em in sorted(emails):
    body = json.dumps({
        "to": [em],
        "from": "DC Hub <jonathan@dchub.cloud>",
        "subject": SUBJECT,
        "html": HTML,
    }).encode("utf-8")
    rq = _ur.Request("https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ.get("RESEND_API_KEY", ""),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (DCHub/1.0)",
        }, method="POST")
    try:
        with _ur.urlopen(rq, timeout=15) as resp:
            print(f"  [{resp.status}] {em}")
    except Exception as e:
        print(f"  [err] {em}: {e}")
