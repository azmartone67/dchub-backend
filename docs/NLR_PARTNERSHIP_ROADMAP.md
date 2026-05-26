# DC Hub × NLR — Partnership Roadmap

**Status as of 2026-05-26**

NLR Year-1 Research Seed ($3K FY 2026) → Year-2+ Strategic Partnership ($10K).
Path to signature: NDA → MOU + Publication Protocol → License Agreement,
targeting execution within 90 days.

This doc tracks every workstream — engineering, legal, publication —
across the research year. Single page so renewal conversations in FY
2027 pull the full picture from one place.

---

## Contacts

| Role           | Name              | Email                          | Status |
|----------------|-------------------|--------------------------------|--------|
| Lead PI        | Gabriel Zuckerman | Gabriel.Zuckerman@nlr.gov      | Active |
| Co-lead        | Galen *(last name TBC)* | TBC                       | Pending key |
| Integrator     | Ian *(last name TBC)*   | TBC                       | Pending key |
| DC Hub founder | Jonathan Martone  | azmartone@gmail.com            | — |

**Action**: confirm Galen + Ian's emails to mint their Developer keys via
`scripts/r72_onboard_reveal_nlr.sh`.

---

## Commercial terms (locked)

| Term              | Value                                                          |
|-------------------|----------------------------------------------------------------|
| Year 1 price      | $3,000 USD / 12 months (Research Seed)                         |
| Stripe link       | https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e                 |
| Year 2+ price     | $10,000 USD / 12 months (Strategic Partnership, on DC funding) |
| Renewal           | CPI-U capped, 60-day written notice                            |
| Cost to serve     | ~$8K/yr (compute + support + AM)                               |
| Year 1 margin     | ~-$5K (strategic investment — paid in-kind by partnership rights) |
| Year 2+ margin    | ~20% cost-plus                                                 |

Cost transparency was disclosed upfront in Jonathan's email to Gabe
(2026-05-26) so renewal conversation isn't a surprise.

---

## Engineering deliverables — already shipped

10 NLR/reVeal-specific endpoints live in production under the
`/api/v1/*` namespace, all behind the partner's Developer key:

### `nlr_intelligence.py` (Blueprint: `nlr_bp`)

| Path | What it returns |
|------|-----------------|
| `/api/v1/geothermal-potential` | Geothermal viability score for a site |
| `/api/v1/colocation-score`     | DCPI sub-score breakdown for a market |
| `/api/v1/grid-headroom`        | Available grid capacity + interconnection queue |
| `/api/v1/microgrid-viability`  | Microgrid + behind-the-meter viability for a site |

### `reveal_endpoints.py` (Blueprint: `reveal_ext_bp`)

| Path | What it returns | reVeal feature mapping |
|------|-----------------|-------------------------|
| `/api/v1/reveal-cell-bulk`        | Bounding-box query, array of cells in one request | Bulk Characterize input |
| `/api/v1/reveal-grid-export`      | Async bulk grid export (Parquet / GeoJSON)         | Training-data bulk pull |
| `/api/v1/reveal-grid-export/status/<job_id>` | Async export job status                  | — |
| `/api/v1/reveal-validation-feed`  | Newly-observed facilities aligned to projection years | reVeal v2 validation |
| `/api/v1/social-acceptance-index` | Composite local-opposition score                   | **Fills slide-25 gap** |
| `/api/v1/climate-risk`            | Flood + wildfire + extreme-heat risk per cell      | Environment dimension |
| `/api/v1/carbon-intensity`        | Marginal + average grid CO₂ intensity per cell     | Carbon dimension |

### `reveal_cell.py` (Blueprint: `reveal_bp`)

| Path | What it returns |
|------|-----------------|
| `/api/v1/reveal-cell` | Single-cell composite (lat/lon → score + features) |

### Plus the cross-cutting 9 (mapped in `DCHub_NLR_Enterprise_License_Proposal.pdf`)

`/api/v1/site-forecast`, `/api/v1/grid-intelligence`, `/api/v1/grid/data`,
`/api/v1/fiber/intel`, `/api/v1/fiber/routes`, `/api/v1/energy/retail`,
`/api/v1/energy/renewable`, `/api/v1/water/stress`, `/api/v1/infrastructure`,
`/api/v1/tax-incentives`.

**Total surface area available to NLR today: ~20 endpoints.**

---

## Document workstreams

| Doc | Status | Owner | Target | Notes |
|-----|--------|-------|--------|-------|
| NDA | ⏳ Awaiting NLR draft | NLR Legal | This week | "Happy to review and countersign the moment it lands" |
| Stripe subscription receipt | ✅ Link sent | DC Hub | Sent 2026-05-26 | $3K/yr — Gabe to procure via PO |
| MOU outline | ⏳ Draft needed | Jonathan | +30 days | Frames the Year-1 → Year-2 path |
| License Agreement | ⏳ Draft needed | DC Hub Legal | +90 days | Defines API access, rate limits, data residency, term |
| Publication Protocol | ⏳ Draft needed | Joint | +60 days | Authorship order, journal/venue, embargo, joint approval |
| Co-authorship policy | ⏳ Inline in MOU | Joint | With MOU | Names DC Hub on the validation paper + reVeal v2 paper |
| JSC Review Paper — draft outline | ⏳ Co-authored outline due | Joint | +45 days | "Improving Data Center Siting Models with Live Infrastructure Data" |
| CRADA framework (optional, Year 2+) | ⏳ Pending DC funding | Joint | Year 2 | For joint IP beyond validation paper |

Status legend: ✅ done · 🟡 in flight · ⏳ not yet started · ❌ blocked

---

## Partnership rights active Day 1 (not gated to Year 2)

Per Jonathan's 2026-05-26 email, all four rights activate the moment Gabe
signs the License — not deferred:

1. **Co-authorship** on the validation paper
2. **Reference rights** for DC Hub marketing (DC Hub can name NLR / NREL / DOE)
3. **Joint conference presence** — booth/talk co-presented at industry events
4. **reVeal v2 outputs first-look** — DC Hub sees draft outputs before public release

---

## Next 90 days — operational sequence

1. **Week 1 (now)**
   - [ ] Sign NLR NDA (NLR Legal → DC Hub countersign same day)
   - [ ] Gabe procures $3K Stripe subscription via PO
   - [ ] Galen + Ian email addresses → DC Hub mints Developer keys
     for each via `scripts/r72_onboard_reveal_nlr.sh`

2. **Weeks 2–4**
   - [ ] Gabe + team complete first integration smoke tests against
     all 20 endpoints (use `/partners/nlr` bookmark page)
   - [ ] DC Hub drafts MOU + Publication Protocol skeleton
   - [ ] Joint kickoff call once data exploration is complete (per
     Gabe's "look at data first, then plan" pacing)

3. **Weeks 5–9**
   - [ ] MOU + License Agreement red-line cycle
   - [ ] JSC Review Paper outline co-authored
   - [ ] Initial validation results: swap water_availability layer
     (reVeal slide 21 lowest-importance feature) with DC Hub live
     USGS readings, retrain RF, compare AUC

4. **Weeks 10–13**
   - [ ] License Agreement executed (90-day target)
   - [ ] Cutover Developer keys → Enterprise keys via the script's
     `PLAN=enterprise` flag (one-line cutover, idempotent)
   - [ ] JSC review paper draft co-finalized

---

## Operational levers

| If you need to | Run |
|----------------|-----|
| Mint a key for a new NLR contact | `CONTACT_EMAIL=... CONTACT_NAME="..." ./scripts/r72_onboard_reveal_nlr.sh` |
| Upgrade NLR keys Developer → Enterprise (post-License) | Same script with `PLAN=enterprise` prefix |
| Read full deal context | `curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" .../api/v1/admin/partner-key/audit \| jq '.keys[] \| select(.partner_slug=="reveal-nlr")'` |
| Revoke a compromised NLR key | `curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" .../api/v1/admin/partner-key/revoke/<key_prefix>` |
| Inspect partner page traffic | `curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" .../api/v1/admin/partner-visits` |

---

## Renewal pre-work (start 60 days before Year 1 end)

When FY 2027 approaches, this doc + the `partner_keys_issued` audit row
contain everything needed for the renewal conversation:

- The original $3K → $10K escalator was disclosed in writing on 2026-05-26
- Cost-plus math was transparent (no surprise)
- Renewal protection clause: CPI-U capped, 60-day notice
- Co-marketing IP / publication count delivered in Year 1 = leverage for
  Year 2 narrative

Read the row:
```bash
curl -s -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  "https://dchub-backend-production.up.railway.app/api/v1/admin/partner-key/audit" \
  | jq '.keys[] | select(.partner_slug=="reveal-nlr") | {plan, amount_usd_year, term_months, stripe_url, renewal_terms, issued_at}'
```

---

## Open questions / awaiting

- Galen's last name + email
- Ian's last name + email
- NLR NDA template (received from NLR Legal)
- Confirmation Gabe is OK with the $3K → $10K escalator disclosure language
- Target journal/venue for the validation paper (JSC implied — confirm full name)
