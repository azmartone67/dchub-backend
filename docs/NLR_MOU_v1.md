# MEMORANDUM OF UNDERSTANDING

**Research Engagement, Data License, and Publication Protocol**

Between

**National Laboratory of the Rockies**
*(operating for the U.S. Department of Energy under prime contract [DOE CONTRACT NUMBER])*

and

**Martone Advisors, LLC, d/b/a DC Hub**
*(a [STATE] limited liability company)*

**Version 1 — DRAFT — 2026-05-26**
*Consolidates the prior 4-document package (00 Overview, 02 MOU Framework, 03 Research Data License, 04 Publication Protocol) into a single executable agreement. All DC Hub-side redlines from the counter-proposal package are incorporated in-line.*

---

## Article I — Parties and Recitals

**1.1 Parties.** This Memorandum of Understanding ("**MOU**" or "**Agreement**") is entered into as of the Effective Date by and between:

- **National Laboratory of the Rockies**, a federally funded research and development center operated by [NLR OPERATING ENTITY LEGAL NAME] under DOE prime contract [DOE CONTRACT NUMBER] ("**NLR**"), and
- **Martone Advisors, LLC, d/b/a DC Hub**, a [STATE] limited liability company with principal offices at [DC HUB ADDRESS] ("**DC Hub**").

Each, a "**Party**"; collectively, the "**Parties**."

**1.2 Recitals.**
   1. NLR has developed and maintains **reVeal**, an open-source geospatial modeling tool that identifies suitable sites for new data-center capacity by combining grid, environmental, and siting-feasibility signals. The reVeal source is published at `github.com/NatLabRockies/reveal`.
   2. DC Hub operates a commercial data-center intelligence platform at `dchub.cloud`, providing real-time grid, fiber, water, energy-pricing, climate, and infrastructure data spanning approximately 25 production endpoints (see **Schedule A**) plus an MCP server endpoint at `dchub.cloud/mcp`.
   3. The Parties wish to formalize a research engagement under which (i) NLR consumes DC Hub data under a research-tier license, (ii) the Parties jointly produce a peer-reviewed validation study comparing reVeal projections against DC Hub operational signals, and (iii) the Parties cooperate on open-method tooling extending the reVeal ecosystem.
   4. This MOU consolidates terms that would otherwise be split across an MOU, a research-data license, and a publication protocol into a single integrated document for clarity and efficiency of counsel review.

---

## Article II — Purpose and Effective Date

**2.1 Purpose.** This MOU establishes the framework for a multi-stream research engagement between NLR and DC Hub, governs DC Hub's licensing of data to NLR for research use, and sets out the protocol under which the Parties jointly publish findings.

**2.2 Effective Date.** This MOU takes effect on the date of the last Party's signature ("**Effective Date**").

---

## Article III — Scope of Engagement (Three Work Streams)

The engagement comprises three concurrent work streams. Each stream operates under the terms of this MOU; no separate agreement is required for any of the three.

**3.1 Stream A — Research Data Integration.** DC Hub grants NLR a research-tier license to the data feeds and endpoints set out in **Schedule A**. NLR incorporates the data into reVeal's feature set for one or more pilot regions identified by the Joint Steering Committee (Section 5). Initial pilot scope is anticipated to focus on transmission hosting capacity in PJM (Mid-Atlantic / Ashburn corridor) and/or ERCOT (Texas Triangle), subject to JSC confirmation.

**3.2 Stream B — Joint Validation Research.** The Parties jointly produce a peer-reviewed publication comparing reVeal's projected data-center buildout against DC Hub's `discovered_facilities` operational signal. Working title: *"Validating Geospatial Data Center Buildout Projections with Real-Time Operational Signals — A reVeal × DC Hub Case Study, 2025–2028."* Authorship, review, and publication terms are set out in **Article IX**.

**3.3 Stream C — Open-Method Extension.** The Parties may cooperate on open-source tooling that exports DC Hub feeds into the reVeal ecosystem. Any code produced under Stream C is licensed under Apache 2.0 or BSD-3-Clause at the contributing Party's election. Substantial work under Stream C requires a separate written project scope confirmed by both Parties.

---

## Article IV — Term and Renewal

**4.1 Initial Term.** Twenty-four (24) months from the Effective Date.

**4.2 Renewal.** This MOU renews automatically for successive twelve (12)-month periods unless either Party provides written notice of non-renewal not less than sixty (60) days prior to the end of the then-current term.

**4.3 Fee Adjustment on Renewal.** Fees set out in **Schedule B** adjust on renewal by the lesser of (a) five percent (5%) or (b) the Consumer Price Index for All Urban Consumers (CPI-U) change over the preceding twelve (12) months. The NLR discount ratio (commercial rate minus NLR rate, expressed as a percentage of commercial rate) remains not less than ninety percent (90%) for the life of the engagement.

**4.4 Termination for Convenience.** Either Party may terminate this MOU for convenience on ninety (90) days' written notice. Fees paid in advance are pro-rated and refunded for the post-termination portion of the then-current term.

---

## Article V — Joint Steering Committee

**5.1 Composition.** A Joint Steering Committee ("**JSC**") oversees the engagement. The initial JSC consists of:

| Role | Party | Representative |
|---|---|---|
| Strategic lead | NLR | Gabriel Zuckerman |
| Technical lead | NLR | Galen Maclaurin |
| Integration lead | NLR | Ian Christie |
| Executive sponsor | DC Hub | Jonathan Martone |

**5.2 Cadence.** The JSC meets at least quarterly. Either Party may convene an ad-hoc JSC session on five (5) business days' notice.

**5.3 Decisions.** The JSC operates by consensus. Where consensus is unavailable, the Parties' respective executive sponsors confer in good faith.

---

## Article VI — Commercial Terms

**6.1 Tiers.** Three subscription tiers are available, set out in **Schedule B**. The NLR election for the Initial Term is Tier 0 (Research Seed, FY 2026 only); the renewal-default election is Tier 1 (Research) unless either Party proposes otherwise during renewal negotiation.

**6.2 Tier 0 — Research Seed (FY 2026 only).** Three thousand United States dollars ($3,000) for the twelve-month period beginning on the Effective Date. Full Schedule A endpoint surface; 95% SLA; standard rate limits per **Schedule C**.

**6.3 Tier 1 — Research (FY 2027+).** Ten thousand United States dollars ($10,000) per year. Identical functional access to Tier 0; the elevated fee reflects DC Hub's cost-recovery posture after FY 2026.

**6.4 Tier 2 — Research Plus (optional, NLR election).** Twenty-five thousand United States dollars ($25,000) per year. Tier 1 entitlements plus enhanced SLA (99%), bulk-export endpoints, and quarterly methodology-sync calls per **Schedule C**.

**6.5 Payment Mechanism.** Payment is via the Stripe Payment Link at `https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e` for Tier 0; subsequent tiers via NLR purchase-order, net-30 from invoice receipt.

**6.6 In-Kind Value Exchange.** The Parties acknowledge that the discounted NLR fees reflect substantial in-kind consideration flowing from NLR to DC Hub. The Parties further agree that the In-Kind Consideration set out in **Schedule B § B.5** is material consideration for the discount and that neither Party will, during the term of this MOU, characterize the relationship as a one-sided commercial sale or a NLR-funded grant.

---

## Article VII — Data License

**7.1 Grant.** Subject to NLR's compliance with this MOU, DC Hub grants NLR a non-exclusive, non-transferable, non-sublicensable license to access, query, cache, and use the data made available via the endpoints listed in **Schedule A** for the research and publication purposes contemplated by Streams A, B, and C.

**7.2 Permitted Uses.** NLR may use the licensed data for: (a) integration into reVeal's research-tier feature set, (b) joint research output under Stream B (including the Validation Study), (c) institutional research presentations and conferences subject to the citation requirements of **Schedule D**, (d) training data-science staff and JSC participants in DC Hub data structures, and (e) any additional use mutually agreed by the JSC in writing.

**7.3 Prohibited Uses.** The Permitted Uses above are exclusive. NLR shall not (i) redistribute the licensed data in bulk to third parties, (ii) use the licensed data to train models that would functionally substitute for a DC Hub subscription, (iii) provide paid consulting opinions on specific data-center siting decisions based solely on the licensed data, or (iv) re-license or resell the data. The full Acceptable Use Policy is set out in **Schedule H**.

**7.4 Endpoint Surface.** The licensed endpoints are enumerated in **Schedule A**. As of the Effective Date, all endpoints listed in Schedule A are live in production; none is "in development" or subject to future delivery risk.

**7.5 Service Levels and Rate Limits.** Set out in **Schedule C**.

**7.6 Endpoint Stability for Cited Endpoints.** Endpoints expressly cited in a peer-reviewed publication produced under Stream B are subject to the change-management commitments in **Schedule G § G.3**, including a minimum twelve (12)-month no-breaking-change window from the date of journal submission.

---

## Article VIII — Attribution and Citation

**8.1 Attribution Form.** When NLR or its publication venue cites DC Hub data, the canonical attribution form is:

> *"Data provided by DC Hub (dchub.cloud) under a research license to [NLR OPERATING ENTITY LEGAL NAME]."*

Full attribution language and acceptable variants are set out in **Schedule D**.

**8.2 No Endorsement.** Attribution under this Article is factual reference only and does not constitute NLR's endorsement of DC Hub or its commercial products. Both Parties agree to avoid language in publications, press releases, social media, or marketing materials that would reasonably be read as NLR endorsing a DC Hub commercial product or service.

---

## Article IX — Publication Protocol

**9.1 Working Title and Scope.** Stream B output is anticipated as a peer-reviewed publication tentatively titled *"Validating Geospatial Data Center Buildout Projections with Real-Time Operational Signals — A reVeal × DC Hub Case Study, 2025–2028."* Final title and venue are subject to JSC concurrence.

**9.2 Authorship.** Co-authorship is shared. The author list as of the Effective Date is Galen Maclaurin (NLR), Jonathan Martone (DC Hub), and Gabriel Zuckerman (NLR), in author order to be confirmed by the JSC at first manuscript review. Additional authors may be added by JSC consensus.

**9.3 Pre-Submission Review.** Each Party shall be provided the manuscript not less than thirty (30) days prior to journal submission for review and comment. Review is limited to (i) factual accuracy as to the reviewing Party's own data, methods, or position; (ii) confidentiality (Article XI); and (iii) attribution language. No Party may require redaction of findings on the basis that the findings are unfavorable to that Party's commercial or institutional interest ("**Honesty Clause**").

**9.4 Acknowledgments.** The publication's Acknowledgments section shall include (a) acknowledgment of DOE prime contract [DOE CONTRACT NUMBER] supporting NLR's contribution, (b) acknowledgment of DC Hub's data license, and (c) any additional acknowledgments mutually agreed by the JSC.

**9.5 Joint Conference Presentations.** Either Party may present joint findings at academic or industry conferences, subject to the citation requirements of Article VIII and the Honesty Clause of Section 9.3.

**9.6 First-Look on Open-Method Outputs.** Each Party will provide the other with not less than seven (7) days' advance review of any open-source code release produced under Stream C prior to public publication, solely to confirm attribution and that the release does not inadvertently expose Confidential Information.

---

## Article X — Confidentiality

**10.1 Definition.** "**Confidential Information**" means non-public information of either Party disclosed under this MOU and identified as confidential at the time of disclosure or that a reasonable recipient would understand to be confidential from the context. Confidential Information specifically includes (a) DC Hub's API keys and authentication tokens, (b) NLR's pre-publication research data, (c) DOE-restricted information, and (d) the executed financial terms of this MOU.

**10.2 Use and Disclosure.** Each Party shall (i) use the other's Confidential Information solely for the purposes of this MOU, (ii) protect it with the same care it uses for its own confidential information of similar sensitivity but no less than reasonable care, and (iii) disclose it only to its personnel, contractors, or authorized counsel with a need to know who are bound to comparable confidentiality obligations.

**10.3 Exclusions.** Confidential Information does not include information that (a) is or becomes publicly available through no fault of the receiving Party, (b) was lawfully known to the receiving Party prior to disclosure without obligation of confidence, (c) is rightfully received from a third party without obligation of confidence, or (d) is independently developed without use of the disclosing Party's Confidential Information.

**10.4 Compelled Disclosure.** Where disclosure is compelled by law or court order, the compelled Party shall (where lawfully permitted) provide prompt written notice to the other Party so that protective measures may be sought.

**10.5 Survival.** Confidentiality obligations survive termination of this MOU for three (3) years.

---

## Article XI — Intellectual Property

**11.1 Background IP.** Each Party retains all right, title, and interest in its own pre-existing intellectual property, including without limitation the DC Hub platform and source code (DC Hub) and the reVeal source and methodology (NLR).

**11.2 Foreground IP — Joint Research Output.** Intellectual property generated jointly under Stream B is owned jointly by the Parties, with each Party having an unrestricted, royalty-free, non-exclusive license to use such joint IP for its own research, commercial, and institutional purposes.

**11.3 Foreground IP — Open-Method Code (Stream C).** Code released under Stream C is open-source under Apache 2.0 or BSD-3-Clause at the contributing Party's election. Each Party retains copyright in its own contributions; neither Party acquires rights in the other Party's contributions other than as expressly granted by the chosen open-source license.

**11.4 No Implied Licenses.** Except as expressly granted in this MOU, no license is granted by either Party to the other under any patent, copyright, trade secret, or other intellectual property right.

---

## Article XII — Co-Marketing and Use of Names

**12.1 Permitted References.** Each Party may make factual references to the existence of this MOU and the research engagement (e.g., "NLR is a research user of DC Hub data"; "DC Hub is a research data partner of NLR's reVeal program"). Both Parties may include the other on a public list of partners, customers, or institutional users provided the listing is factual and not promotional.

**12.2 Endorsement Restriction.** Neither Party may use the other's name, logo, or marks in advertising, marketing, or promotional materials in a manner that would reasonably suggest endorsement of a commercial product or service without the other Party's prior written approval. This restriction reflects NLR's federally funded research and development center status and the corresponding prohibition on endorsement of commercial offerings.

**12.3 Approval Process.** Either Party may submit proposed co-marketing copy to the other Party for approval. The reviewing Party shall respond within ten (10) business days; absence of response within that window is not deemed approval.

**12.4 DC Hub Public Landing Page.** Any DC Hub public web page that references NLR by name shall be reviewed and approved in writing by NLR's strategic JSC lead prior to publication and prior to any material substantive update. As of the Effective Date, DC Hub maintains a pre-execution stub at `dchub.cloud/partners/nlr` that does not reveal commercial terms or NLR-conferred rights; substantive content for that page is subject to JSC approval per this Section.

---

## Article XIII — Security and Data Handling

**13.1 Operational Security.** DC Hub maintains operational security aligned with NIST 800-53 controls, including (a) TLS 1.2+ on all production endpoints, (b) AES-256 encryption at rest, and (c) infrastructure operated on Cloudflare, Railway, and Neon Postgres platforms.

**13.2 Third-Party Certification.** DC Hub does not, as of the Effective Date, hold an active SOC 2, FedRAMP, ISO 27001, or comparable third-party certification. DC Hub commits to initiate a SOC 2 Type 1 audit cycle within FY 2027 if NLR's publication venue or institutional review process requires third-party certification of DC Hub security posture.

**13.3 Incident Notification.** DC Hub shall notify NLR's designated security contact within seventy-two (72) hours of confirmed compromise affecting NLR Confidential Information. Notification includes a description of the affected data, the suspected scope of compromise, and mitigation measures taken.

**13.4 NLR Security Contact.** NLR shall provide a current security contact for incident notification purposes. As of the Effective Date, NLR has not yet provided this contact; NLR shall do so within thirty (30) days of execution.

Additional security and incident-response terms are set out in **Schedule F**.

---

## Article XIV — Change Management and Deprecation

**14.1 Breaking Changes.** DC Hub shall provide NLR with not less than ninety (90) days' advance written notice of any breaking change to an endpoint in Schedule A.

**14.2 Endpoint Deprecation.** DC Hub shall provide NLR with not less than one hundred eighty (180) days' advance written notice of full deprecation of any endpoint in Schedule A.

**14.3 Cited-Endpoint Stability.** For any endpoint expressly cited in a peer-reviewed publication produced under Stream B, DC Hub commits to no breaking change within twelve (12) months of journal submission, irrespective of the standard ninety (90)-day notice in Section 14.1.

**14.4 Alias Maintenance.** Where a Schedule A endpoint path uses one form (e.g., `/api/v1/water-risk`) and DC Hub's live production path uses an aliased form (e.g., `/api/v1/water/stress`), DC Hub shall maintain server-side aliases such that both forms resolve to the same content for the duration of this MOU. Aliases land in production within sixty (60) days of the Effective Date.

Additional change-management terms are set out in **Schedule G**.

---

## Article XV — Acceptable Use

NLR's use of the licensed data is governed by the Acceptable Use Policy set out in **Schedule H**.

---

## Article XVI — Warranties and Disclaimers

**16.1 Mutual Warranties.** Each Party warrants that (a) it has the corporate authority to enter into this MOU, (b) execution of this MOU does not violate any other agreement to which it is a party, and (c) it will perform its obligations in compliance with applicable law.

**16.2 DC Hub Data Warranty.** DC Hub warrants that it has the right to license the data made available under Schedule A and that, to its knowledge, the data does not infringe third-party intellectual property rights.

**16.3 Disclaimer.** **EXCEPT AS EXPRESSLY SET FORTH IN THIS ARTICLE XVI, THE LICENSED DATA IS PROVIDED "AS IS." DC HUB DISCLAIMS ALL OTHER WARRANTIES, EXPRESS OR IMPLIED, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. DC HUB DOES NOT WARRANT THAT THE DATA IS COMPLETE, ERROR-FREE, OR SUITABLE FOR ANY PARTICULAR REGULATORY, INVESTMENT, OR SITING DECISION.**

---

## Article XVII — Limitation of Liability

**17.1 Cap.** Except for breaches of Article X (Confidentiality), Article XI (IP), Article XII (Co-Marketing), or for indemnifiable third-party claims, neither Party's aggregate liability under this MOU shall exceed twice (2x) the fees paid or payable by NLR in the twelve (12) months preceding the event giving rise to liability.

**17.2 Excluded Damages.** Neither Party shall be liable for indirect, incidental, special, consequential, or punitive damages, even if advised of the possibility, except for breaches of confidentiality or IP provisions.

---

## Article XVIII — Acquisition Survival

**18.1 Survival.** This MOU shall survive a change of control, merger, acquisition, or sale of substantially all assets of DC Hub. The acquiring entity shall assume DC Hub's obligations under this MOU as a successor in interest.

**18.2 NLR Termination Right on Material Change.** If, in NLR's reasonable judgment, an acquisition or change of control of DC Hub materially changes the nature of the counterparty (for example, acquisition by an entity whose mission is incompatible with NLR's institutional position or DOE mission), NLR may terminate this MOU on ninety (90) days' written notice without cause. Fees paid in advance are pro-rated and refunded for the post-termination portion of the then-current term.

---

## Article XIX — Termination

**19.1 Termination for Cause.** Either Party may terminate this MOU for material breach by the other Party that remains uncured for thirty (30) days following written notice describing the breach with reasonable specificity.

**19.2 Termination for Convenience.** As set out in Section 4.4.

**19.3 NLR Material-Change Termination.** As set out in Section 18.2.

**19.4 Effect of Termination.** Upon termination, (a) NLR shall cease use of the licensed data except as necessary to complete then-pending Stream B publications, (b) DC Hub shall continue to provide read access to cited Schedule A endpoints for the duration of the cited-endpoint stability window in Section 14.3, and (c) confidentiality obligations survive per Section 10.5.

---

## Article XX — Governing Law and Dispute Resolution

**20.1 Governing Law.** This MOU is governed by the laws of the State of [JURISDICTION], without regard to its conflict-of-laws principles. Federal law applies to provisions implicating NLR's federally funded research and development center status, DOE prime contract obligations, or U.S. Government rights in DOE-funded research output.

**20.2 Dispute Resolution.** The Parties shall first attempt to resolve disputes through good-faith JSC discussion. Unresolved disputes proceed to non-binding mediation in [JURISDICTION] before any litigation.

**20.3 Forum.** Litigation arising from this MOU shall be brought in the state or federal courts located in [JURISDICTION], and each Party consents to personal jurisdiction in such courts.

---

## Article XXI — Notices

**21.1 Form.** Notices required or permitted under this MOU shall be in writing, delivered to the addresses set out below or to such other address as a Party may designate by written notice.

**21.2 NLR Notice Address.**
> National Laboratory of the Rockies
> Attention: [NLR LEGAL NOTICE CONTACT]
> [NLR ADDRESS]
> Email: [NLR LEGAL EMAIL]

**21.3 DC Hub Notice Address.**
> Martone Advisors, LLC, d/b/a DC Hub
> Attention: Jonathan Martone
> [DC HUB ADDRESS]
> Email: jonathan@dchub.cloud

---

## Article XXII — Miscellaneous

**22.1 Entire Agreement.** This MOU, together with its Schedules, constitutes the entire agreement between the Parties on the subject matter and supersedes any prior or contemporaneous communications, agreements, or term sheets.

**22.2 Amendments.** No amendment to this MOU is effective unless in writing and signed by both Parties.

**22.3 Waiver.** Failure to enforce any provision is not a waiver of the right to enforce later.

**22.4 Severability.** If any provision is held unenforceable, the remainder of this MOU remains in full force, and the unenforceable provision is reformed to the minimum extent necessary to render it enforceable.

**22.5 Assignment.** Neither Party may assign this MOU without the other's prior written consent, except that DC Hub may assign to a successor in interest in connection with an acquisition or sale of substantially all assets (subject to NLR's rights under Article XVIII).

**22.6 Counterparts and Electronic Signature.** This MOU may be executed in counterparts, each of which constitutes an original, and all of which together constitute one and the same instrument. Electronic signatures are valid and binding.

**22.7 No Third-Party Beneficiaries.** This MOU is for the sole benefit of the Parties. No third party has rights or remedies under it.

---

## Signatures

**National Laboratory of the Rockies**

_____________________________________
Name: [NLR SIGNATORY NAME]
Title: [NLR SIGNATORY TITLE]
Date: ___________________

**Martone Advisors, LLC, d/b/a DC Hub**

_____________________________________
Name: Jonathan Martone
Title: Founder
Date: ___________________

---

# SCHEDULES

---

## Schedule A — Endpoint Surface

DC Hub licenses NLR the following production endpoints under this MOU. All endpoints listed are live in production as of the Effective Date.

### A.1 Grid and Interconnection (6 endpoints)
- `/api/v1/grid-headroom` — per-region reserve margin
- `/api/v1/grid-intelligence` — composite grid signal (reserve + queue depth)
- `/api/v1/grid-data` — raw ISO load timeseries *(also resolves at `/api/v1/grid/data`)*
- `/api/v1/interconnection-queue` — ISO queue snapshots
- `/api/v1/infrastructure` — HIFLD substations + FEMA hazard overlay
- `/api/v1/energy-prices` — EIA state-level retail electricity rates *(also resolves at `/api/v1/energy/retail`)*

### A.2 Siting Variables (6 endpoints)
- `/api/v1/air-permitting` — state-level air-permitting posture
- `/api/v1/tax-incentives` — 50-state data-center tax abatements
- `/api/v1/water-risk` — USGS water-stress readings *(also resolves at `/api/v1/water/stress`)*
- `/api/v1/fiber-intel` — per-facility carrier intel *(also resolves at `/api/v1/fiber/intel`)*
- `/api/v1/renewable-energy` — renewable capacity + PPA depth *(also resolves at `/api/v1/energy/renewable`)*
- `/api/v1/geothermal-potential` — geothermal score

### A.3 Composite Intelligence — reVeal-aligned (7 endpoints)
- `/api/v1/reveal-cell` — cell-level composite for reVeal input
- `/api/v1/colocation-score` — colocation viability score
- `/api/v1/microgrid-viability` — microgrid feasibility
- `/api/v1/intelligence-index` — composite siting index
- `/api/v1/analyze-site` — single-site full report
- `/api/v1/compare-sites` — multi-site comparison
- `/api/v1/dchub-recommendation` — DC Hub composite recommendation

### A.4 Market and Facility Data (5 endpoints + 1 snapshot)
- `/api/v1/facility` — facility detail lookup
- `/api/v1/search-facilities` — facility search
- `/api/v1/pipeline` — construction pipeline data
- `/api/v1/market-intel` — market-level intelligence
- `/api/v1/news` — facility-tagged news (social-acceptance proxy)
- `/api/v1/list-transactions` — M&A and transaction history
- *Quarterly snapshot:* Parquet or GeoJSON dump of `discovered_facilities`, delivered out-of-band

### A.5 reVeal-Specific Endpoints (6 endpoints, all live as of Effective Date)
- `/api/v1/reveal-cell-bulk` — bulk cell composite by bounding box
- `/api/v1/reveal-grid-export` + `/api/v1/reveal-grid-export/status/<job_id>` — async grid export
- `/api/v1/reveal-validation-feed` — validation feed for reVeal model
- `/api/v1/social-acceptance-index` — local-opposition signal (fills slide-25 gap)
- `/api/v1/climate-risk` — climate-risk overlay
- `/api/v1/carbon-intensity` — carbon-intensity timeseries

**Total: approximately 25 production endpoints + 1 quarterly snapshot.**

**Authentication:** All endpoints accept the `X-API-Key` header with a Developer-tier or higher key issued by DC Hub to JSC members.

**Documentation:** OpenAPI specification at `https://dchub.cloud/openapi.json`. MCP server at `https://dchub.cloud/mcp` with server card at `/.well-known/mcp/server-card.json`.

---

## Schedule B — Fee Schedule and In-Kind Value Exchange

### B.1 Fee Tiers

| Tier | Endpoint Surface | SLA | Rate Limit | Commercial Rate | NLR Rate |
|---|---|---|---|---|---|
| **Tier 0 — Research Seed** *(FY 2026 only)* | All Schedule A | 95% | 50 req/sec, 1M req/month | $30,000/yr | **$3,000/yr** |
| **Tier 1 — Research** *(FY 2027 default)* | All Schedule A | 95% | 50 req/sec, 1M req/month | $100,000/yr | **$10,000/yr** |
| **Tier 2 — Research Plus** *(optional)* | All Schedule A + bulk exports | 99% | 200 req/sec, 10M req/month | $250,000/yr | **$25,000/yr** |

### B.2 Tier 0 — FY 2026 Only
Tier 0 is an introductory rate available solely for the first twelve (12) months following the Effective Date. Year 2 automatically transitions to Tier 1 at the then-current Tier 1 NLR rate, subject to the renewal-fee-adjustment mechanism in Section 4.3 of the main MOU. The Stripe Payment Link at `https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e` reflects the Tier 0 rate.

### B.3 NLR Discount Rationale
The NLR discount ratio (approximately ninety percent (90%) off commercial rates) reflects strategic value to DC Hub in (a) the joint validation study output produced under Stream B, (b) the open-method tooling produced under Stream C, (c) factual reference rights under Article XII, and (d) DC Hub's cost-of-acquisition reduction for the broader federally funded research and development center ecosystem.

### B.4 Invoicing
Tier 0 fees are payable via Stripe at the Payment Link in Section B.2. Tier 1 and Tier 2 fees are payable by NLR purchase order, net thirty (30) days from DC Hub invoice receipt. NLR shall provide the procurement contact and billing contact within thirty (30) days of execution.

### B.5 In-Kind Value Exchange (Material Consideration)

The Parties acknowledge that the discounted NLR fees set out above reflect material in-kind consideration flowing from NLR to DC Hub, including without limitation:

- Co-authorship rights on the Stream B validation study (Article IX)
- Factual reference rights under Article XII
- Joint conference and workshop presence at mutually agreed venues
- First-look access to reVeal v2 outputs and documentation when produced
- Tier 2 only: quarterly methodology-sync calls
- Reputational and institutional value flowing from association with NLR's research program

The Parties further agree that the in-kind consideration is integral to the commercial structure of this MOU and that neither Party will characterize the relationship as a one-sided commercial sale or one-sided NLR grant.

---

## Schedule C — Service Levels and Rate Limits

### C.1 Service Level Agreement (SLA)
- **Tier 0 / Tier 1:** ninety-five percent (95%) monthly availability for the Schedule A endpoint surface, measured as `(uptime_minutes / total_minutes_in_month)`.
- **Tier 2:** ninety-nine percent (99%) monthly availability.

### C.2 Rate Limits
- **Tier 0 / Tier 1:** fifty (50) requests per second sustained; 1,000,000 (one million) requests per month aggregate.
- **Tier 2:** two hundred (200) requests per second sustained; 10,000,000 (ten million) requests per month aggregate; four (4) full-United-States grid-export operations per month.

### C.3 Support
- **Tier 0 / Tier 1:** business-day email support; standard issue response within one (1) business day.
- **Tier 2:** four (4)-hour priority response for production-impacting issues.

### C.4 Key Compromise
Compromised keys are revoked and re-issued within one (1) business hour of NLR's written notice to DC Hub. NLR may revoke keys at any time via written request.

---

## Schedule D — Attribution Language

### D.1 Canonical Form
> *"Data provided by DC Hub (dchub.cloud) under a research license to [NLR OPERATING ENTITY LEGAL NAME]."*

### D.2 Short Form (where space is limited, e.g., chart labels)
> *"Source: DC Hub (dchub.cloud)"*

### D.3 Citation Form for Peer-Reviewed Publications
> *Martone, J. et al. "DC Hub: Real-time data-center operational intelligence platform." dchub.cloud, [YEAR].*

### D.4 No Endorsement Disclaimer
Where required by NLR's institutional review or DOE policy, attribution may be accompanied by:
> *"This citation is factual reference only and does not constitute NLR endorsement of any DC Hub commercial product or service."*

### D.5 Domain Note
The canonical domain is `dchub.cloud`. The `dchub.com` domain is not operated by DC Hub.

---

## Schedule E — Data Dictionary and Provenance

**E.1 Delivery.** DC Hub shall deliver a per-endpoint Data Dictionary to NLR within sixty (60) days of the Effective Date. The Data Dictionary documents, for each Schedule A endpoint: (a) the schema of the response, (b) the upstream data source(s) (FERC / ISO / EIA / EPA / USGS / HIFLD / NLR's own NREL data API / DC Hub-derived), (c) the refresh cadence, and (d) known limitations.

**E.2 Updates.** Material changes to the Data Dictionary follow the change-management terms in **Schedule G**.

**E.3 Format.** Data Dictionary delivered as machine-readable JSON Schema files plus a human-readable Markdown index, hosted at `dchub.cloud/openapi.json` and a companion `dchub.cloud/datadictionary` page (to be created).

---

## Schedule F — Security Posture and Incident Response

**F.1 Current Operational Security.**
- TLS 1.2+ on all production endpoints
- AES-256 encryption at rest (Cloudflare R2, Railway Postgres, Neon Postgres)
- NIST 800-53 framework alignment (control-mapping documentation available on request)
- Cloud-native deployment on Cloudflare, Railway, and Neon
- API key rotation supported

**F.2 Third-Party Certifications.** As of the Effective Date, DC Hub holds no active SOC 2, FedRAMP, ISO 27001, or comparable third-party certification. DC Hub does not represent or warrant any such certification.

**F.3 Future Certification Commitment.** DC Hub commits to initiate a SOC 2 Type 1 audit cycle within FY 2027 if NLR's publication venue or institutional review process requires third-party certification of DC Hub security posture. Cost of the audit cycle is borne by DC Hub from Year-2 Tier 1 / Tier 2 fees.

**F.4 Incident Notification.** DC Hub notifies NLR's designated security contact within seventy-two (72) hours of confirmed compromise affecting NLR Confidential Information. Notification includes the affected data, suspected scope, and mitigation status.

**F.5 NLR Security Contact.** [NLR TO PROVIDE WITHIN 30 DAYS OF EXECUTION]

---

## Schedule G — Change Management and Deprecation

**G.1 Breaking-Change Notice.** Ninety (90) days minimum advance written notice via email to JSC technical lead.

**G.2 Deprecation Notice.** One hundred eighty (180) days minimum advance written notice for full endpoint deprecation.

**G.3 Cited-Endpoint Stability.** For any endpoint expressly cited in a peer-reviewed publication produced under Stream B, no breaking change may be made within twelve (12) months following journal submission date.

**G.4 Server-Side Alias Maintenance.** DC Hub maintains aliases such that the following endpoint pairs both resolve to the same content:
- `/api/v1/grid-data` and `/api/v1/grid/data`
- `/api/v1/energy-prices` and `/api/v1/energy/retail`
- `/api/v1/renewable-energy` and `/api/v1/energy/renewable`
- `/api/v1/water-risk` and `/api/v1/water/stress`
- `/api/v1/fiber-intel` and `/api/v1/fiber/intel`

Aliases shipped within sixty (60) days of Effective Date.

**G.5 Change Log.** DC Hub maintains a public change log at `dchub.cloud/changelog`. Material changes to Schedule A endpoints are summarized in the change log within five (5) business days of deployment.

---

## Schedule H — Acceptable Use Policy

**H.1 Permitted.** Non-commercial research, academic publication, methodology development, internal NLR staff training, conference and workshop presentations subject to the citation requirements of Schedule D.

**H.2 Prohibited.** Commercial redistribution of bulk data; training of machine-learning models that would functionally substitute for a DC Hub subscription; paid third-party consulting opinions on specific data-center siting decisions made primarily on the basis of the licensed data; re-licensing or resale of the data.

**H.3 Caching and Storage.** NLR may cache and store the licensed data for the duration of the engagement and for the duration of any peer-reviewed publication's data-availability window required by the publication venue.

**H.4 Sharing with Third-Party Collaborators.** NLR may share derived analytical output (not raw DC Hub data) with academic collaborators subject to the citation requirements of Schedule D and the confidentiality obligations of Article X.

**H.5 Audit.** DC Hub may, on thirty (30) days' written notice and no more than once per calendar year, request usage reports sufficient to confirm Acceptable Use compliance. NLR's usage reports may redact NLR Confidential Information.

---

**[END OF MEMORANDUM OF UNDERSTANDING — VERSION 1 DRAFT — 2026-05-26]**

---

## Internal note (not part of executed MOU)

This document consolidates the prior 4-PDF agreement package (Overview, MOU Framework, Research Data License v2, Publication Protocol) into a single executable instrument. All twelve (12) DC Hub-side redline items from the prior counter-proposal package (`docs/NLR_LEGAL_REDLINE_NOTES.md`) are incorporated:

| # | Original redline | Where incorporated |
|---|---|---|
| A1 | Tier 0 Research Seed row at $3K | Article VI Section 6.2; Schedule B § B.1, B.2 |
| A2 | `dchub.com` → `dchub.cloud` | Article VIII; Schedule D § D.1, D.5 |
| A3 | Strip "in development" from A.5 endpoints | Schedule A § A.5 ("all live as of Effective Date") |
| A4 | Endpoint path alignment via aliases | Article XIV Section 14.4; Schedule G § G.4 |
| B1 | NLR operating entity legal name | Article I § 1.1 ([BRACKETED]) |
| B2 | DOE prime contract number | Article I § 1.1 ([BRACKETED]); Article IX § 9.4 |
| B3 | NLR signatory + governing law | Signatures block; Article XX § 20.1 ([BRACKETED]) |
| B4 | NLR security / billing / PO contacts | Schedule B § B.4; Schedule F § F.5 |
| C1 | Tier 1 election with upgrade path | Article VI § 6.1, 6.3, 6.4 |
| C2 | Honest NIST 800-53 framing, no false certs | Article XIII; Schedule F § F.1, F.2, F.3 |
| C3 | DC Hub legal entity (Martone Advisors LLC) | Article I § 1.1 |
| C4 | Counsel engagement protocol | (Out of MOU scope — handled outside the document) |

**Plus:** Article XII Section 12.4 adds DC Hub's commitment that the `dchub.cloud/partners/nlr` public landing page is subject to NLR JSC approval before substantive content is published. This was the additional Article-VII-style concession surfaced during pre-execution review on 2026-05-26.
