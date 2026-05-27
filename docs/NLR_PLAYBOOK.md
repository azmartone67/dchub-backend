# DC Hub × NLR — Negotiation & Execution Playbook

**Status as of 2026-05-26 (post-MOU send)**

Everything you need for the next 30-90 days of the NLR engagement — reply scripts, post-execution press collateral, Stream C launch plan, and a follow-up timeline. Update sections as state changes.

---

## §0 — How to resume this work in a new conversation

When you fork or start a fresh Claude conversation to continue NLR work, paste **one** of the two seed prompts below. Both are designed to give a new Claude full context in ~3-5 minutes of self-reading.

### Seed A — Universal (use 90% of the time)

```
Continuing NLR partnership work. Read these three docs in order:
  - docs/NLR_PLAYBOOK.md         (operational state + reply variants + timeline)
  - docs/NLR_MOU_v1.md           (the MOU we sent Gabe)
  - docs/NLR_PARTNERSHIP_ROADMAP.md (background + history)

State as of 2026-05-26:
  - MOU v1 sent to Gabriel.Zuckerman@nlr.gov
  - 3 Developer keys live: Gabriel + Galen.Maclaurin + Ian.Christie @nlr.gov
  - /partners/nlr = sanitized stub (no leak, pre-execution)
  - 4 emails in Gmail Drafts (3 onboarding + 1 courtesy ask) — may or may not be sent yet, confirm with me
  - CF Pages flap RESOLVED; worker v4.34.22-r77 stable

Today: [PASTE WHAT JUST HAPPENED HERE]
```

### Seed B — Menu Form (fast for common cases)

```
Continuing NLR partnership work. Context: read docs/NLR_PLAYBOOK.md + docs/NLR_MOU_v1.md.
Last session (2026-05-26): MOU sent to Gabe, 3 keys live, /partners/nlr stub deployed.

Today, one of:
  [ ] Gabe acknowledged receipt — draft response per Playbook §2 Variant 1A
  [ ] Counsel sent redlines — paste below, prep v2
  [ ] Counsel sent redlined .docx — attached, please review against canonical v1
  [ ] Gabe approved/signed — execution mode: flip /partners/nlr to public copy, draft post-execution press release per Playbook §2
  [ ] No response yet (Day __) — should we send a gentle nudge?
  [ ] Unrelated NLR task: ______

Details: _______
```

### What carries forward automatically (don't re-explain)

The new Claude already has access to:

| Item | Where it lives |
|---|---|
| All committed code/docs in `dchub-backend` and `dchub-frontend` | Both repos accessible via Bash/Read tools |
| Persistent memory notes — project context, deploy architecture, CF Pages flap resolution, MCP auth chain, etc. | `~/.claude/projects/.../memory/` (global across forks) |
| Gmail draft IDs | This Playbook §5 |
| Stripe link, key prefixes, file paths, code paths | This Playbook §5 |
| The unified MOU v1 (markdown + docx) | `docs/NLR_MOU_v1.md` + `.docx` |
| Workflow state (deploy-pages.yml retry-loop, auto-deploys disabled in CF Dashboard) | Visible at runtime via probe |

The new Claude won't need re-introduction to any of this. Just paste the seed + the new event.

---

## Where we are right now

| Item | State |
|---|---|
| 3 NLR Developer keys | ✅ Live (Gabe / Galen / Ian) |
| Unified MOU v1 | ✅ Sent to Gabe + NLR Legal (2026-05-26) |
| `/partners/nlr` page | ✅ Sanitized stub (no leak), pending Gabe approval |
| 3 onboarding emails | 📥 In Drafts (Gabe / Galen / Ian) |
| Gabe courtesy email (public-ref permission) | 📥 In Drafts |
| NDA | ⏳ Awaiting NLR's standard form |

---

## Section 1 — Reply Variants for Gabe's likely responses

When Gabe (or NLR Legal) responds to the MOU, the response will land in one of three buckets. Pre-drafted replies for each — paste-and-customize.

### Variant 1A — "Received, counsel reviewing"
*(Most likely first response, 1-3 days out.)*

> Gabe — perfect, appreciate the fast turnaround. No urgency on counsel timeline; happy to give them runway to do this right.
>
> While they read, two things I can offer in parallel:
>
> 1. **Stream A is technically live today** — Ian's API key is active, so if he wants to start poking at `/site-forecast`, `/water/stress`, and `/grid-intelligence` for a pilot region, nothing's gated on the MOU executing first. The data dictionary referenced in Schedule E is in OpenAPI form at dchub.cloud/openapi.json already.
> 2. **Open methodology questions for Galen** — when he's ready, I want to lock in (a) pilot region (PJM/ERCOT or other) and (b) priority limitation (transmission hosting vs water vs another). Affects the Validation Study scope per Article IX.
>
> Standing by for counsel's redline. Let me know if any clarifying questions surface while they read.
>
> Best,
> Jonathan

### Variant 1B — "Redline questions / concerns"
*(2nd most likely — they'll have a few specific clauses they want adjusted.)*

> Gabe — thanks for the feedback. Quick reactions:
>
> [Then for each redline item, one of the following responses — pick the right one per item:]
>
> - **Accept**: "Counsel is right on that one — happy to accept. Will land in v2."
> - **Negotiate**: "Reasonable position. Counter-proposal: [DC Hub's modified version]. Underlying concern is [reason]. Open to discussion."
> - **Push back**: "I'd ask we keep the original language. The reason it's structured that way is [reason]. Open to a call to walk through if it'd help."
>
> Where you land on overall timing — do you want me to incorporate accepted redlines into a v2 now and send back, or wait for the full set of counsel's comments?
>
> Best,
> Jonathan

### Variant 1C — "Ready to sign" / "Approved"
*(Best case — happens after one or two redline cycles.)*

> Gabe — let's execute.
>
> I'll counter-sign as soon as I have the NLR signature copy. From my side, the [STATE] placeholder for our LLC formation is **Delaware** (I'll update v-final before counter-signing).
>
> After execution:
>   1. I'll flip the dchub.cloud/partners/nlr page from the sanitized stub to the factual-reference copy we agreed to in Article XII Section 12.4 (subject to your final review of the substantive copy)
>   2. Schedule A data-dictionary delivery starts the 60-day clock (per Schedule E Section E.1)
>   3. JSC kickoff at your convenience — 30 min, you + Galen + Ian + me, agenda already drafted
>   4. I'll send a courtesy note to your counsel directly thanking them for the fast turn
>
> If you want, send me your Outlook free/busy and I'll find a kickoff slot in the next 2 weeks.
>
> Best,
> Jonathan

---

## Section 2 — Post-Execution Press Release (NLR-approval-gated)

**Status: DRAFT — do NOT publish without explicit NLR JSC sign-off per MOU Article XII § 12.4.**

When MOU executes, the announcement copy below is ready. Submit to Gabe for JSC review before any publication; minimum 10-business-day approval window per Article XII § 12.3.

### Draft press release

> **DC Hub and National Laboratory of the Rockies Begin Open-Method Validation Study of Geospatial Data-Center Siting Projections**
>
> *Joint research effort compares reVeal model output against real-time operational data across U.S. interconnection markets; methodology and findings to be published in peer-reviewed venue.*
>
> **[CITY] — [DATE]** — DC Hub, the data-center intelligence platform at dchub.cloud, and the National Laboratory of the Rockies (NLR), a U.S. Department of Energy federally funded research and development center, today announced a research engagement to validate NLR's open-source reVeal geospatial siting model against real-time grid, infrastructure, and operational data flowing through DC Hub's production platform.
>
> NLR's reVeal package, available at github.com/NatLabRockies/reveal, identifies suitable sites for new data-center capacity by combining grid, environmental, and siting-feasibility signals. DC Hub provides approximately 25 production endpoints spanning grid intelligence (reserve margin, queue depth, ISO load timeseries), siting variables (water stress, fiber routing, tax incentives), and composite scoring across 12,000+ tracked facilities globally.
>
> The engagement is structured as three concurrent work streams: research-data integration (NLR consumes DC Hub data under a research-tier license), joint validation research (peer-reviewed publication comparing reVeal projections against DC Hub's operational signals), and open-method extension (open-source tooling to export DC Hub feeds into the reVeal ecosystem). The Joint Steering Committee comprises Galen Maclaurin and Gabriel Zuckerman from NLR and Jonathan Martone from DC Hub.
>
> "NLR's reVeal program represents the open-method standard for data-center siting research, and we're privileged to provide the real-time operational signal that makes their projections testable against the ground truth," said Jonathan Martone, founder of DC Hub. "The validation paper produced by this engagement should set a new bar for how the field validates its siting models — and the open-source tooling from Stream C will lower the methodology barrier for the next wave of academic research."
>
> [PLACEHOLDER FOR NLR-DRAFTED QUOTE — Gabriel Zuckerman or Galen Maclaurin, subject to NLR's institutional review.]
>
> The engagement is supported by DOE prime contract [DOE CONTRACT NUMBER]. The methodology and findings from the joint validation study will be submitted to a peer-reviewed venue, with publication anticipated within 6-9 months of the engagement's effective date.
>
> ---
> **About DC Hub** — DC Hub is a data-center intelligence platform tracking 12,000+ global facilities with real-time grid, fiber, water, energy-pricing, climate, and infrastructure data. The platform's OpenAPI surface is documented at dchub.cloud/openapi.json. DC Hub is operated by Martone Advisors, LLC (d/b/a DC Hub).
>
> **About NLR** — The National Laboratory of the Rockies is a federally funded research and development center operating for the U.S. Department of Energy. NLR develops open-source tools and methodologies for energy-systems research, including the reVeal package for data-center siting analysis.
>
> ---
> Media contact: Jonathan Martone, DC Hub — jonathan@dchub.cloud
> NLR media contact: [PLACEHOLDER — NLR to provide]

### Required approvals before publication

- [ ] Gabe / NLR JSC review of substantive copy (Article XII § 12.4)
- [ ] NLR Legal review of attribution language (Schedule D § D.4)
- [ ] NLR-drafted institutional quote (replacing placeholder)
- [ ] DOE contract number confirmed (replacing placeholder)
- [ ] NLR media contact confirmed (replacing placeholder)
- [ ] DC Hub Effective Date filled in (replacing [DATE])

---

## Section 3 — Stream C Open-Source Scaffold

**Status: PRE-EXECUTION — do NOT create the public repo until MOU executes.** Stream C requires both Parties' written project scope per MOU Article III § 3.3.

### Proposed repo structure (after MOU execution)

**Suggested name:** `dchub-revealkit` or `dchub-reveal-bridge` (final name JSC-agreed at kickoff)
**License:** Apache 2.0 or BSD-3-Clause (Section 11.3 — DC Hub's election as initial contributor)
**Hosting:** github.com/azmartone67/dchub-revealkit (DC Hub-maintained) with a mirror or fork option for NLR if requested

**Initial directory layout:**

```
dchub-revealkit/
├── README.md                    # what this is, who maintains, license, citation
├── LICENSE                       # Apache 2.0 text
├── CONTRIBUTING.md              # PR process, code review, attribution
├── CODE_OF_CONDUCT.md           # standard
├── CITATION.cff                 # canonical citation per Schedule D § D.3
├── pyproject.toml               # Python package; alternative: requirements.txt
├── docs/
│   ├── quickstart.md            # "first-pull-to-reveal" walkthrough
│   ├── data-dictionary.md       # endpoint-by-endpoint reVeal-input semantics
│   └── api-keys.md              # how to request a research-tier key
├── src/
│   └── dchub_revealkit/
│       ├── __init__.py
│       ├── client.py            # thin DC Hub API wrapper with research-tier auth
│       ├── exporters/
│       │   ├── grid.py          # /grid-intelligence + /grid/data → reVeal grid layer
│       │   ├── water.py         # /water/stress → reVeal water layer
│       │   ├── siting.py        # /infrastructure + /fiber/* → reVeal siting layer
│       │   └── composite.py     # /site-forecast → reVeal benchmark target
│       └── transformers/
│           └── reveal_cells.py  # bbox → reveal-cell-bulk format
├── examples/
│   ├── load_pjm_corridor.py     # the validation paper's worked example
│   └── water_layer_swap.ipynb   # before/after notebook (Galen's territory)
└── tests/
    └── test_smoke.py
```

### Initial commit set (post-execution)

1. `README.md` with NLR-co-authored intro paragraph + citation block
2. `LICENSE` (Apache 2.0)
3. Stubbed `client.py` that hits dchub.cloud with X-API-Key auth
4. One worked example: `examples/load_pjm_corridor.py`
5. `CITATION.cff` per Schedule D citation form

### Coordination protocol

- DC Hub opens PRs to feature branches; NLR (Galen, Ian) review before merge
- NLR can open PRs back to DC Hub against the DC Hub repo for upstream changes
- Both parties named in `CONTRIBUTORS.md`
- Releases tagged + DOI'd via Zenodo for the validation paper to cite

### Pre-execution prep DC Hub can do internally

These don't violate MOU Article III § 3.3 because they're local-only / unpublished:

- [ ] Reserve the `dchub-revealkit` name on a private DC Hub repo (so name isn't squatted)
- [ ] Draft the `README.md` and `client.py` locally
- [ ] Stand up the package scaffold in a private branch of dchub-backend
- [ ] On Effective Date, push the private branch → new public repo → tag `v0.0.1-rc1`

---

## Section 4 — Follow-up Timeline

Pacing for the next 90 days. Items marked ⏰ are reminder-worthy.

### Days 0-3 (now → 2026-05-29)
- [x] MOU v1 sent to Gabe (2026-05-26)
- [ ] Send 3 onboarding emails + Gabe courtesy email (currently in Drafts)
- [ ] Confirm Gabe receipt of MOU
- ⏰ **If no acknowledgement by Day 3**: send a gentle "received?" follow-up

### Days 4-10 (2026-05-30 → 2026-06-05)
- [ ] NLR Legal first read of MOU
- [ ] Gabe likely first response (Variant 1A territory)
- [ ] Ian may begin technical integration (smoke tests, OpenAPI exploration)
- [ ] Galen may begin methodology conversations (pilot region, priority limitation)
- ⏰ **If no Gabe response by Day 7**: ping Gabe directly (not Legal)

### Days 11-21 (2026-06-06 → 2026-06-16)
- [ ] First counsel redline received
- [ ] DC Hub turn-around: 48 hours target
- [ ] v2 MOU sent
- ⏰ **If counsel response slips past Day 21**: schedule a joint call (you + their counsel + ours)

### Days 22-45 (~2 months out)
- [ ] Second redline cycle (if needed)
- [ ] MOU execution
- [ ] Within 24h of execution: send post-execution congratulations note, propose JSC kickoff date
- [ ] Within 1 week of execution: JSC kickoff meeting
- [ ] Within 1 week of execution: NLR-approved public landing-page copy live
- [ ] Within 2 weeks of execution: NLR-approved press release published

### Days 46-90 (~2-3 months out)
- [ ] Schedule E Data Dictionary delivered (60 days from Effective Date per § E.1)
- [ ] Server-side path aliases shipped (60 days from Effective Date per § 14.4)
- [ ] Stream C public repo launched (`dchub-revealkit`)
- [ ] Stream B pilot-region work begins
- [ ] Quarterly JSC scheduled

### Days 90-180 (~3-6 months out)
- [ ] First Validation Study draft circulated for JSC review
- [ ] 30-day pre-submission review window opens (per § 9.3)
- [ ] Manuscript submitted to peer-reviewed venue
- [ ] Cited-endpoint stability commitment activates (12 months from submission, per § 14.3)

---

## Section 5 — Quick reference

### Key files
- `docs/NLR_MOU_v1.md` + `.docx` — the sent MOU
- `docs/NLR_PARTNERSHIP_ROADMAP.md` — internal roadmap (v2)
- `docs/NLR_LEGAL_REDLINE_NOTES.md` + `.docx` — historical context (what we changed and why)
- `docs/NLR_PLAYBOOK.md` — this document

### Gmail draft IDs (in Gmail's API format)
- Gabe onboarding: `19e65ef08ecf8b84`
- Galen onboarding: `19e65ef856f14743`
- Ian onboarding: `19e65f00bf0477bf`
- Gabe courtesy (public-ref): `r2293638786167165649`
- Gabe MOU (sent): superseded by send

### Code paths
- `routes/partner_landing.py` — `/partners/nlr` stub + flip flag
- `routes/partner_key_issuer.py` — admin key minting
- `scripts/r72_onboard_reveal_nlr.sh` — onboarding script

### Stripe link
- `https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e` — $3K Research Seed

### URLs to update post-execution
- `dchub.cloud/partners/nlr` — flip `pre_execution=False` after NLR sign-off on public copy
