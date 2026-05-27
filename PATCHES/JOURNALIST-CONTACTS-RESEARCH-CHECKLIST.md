# Journalist Contacts Research — 30-Minute Sprint

The press_contacts table is seeded with 20 outlets. Email field is blank
on all 20 because I never invent contact info. This is the 30-min sprint
to populate the 5-10 that matter, then fire one bulk-upsert curl.

## Step 1 — Find the right name (3 min each, 20 min total)

For each outlet below, do this Google search pattern:

```
"<outlet-name>" "data center" reporter email
```

OR check the outlet's masthead. OR pick a recent article on data centers
and find the byline.

### Top-5 to populate first (highest priority, highest ROI)

| Outlet | Beat | Where to look | What to grab |
|---|---|---|---|
| **Datacenter Dynamics (DCD)** | data_centers | datacenterdynamics.com/about-us | Sebastian Moss or current US editor |
| **Bisnow Data Center** | data_centers | bisnow.com/data-center byline | Mark Faithfull / Ethan Rothstein |
| **Bloomberg — Infrastructure** | ai_infra | Bloomberg DC coverage recent articles | infra desk byline |
| **The Information** | ai_infra | theinformation.com search "data center" | Aaron Holmes / Anissa Gardizy |
| **WSJ — Tech / Heard on the Street** | ai_infra | WSJ AI infra column bylines | Pro Crawford / Asa Fitch |

### Next-5 if you have time

| Outlet | Beat | Likely byline pattern |
|---|---|---|
| **Reuters — M&A / Infrastructure** | m_and_a | Greg Roumeliotis / Anirban Sen |
| **Axios Pro — Climate Deals** | energy_grid | Climate Pro newsletter masthead |
| **Heatmap News** | energy_grid | Bylines on grid + DC pieces |
| **Stratechery (Ben Thompson)** | ai_infra | One person — `ben@stratechery.com` |
| **Latitude Media (Stephen Lacey)** | energy_grid | One person — `stephen@latitudemedia.com` |

## Step 2 — Fire the bulk-upsert (one curl)

Once you have 5+ emails, replace the placeholders and run:

```bash
KEY="83a984cb494aa8ebb4b4032239f8bae5a3c1d91873df09d67b40bedbfa427093"

curl -s -X POST -H "X-Admin-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "contacts": [
      {"outlet":"Datacenter Dynamics (DCD)",
       "contact_name":"Sebastian Moss",
       "contact_email":"sebastian.moss@datacenterdynamics.com",
       "beat":"data_centers","priority":9,"pitch_style":"data_first"},

      {"outlet":"Bisnow Data Center",
       "contact_name":"<their name>",
       "contact_email":"<their email>",
       "beat":"data_centers","priority":9,"pitch_style":"narrative"},

      {"outlet":"Bloomberg — Infrastructure / Equity Research",
       "contact_name":"<their name>",
       "contact_email":"<their email>",
       "beat":"ai_infra","priority":10,"pitch_style":"data_first"},

      {"outlet":"The Information",
       "contact_name":"Aaron Holmes",
       "contact_email":"<their email>",
       "beat":"ai_infra","priority":10,"pitch_style":"exclusive_ok"},

      {"outlet":"WSJ — Tech / Heard on the Street",
       "contact_name":"<their name>",
       "contact_email":"<their email>",
       "beat":"ai_infra","priority":10,"pitch_style":"exclusive_ok"}
    ]
  }' \
  "https://dchub.cloud/api/v1/admin/press-outreach/contacts/bulk-upsert" \
  | python3 -m json.tool
```

## Step 3 — Regenerate drafts targeting the new contacts

```bash
# Clear old "Hi there / no email" drafts
curl -s -X POST -H "X-Admin-Key: $KEY" \
  "https://dchub.cloud/api/v1/admin/press-outreach/drafts/clear-pending"

# Regenerate fresh drafts targeting the populated contacts
curl -s -X POST -H "X-Admin-Key: $KEY" \
  "https://dchub.cloud/api/v1/admin/press-outreach/generate-drafts?top=3&min_priority=8" \
  | python3 -m json.tool
```

## Step 4 — Approve from dashboard (10 min)

```bash
open "https://dchub-backend-production.up.railway.app/admin/partnerships/review"
```

Review each draft. Approve = Resend fires. Reject = audit-kept.

## A reasonable cold-pitch ratio to expect

- **5 pitches → 1-2 replies → 0-1 stories** is normal for cold press
  outreach with no prior relationship.
- **Higher hit rate** when the angle is timely (DCPI shift, new milestone)
  or when you've been quoted in their previous article.
- **The Bloomberg / The Information replies are the goldmine** — even one
  story from either gets you in the citation graph of every AI agent
  scraping their archives for the next decade.

## What NOT to do

- Don't BCC all 5 on one email — that's a press release blast and
  journalists ignore those.
- Don't fire pitch + follow-up + follow-up + follow-up. The drafts
  already include "happy to chat" — let them reply on their timeline.
- Don't pitch the same angle to the same journalist twice within 14
  days. The dedupe is built into /generate-drafts; let it work.
