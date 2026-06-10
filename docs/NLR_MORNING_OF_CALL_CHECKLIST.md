# DC Hub × NLR — Morning-of-Call Checklist

**Use in the 30 minutes before the JSC kickoff call.**

Sequence — top to bottom in real time. Items in `[ ]` are paste-and-verify.

---

## T-30 min — Confirm production state still healthy

```bash
# Export keys + admin once
export GABE_KEY=dchub_developer_jhfHONJxqbyNKGJHbT4ygCcprVqly3hI
export GALEN_KEY=dchub_developer_iWmhspMSORRBBUFwjhlh7lkzWu2oC1ob
export IAN_KEY=dchub_developer_jZ6bKqlrBJvnXh9p7ylegjNR9aHZeYEv
# DCHUB_ADMIN_KEY should already be in your env
```

- [ ] **Verify site-forecast still serves Pro payload** (the r78-d fix):

  ```bash
  curl -sS -H "X-API-Key: $GABE_KEY" \
    "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); fc=d.get('deployment_forecast',{}); print('✅ Pro' if 'reference_scenario' in fc else '❌ Stub — re-deploy r78-d')"
  ```

  Expected: `✅ Pro`

- [ ] **Check for any new NLR activity since last session:**

  ```bash
  curl -sS -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
    "https://dchub-backend-production.up.railway.app/api/v1/admin/partner-usage/reveal-nlr" \
    | python3 -c "
  import sys, json
  d = json.load(sys.stdin)
  s = d.get('summary', {})
  print(f\"  active={s.get('active_keys','?')}  total={s.get('calls_total','?')}  today={s.get('calls_today','?')}  last={s.get('most_recent_call','?')}\")"
  ```

  If total > the verification-test count from last session (~87), NLR engaged overnight — great signal. Read which key spiked.

---

## T-15 min — Open these tabs

- [ ] `docs/NLR_MOU_v1.md` — current MOU (Schedule A reference)
- [ ] `docs/NLR_PRODUCT_ROADMAP.md` — Day 1 → 24mo product timeline
- [ ] `docs/NLR_SCHEDULE_A_EXPANSION.md` — 27 new endpoints proposal
- [ ] `docs/NLR_PLAYBOOK.md` — reply variants + open methodology questions
- [ ] Terminal with keys exported, ready for live demo
- [ ] `https://dchub.cloud/openapi.json` (if asked for the spec)
- [ ] `https://github.com/NatLabRockies/reVeal` (their repo)
- [ ] `https://docs.nlr.gov/docs/fy26osti/99256.pdf` (their canonical paper — page 23-24 has the methodology specs)

---

## T-5 min — Mental prep

The opener (locked in):

> *"Thanks for making time. I read the March reVeal deck — the limitations slide where you flagged transmission hosting capacity and interconnection-queue data as the priority improvements lined up exactly with where DC Hub sits today. Where would be most useful for me to start — pilot region selection, validation methodology, or something else on your minds?"*

Reframe for "why hasn't anyone touched the keys" if it comes up (it won't, you won't bring it up):

> *"Federal lab caution makes total sense — happy to confirm in writing that pre-execution Schedule A exploration is permitted under the current Tier 0. Want to do a live screen-share walkthrough so you see the data before you point it at reVeal?"*

The Schedule A expansion drop-in (when you transition methodology → product):

> *"In the 14 days since you got keys, we shipped roughly 27 new endpoints that map directly to reVeal's stated needs — including ERCOT real-time for the Texas Triangle pilot, global IXP data, and a machine-readable Data Dictionary that satisfies our Schedule E.1 deliverable today. Walk through the new categories, or pick a reVeal layer to prioritize?"*

---

## During the call — what to listen for

| If they say… | Do this |
|---|---|
| *"We haven't gotten to integration yet"* | Live screen-share `/site-forecast` for Ashburn VA. Show them the response. |
| *"We've been waiting for the MOU"* | Acknowledge, offer to confirm in writing that Tier 0 exploration is permitted now. |
| *"We have redline questions"* | Variant 1B in playbook — take notes, turn around in 48h. |
| *"What's new since we signed?"* | The Schedule A expansion drop-in above. |
| *"Can Galen pair-program with you?"* | YES. Offer a 60-min screen-share within the week. |
| *"We want to start with [region X]"* | Defer the pilot-region recommendation; let them drive. Pull `grid-intelligence?iso=X` live to show what's available. |

---

## Hot-recovery — if something breaks live

- **A key returns 401:** check via `/admin/partner-key/audit`, confirm `is_active`. If revoked accidentally, re-issue via `scripts/r72_onboard_reveal_nlr.sh`.
- **An endpoint returns 500:** switch demo to `/grid-intelligence?iso=PJM` (most reliable).
- **`/site-forecast` returns stub again:** hit Railway direct (`https://dchub-backend-production.up.railway.app`) instead of `dchub.cloud`. The edge may be cached.
- **CF Pages flap re-emerges:** check worker version via `curl -I https://dchub.cloud/alive | grep -i x-dc-worker`. If it's not 4.34.22+, the auto-deploy toggle may have been re-enabled. Re-disable via dashboard.
- **The call goes great:** activate Variant 1C — send the Schedule A expansion proposal as a follow-up the same day.

---

## After the call — 5-min wrap-up

- [ ] Update `docs/NLR_PLAYBOOK.md` with what happened (which response variant materialized, what was said)
- [ ] Send Gabe a thank-you with action items within 1 hour
- [ ] If NLR Legal sent redlines, queue r79 to address them
- [ ] If Galen asked methodology questions you couldn't answer, follow up in writing within 24h
- [ ] If Ian agreed to pair on integration, schedule the screen-share before he loses momentum
- [ ] Re-run the partner-usage check 24h after the call — engagement should have spiked if the call went well

---

## File pointers (open in tabs)

- `docs/NLR_MOU_v1.md` and `.docx`
- `docs/NLR_PRODUCT_ROADMAP.md`
- `docs/NLR_SCHEDULE_A_EXPANSION.md`
- `docs/NLR_PLAYBOOK.md`
- `docs/NLR_PARTNERSHIP_ROADMAP.md`
- `docs/NLR_LEGAL_REDLINE_NOTES.md` (historical — what we changed and why)

Live URLs:
- `https://dchub.cloud/openapi.json` — OpenAPI spec
- `https://dchub.cloud/mcp` — MCP server endpoint
- `https://dchub.cloud/api/v1/methodology/data-dictionary.json` — Data Dictionary (Schedule E.1 satisfied)
- `https://dchub-backend-production.up.railway.app/api/v1/admin/partner-usage/reveal-nlr` — live activity check

Their references:
- `https://github.com/NatLabRockies/reVeal` — open-source repo
- `https://docs.nlr.gov/docs/fy26osti/99256.pdf` — canonical March 2026 paper
- `https://research-hub.nlr.gov/en/persons/galen-maclaurin/` — Galen's research profile
