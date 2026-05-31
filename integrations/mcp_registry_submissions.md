# DC Hub — MCP Registry Submission Packet (STAGED — owner review required)

**Status:** 🟡 STAGED. Nothing here has been submitted. No external PRs opened, no
accounts created, no forms filled. This file is a copy-paste-ready packet for when
the owner (Jonathan / azmartone@gmail.com) greenlights external submission.

**Authored:** 2026-05-31. Verified against the live, canonical source files:
- `/.well-known/mcp.json` (live, HTTP 200) → 25 tools, transport `streamable-http`
- `server.json` (repo root, used by `mcp-publisher`) → name `cloud.dchub/mcp-server`, v2.2.0
- `/llms.txt` (live, HTTP 200), `/.well-known/glama.json` (live, HTTP 200)
- `/mcp` (live, HTTP 405 on GET — correct; it's a POST streamable-http endpoint)

> ⚠️ **Numbers drift.** The stat counts below (facilities, M&A $, pipeline GW, AI
> platforms) are the figures used in current site copy. Before pasting any
> submission, re-confirm the live tool count and headline stats so we don't ship a
> stale claim:
> ```bash
> curl -s https://dchub.cloud/.well-known/mcp.json \
>   | python3 -c "import json,sys; d=json.load(sys.stdin); print('tools:', len(d['tools']))"
> # expect 25
> ```
> Older staged drafts (`REGISTRY_SUBMISSIONS.md`, `PATCHES/REGISTRY_SUBMISSIONS_r45/`)
> claimed "23+" / "29" tools and the legacy server name `dchub-mcp-server`. Those are
> SUPERSEDED by this file. The live manifest has exactly **25** tools and the
> registry-canonical name is **`cloud.dchub/mcp-server`**.

---

## 0. Canonical metadata (single source of truth — paste into every registry)

```
Registry name (reverse-DNS):  cloud.dchub/mcp-server
Display name:                 DC Hub
Title:                        DC Hub — Data Center & Energy Intelligence
Version:                      2.2.0
MCP endpoint:                 https://dchub.cloud/mcp
Transport:                    streamable-http
Manifest (well-known):        https://dchub.cloud/.well-known/mcp.json
Homepage:                     https://dchub.cloud
llms.txt:                     https://dchub.cloud/llms.txt
Server card (live JSON):      https://dchub.cloud/.well-known/mcp/server-card.json
GitHub repo:                  https://github.com/azmartone67/dchub-backend
Contact:                      api@dchub.cloud
Operator:                     DC Hub (azmartone@gmail.com)
License note:                 Free for AI citation. Data subject to https://dchub.cloud/terms
Tool count:                   25
```

**One-line description (≤160 chars, for compact fields):**
```
Data-center + energy intelligence for AI agents: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A.
```

**Short description (~300 chars, for tweet/bio fields):**
```
DC Hub is the MCP server for data-center & energy intelligence. 25 tools over 21,000+ facilities, the DC Hub Power Index (DCPI) + DC Hub Gas Index (DCGI), live ISO grid telemetry, fiber routes, and M&A deal history. For site selection, market analysis, and AI-load siting.
```

**Long description (use when the field allows >500 chars):**
```
DC Hub is a data-center and energy intelligence MCP server for AI agents. It exposes
25 tools covering 21,000+ global data-center facilities, the proprietary DC Hub Power
Index (DCPI) and DC Hub Gas Index (DCGI) for ranking markets, live ISO grid telemetry
(PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE and more), fiber-route and carrier
intelligence, energy and tax-incentive data, water/drought risk, interconnection-queue
headroom, and data-center M&A transaction history. Agents use it for site selection,
greenfield site scoring, market analysis, capacity/pipeline tracking, grid-risk and
carbon modeling, and AI-load siting. Free anonymous tier; optional X-API-Key header
unlocks full data and higher rate limits.
```

**Tags / keywords:**
```
data-center, datacenter, infrastructure, energy, grid, iso, dcpi, dcgi,
power-markets, site-selection, renewable, m-and-a, fiber, real-estate,
ai-infrastructure, interconnection-queue, intelligence
```

**Categories (map to whatever each registry offers):**
```
Data & APIs · Infrastructure · Energy · Research · Finance · Location Services
```

**The 25 tools (verbatim from the live manifest — use where a tool list is asked):**
```
search_facilities, get_facility, list_transactions, get_market_intel, get_news,
analyze_site, get_grid_data, get_pipeline, get_infrastructure, get_fiber_intel,
get_energy_prices, get_renewable_energy, get_agent_registry, get_intelligence_index,
get_dchub_recommendation, get_tax_incentives, compare_sites, get_water_risk,
get_backup_status, get_grid_intelligence, get_geothermal_potential,
get_colocation_score, get_grid_headroom, get_microgrid_viability, get_air_permitting
```

**Auth (from the live manifest):** optional `X-API-Key` header. Anonymous = limited
free tier (3 results/query, basic fields). Developer $49/mo, Pro $199/mo, Enterprise
$699/mo unlock full data + higher daily call caps. Key signup: https://dchub.cloud/pricing#developer

**Disambiguation (include if a notes/anti-confusion field exists):**
```
DC Hub (dchub.cloud) tracks PHYSICAL data-center facilities, power, and energy
infrastructure. It is NOT DataHub / DataHub Cloud / Azure Data Hub or any
metadata/data-catalog product.
```

---

## Per-registry summary

| # | Registry | Listing status | Submission type | Owner action needed |
|---|----------|----------------|-----------------|---------------------|
| 1 | **lobehub** MCP marketplace | 🆕 NEW | Web "Submit" modal → GitHub-issue fallback | GitHub login (for issue fallback) |
| 2 | **mcphub** (mcphub.io) | 🆕 NEW | Web form on site (`/submit`); verify live | Site account may be required |
| 3 | **toolhive** (Stacklok) | 🆕 NEW | GitHub PR — add `server.json` to registry | GitHub PR + maintainer approval |
| 4 | **mcp.so** | 🔁 VERIFY/UPDATE (per notes) | Comment on GitHub issue `chatmcp/mcpso#1` | GitHub login |
| 5 | **glama** | 🔁 VERIFY/UPDATE (already indexed) | Auto-index from GitHub + `glama.json` claim | GitHub login OR email-match claim |
| 6 | **smithery** | 🔁 VERIFY/UPDATE (already listed) | Web URL method at `smithery.ai/new` | GitHub login |
| 7 | **awesome-mcp-servers** (punkpeye) | 🆕 NEW | GitHub PR — add one README line | GitHub PR + maintainer approval |

---

## 1. lobehub MCP marketplace — 🆕 NEW

- **Submission URL (primary):** https://lobehub.com/mcp → top-right **"Submit"** button (modal).
- **Submission URL (fallback):** open a GitHub issue at https://github.com/lobehub/lobehub/issues
  using the "Add MCP server to marketplace" request template (the Submit modal has been
  flaky for some users; the issue is the reliable path).
- **Process:** LobeHub indexes from a public GitHub repo. It runs a quality checklist:
  repo must have a README, ≥1 install method, ≥1 tool, a LICENSE, and a friendly
  one-line install config. Our repo (`azmartone67/dchub-backend`) satisfies these; the
  install method is the hosted streamable-http URL.

### (a) Submit-modal form fields
```
Name:            DC Hub
Identifier:      cloud.dchub/mcp-server
GitHub repo URL: https://github.com/azmartone67/dchub-backend
Homepage:        https://dchub.cloud
Description:     Data-center + energy intelligence for AI agents: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A.
Category:        Data & APIs  (also tag: Infrastructure, Energy)
Tags:            data-center, energy, grid, iso, dcpi, dcgi, site-selection, m-and-a, fiber
```

### (b) Client-install config snippet to provide (LobeHub asks for the user-facing config)
```json
{
  "mcpServers": {
    "dchub": {
      "type": "http",
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "${DCHUB_API_KEY}" }
    }
  }
}
```

### (c) GitHub-issue fallback body (paste verbatim if the modal fails)
```markdown
### MCP Server Marketplace Submission

- **Server name:** DC Hub
- **Identifier:** cloud.dchub/mcp-server
- **Repository:** https://github.com/azmartone67/dchub-backend
- **Homepage:** https://dchub.cloud
- **MCP endpoint:** https://dchub.cloud/mcp  (transport: streamable-http)
- **Manifest:** https://dchub.cloud/.well-known/mcp.json
- **Tools:** 25
- **Description:** Data-center + energy intelligence for AI agents — 21,000+ facilities,
  the DC Hub Power Index (DCPI) + DC Hub Gas Index (DCGI), live ISO grid telemetry,
  fiber routes, and M&A deal history. For site selection, market analysis, and AI-load siting.
- **Auth:** Optional `X-API-Key` header. Free anonymous tier; paid tiers unlock full data.
- **License:** Free for AI citation; data per https://dchub.cloud/terms
- **Contact:** api@dchub.cloud
```

### 🔒 Owner manual steps
- Sign in to lobehub.com (GitHub OAuth) to use the Submit modal, **or** be logged in to
  GitHub to file the fallback issue.
- A maintainer/automated check approves; listing is not instant.

---

## 2. mcphub (mcphub.io) — 🆕 NEW

- **Submission URL:** https://mcphub.io/submit  (web form on the site).
  - ⚠️ **Verify before submitting:** mcphub.io is a Next.js single-page app
    (repo `MCP-Club/mcphub`) and the exact submit path/fields render client-side and
    could not be confirmed by static fetch. Open https://mcphub.io, find the **Submit /
    Add Server** control in the nav, and confirm whether it's an on-site form or links
    to a GitHub issue. If the path differs, update this section.
- **Process:** directory-style listing. Expected fields: name, description, URL,
  category, tags (typical for this directory).

### (a) Form fields
```
Name:          DC Hub
Server URL:    https://dchub.cloud/mcp
Transport:     streamable-http
Homepage:      https://dchub.cloud
Repository:    https://github.com/azmartone67/dchub-backend
Category:      Data & APIs / Infrastructure
Tags:          data-center, energy, grid, iso, dcpi, dcgi, site-selection, m-and-a, fiber
Description:   Data-center + energy intelligence for AI agents: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A. 25 tools. Optional X-API-Key; free anonymous tier.
Contact:       api@dchub.cloud
```

### 🔒 Owner manual steps
- May require a site account / email verification.
- Confirm the live submit mechanism (form vs GitHub issue) before pasting.

---

## 3. toolhive (Stacklok) — 🆕 NEW  *(highest-fidelity submission)*

- **Submission URL:** https://github.com/stacklok/toolhive-registry  (GitHub **Pull Request**).
- **Process (confirmed against the live repo):**
  1. Fork `stacklok/toolhive-registry`.
  2. Create directory `registries/toolhive/servers/dchub-remote/`.
  3. Add `server.json` (below) — for a hosted server, the dir uses the `-remote` suffix
     convention (cf. `cloudflare-remote`, `stripe-remote`).
  4. (Optional) add an `icon.svg` in the same dir and reference it via the `icons` field.
  5. Open a PR. A maintainer reviews for quality/security and merges.
- **Schema notes (verified against real remote entries):** uses the standard MCP
  `server.schema.json` (2025-12-11). The `_meta.…publisher-provided` block is keyed by
  the **publisher namespace** then by the **exact remote URL** (must match
  `remotes[0].url` byte-for-byte). `tier` = `Official` | `Community` (use **Community** —
  we are not Stacklok-official). `status` = `Active` | `Deprecated`.

### (a) EXACT file to commit — `registries/toolhive/servers/dchub-remote/server.json`
```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.stacklok/dchub-remote",
  "title": "DC Hub (Remote)",
  "description": "Data-center & energy intelligence: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A",
  "version": "2.2.0",
  "repository": {
    "url": "https://github.com/azmartone67/dchub-backend",
    "source": "github"
  },
  "remotes": [
    {
      "type": "streamable-http",
      "url": "https://dchub.cloud/mcp"
    }
  ],
  "_meta": {
    "io.modelcontextprotocol.registry/publisher-provided": {
      "io.github.stacklok": {
        "https://dchub.cloud/mcp": {
          "custom_metadata": {
            "author": "DC Hub",
            "homepage": "https://dchub.cloud",
            "contact": "api@dchub.cloud",
            "license": "Proprietary — free for AI citation; data per https://dchub.cloud/terms"
          },
          "overview": "## DC Hub (Remote)\n\nDC Hub is a data-center and energy intelligence MCP server for AI agents. It exposes 25 tools over 21,000+ global data-center facilities, the proprietary DC Hub Power Index (DCPI) and DC Hub Gas Index (DCGI) for ranking markets, live ISO grid telemetry (PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE and more), fiber-route and carrier intelligence, energy and tax-incentive data, water/drought risk, interconnection-queue headroom, and data-center M&A history. Used for site selection, greenfield site scoring, market analysis, capacity/pipeline tracking, grid-risk and carbon modeling, and AI-load siting. Streamable-HTTP transport with optional X-API-Key authentication; free anonymous tier.",
          "status": "Active",
          "tier": "Community",
          "tags": [
            "remote", "data-center", "energy", "grid", "iso", "dcpi", "dcgi",
            "site-selection", "m-and-a", "fiber", "renewable", "infrastructure",
            "interconnection-queue", "real-estate"
          ],
          "tools": [
            "search_facilities", "get_facility", "list_transactions", "get_market_intel",
            "get_news", "analyze_site", "get_grid_data", "get_pipeline", "get_infrastructure",
            "get_fiber_intel", "get_energy_prices", "get_renewable_energy", "get_agent_registry",
            "get_intelligence_index", "get_dchub_recommendation", "get_tax_incentives",
            "compare_sites", "get_water_risk", "get_backup_status", "get_grid_intelligence",
            "get_geothermal_potential", "get_colocation_score", "get_grid_headroom",
            "get_microgrid_viability", "get_air_permitting"
          ]
        }
      }
    }
  }
}
```

> **Naming caveat:** ToolHive's own remote entries are published under the
> `io.github.stacklok/<name>` namespace (the entries are curated by Stacklok, not the
> upstream vendor — see `stripe-remote`, `cloudflare-remote`). So our PR's `name` and
> the `_meta` publisher key use `io.github.stacklok` to match repo convention. Our
> *own* registry-canonical identity stays `cloud.dchub/mcp-server` (that's what the
> official MCP registry and `server.json` use). If a maintainer prefers a vendor
> namespace, switch both `name` and the publisher key to `cloud.dchub` and note it in
> the PR description.

### (b) PR title + body
```
Title: Add DC Hub (remote) — data-center & energy intelligence MCP server

Body:
Adds DC Hub, a hosted streamable-http MCP server for data-center and energy
intelligence (25 tools, 21,000+ facilities, DCPI/DCGI indices, live ISO grid data,
fiber, M&A).

- Endpoint: https://dchub.cloud/mcp  (streamable-http)
- Manifest: https://dchub.cloud/.well-known/mcp.json
- Homepage: https://dchub.cloud
- Auth: optional X-API-Key; free anonymous tier
- Tier: Community

Entry added at registries/toolhive/servers/dchub-remote/server.json.
```

### 🔒 Owner manual steps
- Fork + PR from a GitHub account (or authorize the existing `PR_SUBMIT_TOKEN` workflow,
  if used) — **do not push without owner greenlight**.
- Maintainer review/merge required (security + quality gate).
- Optional: add `icon.svg` (DC Hub logo) to the same directory before opening the PR.

---

## 4. mcp.so — 🔁 VERIFY / UPDATE  *(notes say we're already on mcp.so)*

- **Submission URL:** comment on the pinned GitHub issue
  **https://github.com/chatmcp/mcpso/issues/1** ("Submit Your MCP Servers here").
  The maintainer harvests links from this thread and publishes them to https://mcp.so.
  (Repo: `chatmcp/mcpso`. The issue body just says *"Leave your MCP Servers links. We
  will make it visible on https://mcp.so"* — no rigid template, so give a clean block.)
- **First: verify** whether DC Hub is already live: open
  https://mcp.so and search "DC Hub" / "dchub". If present, confirm the endpoint shows
  `https://dchub.cloud/mcp` and the description/tool count are current; if stale, post a
  short update comment on issue #1 asking to refresh the listing.

### (a) Comment to post on issue #1 (new listing OR refresh)
```markdown
**DC Hub** — Data-center & energy intelligence for AI agents

- MCP endpoint: https://dchub.cloud/mcp  (streamable-http)
- Manifest: https://dchub.cloud/.well-known/mcp.json
- Homepage: https://dchub.cloud
- GitHub: https://github.com/azmartone67/dchub-backend
- Tools: 25 — 21,000+ facilities, DC Hub Power Index (DCPI) + DC Hub Gas Index (DCGI),
  live ISO grid telemetry, fiber routes, M&A deal history.
- Auth: optional X-API-Key; free anonymous tier.
- Contact: api@dchub.cloud
```

### 🔒 Owner manual steps
- GitHub login to comment.
- If already listed, this is an UPDATE request (no duplicate); ask maintainer to refresh
  rather than re-add.

---

## 5. glama — 🔁 VERIFY / UPDATE  *(already indexed; claim via glama.json)*

- **How Glama works (confirmed):** Glama **auto-indexes** public GitHub MCP repos — no
  "submit" form is required for a repo that's already discoverable. Authors **claim** a
  listing to edit it, either by (a) authenticating with GitHub when the repo is under
  your personal account, or (b) committing a `glama.json` at the repo root with a
  `maintainers` email that matches the Glama account.
- **Current state:** a `glama.json` already exists in the frontend repo
  (`dchub-frontend/well-known/glama.json`) using the **connector** schema with
  `azmartone@gmail.com`. Confirm it's served live and matches the account email:
  ```bash
  curl -s https://dchub.cloud/.well-known/glama.json | python3 -m json.tool
  ```
- **Gap to check:** Glama also indexes the *code repo*. For the canonical claim of the
  server listing, place a `glama.json` at the **root of `azmartone67/dchub-backend`**
  too (server schema with the GitHub username), so the GitHub-repo listing is claimed,
  not just the connector. Verify on https://glama.ai/mcp/servers (search "dchub").

### (a) `glama.json` for the GitHub repo root (server-claim form)
```json
{
  "$schema": "https://glama.ai/mcp/schemas/server.json",
  "maintainers": ["azmartone67"]
}
```

### (b) Connector-claim form (already deployed; keep for reference)
```json
{
  "$schema": "https://glama.ai/mcp/schemas/connector.json",
  "maintainers": [{ "email": "azmartone@gmail.com" }]
}
```

### (c) After claiming, update these listing attributes in the Glama admin
```
Display name:  DC Hub
Description:   Data-center + energy intelligence for AI agents: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A. 25 tools, streamable-http, optional X-API-Key.
Homepage:      https://dchub.cloud
Endpoint:      https://dchub.cloud/mcp
Tags:          data-center, energy, grid, iso, dcpi, dcgi, site-selection, m-and-a, fiber
```

### 🔒 Owner manual steps
- Log in to glama.ai (GitHub OAuth, or email matching the connector `glama.json`).
- Click **Claim** on the DC Hub listing, then edit name/description/tags.
- (Optional but recommended) commit the server-schema `glama.json` to the backend repo
  root — **owner greenlight before committing** (this file says do not commit).

---

## 6. smithery — 🔁 VERIFY / UPDATE  *(already listed; URL method)*

- **Submission URL (URL method, confirmed):** https://smithery.ai/new → paste the
  server's public HTTPS URL → complete the publishing flow. No code deploy through
  Smithery is needed for an already-hosted streamable-http server; "any server exposing
  Streamable HTTP is compatible." No `smithery.yaml` or reverse-DNS name is required for
  the URL method.
- **First: verify** the existing listing at https://smithery.ai/server/… (search
  "DC Hub" / "dchub"). Confirm endpoint = `https://dchub.cloud/mcp`, tool count = 25,
  and description is current. Update via the listing's edit/claim if stale.

### (a) URL-method input
```
URL to paste at smithery.ai/new:   https://dchub.cloud/mcp
```

### (b) Listing metadata (fill/refresh on the Smithery listing page)
```
Name:          DC Hub
Description:   Data-center + energy intelligence for AI agents: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber, and M&A.
Homepage:      https://dchub.cloud
Categories:    Data & APIs / Infrastructure
Tags:          data-center, energy, grid, iso, dcpi, dcgi, site-selection, m-and-a, fiber
```

### (c) Auth note for Smithery scan
Smithery auto-generates a config/auth modal and prompts for credentials when a server
requires auth. DC Hub allows **anonymous** calls (limited free tier), so the scan
should succeed without a key. To expose full data, users add header `X-API-Key`. If the
Smithery flow asks how auth is supplied, the answer is: **HTTP header `X-API-Key`**
(Smithery's OAuth-centric flow may need a manual note that we use a header key, not OAuth).

### 🔒 Owner manual steps
- Log in to smithery.ai (GitHub OAuth) to publish/claim.
- If a duplicate already exists, claim/update rather than create a second listing.

---

## 7. awesome-mcp-servers (punkpeye) — 🆕 NEW

- **Submission URL:** https://github.com/punkpeye/awesome-mcp-servers  (GitHub **PR**
  adding ONE line to `README.md`).
- **Process (confirmed against CONTRIBUTING + README legend):**
  - One server per line, linked to its repo, with a concise description.
  - Maintain **alphabetical order** within the chosen category section.
  - Append the legend emoji(s) after the repo link.
  - The maintainer also asks contributors to **claim the server on
    https://glama.ai/mcp/servers** (covered in §5 above).
- **Legend codes (verbatim from the repo):** language — `🐍` Python, `📇`
  TypeScript/JS, `🏎️` Go, `🦀` Rust, `#️⃣` C#, `☕` Java, `🌊` C/C++, `💎` Ruby;
  scope — `☁️` Cloud Service, `🏠` Local Service, `📟` Embedded; OS — `🍎` macOS,
  `🪟` Windows, `🐧` Linux.
- **DC Hub codes:** `🐍` (Flask/Python backend) + `☁️` (hosted cloud service). OS emojis
  are omitted for cloud/remote servers (cf. the Discogs example line, which is `📇 ☁️`).
- **Category:** best fit is **`🔎 Search & Data Extraction`** (a data/intel API). Close
  alternatives the README offers: **`💰 Finance & Fintech`** and **`🗺️ Location
  Services`**. Pick one section; place the line in correct alphabetical order under it.

### (a) EXACT README line to add (under `🔎 Search & Data Extraction`, alphabetized)
```markdown
- [DC Hub](https://github.com/azmartone67/dchub-backend) 🐍 ☁️ - Data-center & energy intelligence: 21,000+ facilities, DCPI/DCGI indices, live ISO grid telemetry, fiber routes, and M&A — for site selection, market analysis, and AI-load siting.
```
> Format mirrors the repo's canonical example line verbatim in structure:
> `` `[cswkim/discogs-mcp-server](https://github.com/cswkim/discogs-mcp-server) 📇 ☁️ - MCP server to interact with the Discogs API` ``

### (b) PR title + body
```
Title: Add DC Hub — data-center & energy intelligence MCP server

Body:
Adds DC Hub under "Search & Data Extraction". DC Hub is a hosted streamable-http MCP
server (25 tools) for data-center and energy intelligence: 21,000+ facilities, the DC
Hub Power Index (DCPI) + DC Hub Gas Index (DCGI), live ISO grid telemetry, fiber routes,
and M&A deal history.

- Repo: https://github.com/azmartone67/dchub-backend
- Endpoint: https://dchub.cloud/mcp (streamable-http)
- Homepage: https://dchub.cloud

Claimed on Glama: (link once §5 claim is done)
```

### 🔒 Owner manual steps
- Fork + PR from a GitHub account — **do not push without owner greenlight**.
- Maintainer review/merge required.
- Before/at PR time, claim the server on https://glama.ai/mcp/servers (see §5) — the
  maintainer expects this.
- Re-check exact alphabetical neighbors at submit time (the list changes constantly).

---

## After any submission lands (existing internal ledger hooks)

Once a registry confirms a listing, refresh the L23 lifecycle-audit ledger so
`registry_presence` reflects reality (these are READ/POST to our own admin API, safe to
run after the owner has actually submitted — not part of staging):

```bash
# Refresh the outreach_submissions ledger the lifecycle audit cross-references
curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit

# Optional: always-fresh draft generator (pulls live server-card stats)
curl -s -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/draft-submissions | jq
```

And log each confirmed listing in `ai_citations` (template — fill real listing URLs):
```sql
INSERT INTO ai_citations
  (engine, platform, query, cited_url, citation_type, dchub_cited, dchub_position, source, observed_at)
VALUES
  ('lobehub',  'lobehub',  'mcp marketplace listing', '<listing-url>', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('mcphub',   'mcphub',   'mcp directory listing',   '<listing-url>', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('toolhive', 'toolhive', 'mcp registry PR',         '<pr-url>',      'registry_pr',      true, 1, 'manual_submission', NOW()),
  ('mcp.so',   'mcp.so',   'mcp directory listing',   '<listing-url>', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('glama',    'glama',    'mcp registry listing',    '<listing-url>', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('smithery', 'smithery', 'mcp registry listing',    '<listing-url>', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('awesome-mcp-servers', 'github', 'awesome list PR', '<pr-url>',     'registry_pr',      true, 1, 'manual_submission', NOW());
```

---

## Pre-flight checklist before the owner submits anything

- [ ] Re-run the tool-count check (expect 25); update counts if the manifest changed.
- [ ] Re-confirm headline stats in site copy (facilities, M&A $, pipeline GW) and edit §0.
- [ ] Confirm `https://dchub.cloud/mcp` answers a real MCP `initialize` (POST), not just 405-on-GET.
- [ ] For toolhive + awesome-mcp PRs: confirm the target file path / alphabetical slot at submit time.
- [ ] For glama + smithery + mcp.so: VERIFY the existing listing first; UPDATE, don't duplicate.
- [ ] Owner is logged in to the relevant GitHub / registry accounts.
