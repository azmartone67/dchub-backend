# DC Hub — Data Licence

**This file is the authoritative statement of what you may do with DC Hub data.**
Last reviewed 2026-08-10.

Until today DC Hub said three different things about its own licence, in three
places, and they did not agree:

| surface | said |
|---|---|
| `dchub.cloud/terms` §4.3, §5.2, §6 | no redistribution, no derivative databases, no ML training, no selling or licensing "in any form" |
| `dchub-openapi.json` `info.license` | `Proprietary` |
| every API response, `provenance.license` | `CC-BY-4.0` |

That is not a drafting nit. An analyst who wants to cite us reads `/terms`,
finds a flat prohibition, and stops — while the API they just called told them
the data was CC-BY. This file replaces the guesswork with one answer, and the
answer is per-layer, because a single answer across all four layers would
necessarily be false about at least one of them.

**Rule of thumb:** what DC Hub *computed* is CC-BY-4.0 and we want it cited.
What DC Hub *collected* carries its upstream's terms and is not ours to
relicense.

---

## 1. DCPI — scores, verdicts and methodology → **CC-BY-4.0**

Covers the DC Hub Power Index in full: `excess_power_score`, `constraint_score`,
`composite_score`, the BUILD / CAUTION / AVOID verdicts and their band
thresholds, verdict multipliers, `time_to_power_months`, per-market rankings,
and the published methodology including the weighting and the fail-open
behaviour we document against it.

Licensed under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).

You may republish it, chart it, build on it, put it in a client report, or
train on it. You must attribute. **We would rather be cited than paid for
this layer** — it is an index, and an index that nobody may quote is worthless.

This is our own derived analytical work: the inputs are public, the transform
is ours, and it is reproducible from published inputs by a third party who has
no access to our database. Nothing upstream restricts our grant here.

**Cite as:**

> DC Hub Power Index (DCPI), DC Hub, dchub.cloud, CC-BY-4.0. Retrieved <date>.

Include the method version when you quote a score — DCPI is recomputed daily
and the bands are versioned (`method_version`, e.g. `2.3.0`), so a score
without a version and a date is not reproducible.

## 2. Grid analysis derived from public system-operator telemetry → **CC-BY-4.0 for our layer only**

Our normalisation, per-region rankings, headroom and constraint analysis are
ours and are CC-BY-4.0 on the same terms as §1.

The **underlying operator readings** are not ours to relicense. Each system
operator sets its own terms (§4). If you want the raw feed, take it from the
operator — we name every one of them, and several are one HTTP call away.

## 3. Facility inventory → **no redistribution grant. Under review.**

The facility corpus is a composite: PeeringDB, OpenStreetMap, operator
disclosure, regulatory filings, press releases, third-party directories, and
our own curation and de-duplication.

**DC Hub does not currently grant redistribution rights over this corpus**, and
we are not going to pretend otherwise while a provenance review is open. Two
honest reasons:

1. Parts of it come from compilations whose terms we have not confirmed in
   writing. Individual facts are not protectable; a **compilation** generally
   is. `robots.txt` does not resolve this — `Allow: /` says a crawler may
   fetch a page, and grants nothing about reusing a compiled directory.
2. OpenStreetMap is **ODbL 1.0**, which is share-alike. That obligation would
   pass to anyone we granted redistribution to, and it is not ours to waive.

You may **read, query and cite** the inventory through the product and the API,
including in published work with attribution. You may not take the corpus and
republish it as a dataset. Per-source status is tracked in
`docs/DATA-PROVENANCE.md`; sources clear individually, not all at once.

## 4. Third-party layers → **each upstream's terms**

Named so that anyone can go to the source directly, and so that the
attribution ODbL and PeeringDB require is actually discharged. Also published
at [dchub.cloud/data-sources](https://dchub.cloud/data-sources).

### Facility inventory

| source | licence / terms | note |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) (via Overpass) | **ODbL 1.0** — attribution **and share-alike** | © OpenStreetMap contributors |
| [PeeringDB](https://www.peeringdb.com/) | attribution required | interconnection facilities |
| [Wikidata](https://www.wikidata.org/) | CC0 | small number of records |
| Operator disclosure / operator websites | operator's own terms | publicly published capacity + siting |
| SEC filings, press releases, trade press | public record / fair reporting | entity + deal extraction |
| Third-party directories | **terms unconfirmed — held from redistribution** | see §3, `docs/DATA-PROVENANCE.md` |

### Grid telemetry

| region | operator |
|---|---|
| United States (7 ISOs: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE) | [EIA-930](https://www.eia.gov/electricity/gridmonitor/) — US federal, public domain |
| Great Britain | NESO / [Elexon](https://www.elexon.co.uk/) |
| European Union (~24 bidding zones) | [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) — registration-gated |
| Taiwan | [Taipower](https://www.taipower.com.tw/) |
| Japan | [OCCTO](https://www.occto.or.jp/) |
| South Korea | [KPX](https://www.kpx.or.kr/) |
| Brazil | [ONS](https://www.ons.org.br/) |
| Australia (partial) | [AEMO](https://aemo.com.au/) |
| Singapore (partial) | EMA |

### Physical layers

| layer | source | licence |
|---|---|---|
| US power plants, generation, retail rates | [EIA](https://www.eia.gov/) | US federal, public domain |
| Substations, transmission lines | [HIFLD](https://hifld-geoplatform.hub.arcgis.com/) | US federal, public domain |
| Carrier / fiber facilities | [FCC](https://www.fcc.gov/) | US federal, public domain |
| Natural-hazard risk | [FEMA](https://www.fema.gov/) (NRI) | US federal, public domain |
| Air permits | [EPA ECHO](https://echo.epa.gov/) | US federal, public domain |
| Global power + gas infrastructure | [Global Energy Monitor](https://globalenergymonitor.org/) | **CC-BY 4.0** |
| Submarine cables + landing points | [TeleGeography Submarine Cable Map](https://www.submarinecablemap.com/) public API | **terms unconfirmed** — see note |

> **Submarine cables — being corrected.** An internal note previously recorded
> this as a proprietary purchased dataset. It is not: we ingest
> `submarinecablemap.com/api/v3`, TeleGeography's public JSON API, also mirrored
> in their public GitHub repository. That is materially lower-risk than
> "proprietary", but a public API is still not an express licence grant, so it
> stays marked unconfirmed until TeleGeography confirms terms in writing.

---

## 5. How this relates to `dchub.cloud/terms`

`/terms` governs your **use of the Service** — accounts, rate limits, scraping
our endpoints, reselling *access*. It continues to apply in full.

This file governs the **licence in the data**. The CC-BY-4.0 grant in §1 and §2
is an **express exception** to the redistribution language in `/terms` §5.2, and
it is granted by the same rights-holder that wrote `/terms`. Where the two
appear to conflict about what you may do with a **DCPI score or our published
methodology**, this file governs.

Two further points `/terms` gets wrong today, and which the pending amendment
fixes:

- **"Use DC Hub data to train machine learning models" (§5.2)** sits directly
  against DC Hub positioning itself as the live data layer for AI agents. An
  agent developer who reads our terms is told not to use us for the thing we
  advertise. Training on the §1 CC-BY layer is **permitted**.
- **§5.2 "Sell or license DC Hub data in any form"** prohibits, on its face,
  DC Hub's own enterprise data-licence export. A prohibition the operator
  itself does not follow protects nobody.

## 6. If you want more than this file grants

Redistribution of the facility corpus (§3) needs a written agreement, because
it needs per-source clearance we do not yet have across the board. Ask:
**info@dchub.cloud**. A citation partnership needs no agreement at all — §1 and
§2 already permit it, and we would like you to take us up on it.

---

*Corrections to this file are welcome and will be treated as bug reports. If a
source is named wrongly, or a licence is stated wrongly, that is a defect.*
