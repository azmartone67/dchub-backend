# DC Hub — Action Items Walkthrough (2026-05-25)

Single source of truth for every outstanding action item after
r51 / r52 / r53 / r54. Items grouped by where YOU need to act vs
what the platform now does autonomously.

---

## ✅ Same-week wins — Token / env rotations (5 min each)

Each rotation has a verifier call so you can confirm the fix landed.
**Hit `/api/v1/admin/integrations/health` after each one** — broken
items become healthy, you move on.

### 1. REFRESH_SECRET — fixes /daily stuck at 2026-03-31

**The problem**: `/daily` snapshot has been frozen since March because
the heroic-reprieve `/refresh` endpoint returns HTTP 401 — the
GH Actions secret doesn't match the Railway env var.

**Steps:**

1. Open both Railway services:
   - **resourceful-essence** → Variables: https://railway.com/project/8b33570c-80fa-4869-8de6-dd62899a0eb2/service/f6198b88-799d-4b60-8cc8-069f3552fc99/variables
   - **heroic-reprieve** → its own Variables page (DCHub Daily service)

2. Pick a strong shared secret (e.g. `openssl rand -hex 32`).

3. Set `REFRESH_SECRET=<value>` in:
   - heroic-reprieve Railway env
   - GitHub repo secret: https://github.com/azmartone67/dchub-backend/settings/secrets/actions → `REFRESH_SECRET`
   - (Optional) resourceful-essence Railway env if any code there reads it

4. **Verify**:
   ```bash
   curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
     https://dchub.cloud/api/v1/admin/integrations/health?only=refresh_secret
   ```
   Expect `"ok": true, "status": 200`.

5. Manually trigger the cron once to repopulate:
   ```bash
   gh workflow run daily-r2-render.yml
   ```
   `/daily` should refresh within ~2 minutes.

### 2. LINKEDIN_ACCESS_TOKEN — fixes LinkedIn auto-publishing

LinkedIn tokens expire every 60 days. Currently returns 401 on
`/v2/userinfo` checks.

**Steps:**

1. Visit https://dchub.cloud/api/linkedin/auth — completes the
   OAuth flow with your LinkedIn login.

2. Once redirected back, the token is auto-saved to DB. Also
   set `LINKEDIN_ACCESS_TOKEN=<value>` in resourceful-essence Railway env
   so cron retries can read it as fallback.

3. **Verify**:
   ```bash
   curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
     https://dchub.cloud/api/v1/admin/integrations/health?only=linkedin
   ```

### 3. TWITTER_BEARER_TOKEN — fixes X/Twitter auto-publishing

**Steps:**

1. Go to https://developer.twitter.com/en/portal/dashboard.

2. Under your dchub project → Keys & Tokens → regenerate Bearer Token.

3. Set `TWITTER_BEARER_TOKEN=<value>` in resourceful-essence Railway env.

4. **Verify**:
   ```bash
   curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
     https://dchub.cloud/api/v1/admin/integrations/health?only=twitter
   ```

### 4. BLUESKY_HANDLE + BLUESKY_APP_PASSWORD — activates Bluesky

Currently DARK. To enable:

1. Sign up at https://bsky.app — choose handle (e.g. `dchub.bsky.social`).

2. Settings → App Passwords → create one for DC Hub publishing.

3. Set in resourceful-essence Railway env:
   - `BLUESKY_HANDLE=dchub.bsky.social`
   - `BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`

4. **Verify**:
   ```bash
   curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
     https://dchub.cloud/api/v1/admin/integrations/health?only=bluesky
   ```

### 5. ERCOT_API_KEY — fixes ERCOT grid telemetry

**Steps:**

1. Sign in at https://apiexplorer.ercot.com.

2. Subscribe to the public-reports API; copy your subscription key.

3. Set `ERCOT_API_KEY=<value>` in resourceful-essence Railway env.

4. **Verify**:
   ```bash
   curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
     https://dchub.cloud/api/v1/admin/integrations/health?only=ercot
   ```

### 6. PR_SUBMIT_TOKEN (GH Actions secret) — enables awesome-mcp auto-PR

**Steps:**

1. Create a fine-grained PAT at https://github.com/settings/tokens?type=beta with:
   - Repository access: `dchub-cloud-bot/awesome-mcp-servers` (your fork)
   - Permissions: Contents (R/W), Pull requests (R/W)

2. Set as GH Actions secret: https://github.com/azmartone67/dchub-backend/settings/secrets/actions → `PR_SUBMIT_TOKEN`.

3. The weekly cron (Sun 04:17 UTC) auto-runs next; OR fire manually:
   ```bash
   gh workflow run awesome-mcp-pr.yml
   ```

### **Run all checks at once**

```bash
curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/integrations/health | jq
```

Output shows healthy/broken/missing for all 7 integrations in one view.

---

## ✅ Same-week ops — Content actions (10 min each)

### A. LinkedIn partners post — fire from draft

Draft is at `PATCHES/LINKEDIN_PARTNERS_POST.md` with long + short variants.

**Easiest path** — use the helper script:

```bash
export DCHUB_ADMIN_KEY=<your-key>

# Long variant (1,400 chars, recommended)
bash scripts/publish_partners_post.sh

# Short variant (700 chars)
bash scripts/publish_partners_post.sh --short

# Dry run (print, don't post)
bash scripts/publish_partners_post.sh --dry-run
```

OR copy-paste manually from the draft at:
https://www.linkedin.com/company/dchub/admin/content/

### B. Lightcone MCP gateway PR

Draft at `PATCHES/LIGHTCONE_MCP_GATEWAY_PR.md`.

```bash
gh repo fork lightconetech/mcp-gateway --clone --remote
cd mcp-gateway

# Edit servers.json or equivalent (see draft for exact entry)
git checkout -b add-dchub-mcp
git add servers.json
git commit -m "Add DC Hub — data-center intelligence MCP server"
git push -u origin add-dchub-mcp
gh pr create --title "Add DC Hub — data-center intelligence MCP server" \
              --body-file PATCHES/LIGHTCONE_MCP_GATEWAY_PR.md
```

### C. Other 6 registry submissions

Drafts at `PATCHES/REGISTRY_SUBMISSIONS_r45/`:

| Registry | File | Method |
|----------|------|--------|
| MCPHub | `mcphub.md` | Web form: https://mcphub.io/submit |
| Lobehub | `lobehub.md` | https://lobehub.com/mcp/submit |
| MCP Hive | `mcp-hive.md` | https://mcphive.com/submit |
| ToolHive | `toolhive.md` | https://toolhive.io/submit |
| Yellowmcp | `yellowmcp.md` | https://yellowmcp.com/submit |
| Anthropic Directory | `anthropic-directory.md` | Email sales |
| awesome-mcp-servers | `awesome-mcp-servers.md` | **Auto via cron** (needs PR_SUBMIT_TOKEN) |

For each: open the URL, copy `markdown_block` content from the draft,
paste into the form, hit submit.

**~30 seconds each. 7 registries = ~3.5 min total.**

After each submission, refresh the L23 audit's outreach ledger:
```bash
curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit
```
The next 2h lifecycle audit will reflect the new registry presence count.

---

## ✅ Now-autonomous — Code that's working without you

| Capability | Status | Watch |
|---|---|---|
| 503 storm healing | Auto via r51 circuit breaker | `/api/v1/admin/internal-bot-cb` |
| Tax-incentives map | All 50 states (r52) | https://dchub.cloud/tax-incentives |
| Marketing copy alignment | 12,877 facilities across 2K pages (r53) | https://dchub.cloud (homepage) |
| /daily cron resilience | Now 2x daily, fails loudly on 401 | GH Actions tab |
| Log noise throttle | 5min per (UA, path) | Railway logs |
| **DCPI per-market freshness** | New /api/v1/dcpi/freshness + force-rescore | L23 audit `dcpi_freshness` dim |
| **Testimonials seeder** | /api/v1/admin/testimonials/seed | Run weekly to pull new citations |
| L23 lifecycle audit | 16 dimensions, 88% composite | /lifecycle |
| LinkedIn rich-image posts | r48–r50.3 chain | 4/day at 08/12/16/20 UTC |
| MCP server discovery | server-card.json, citation endpoint | /.well-known/mcp/server-card.json |

---

## 📅 Future rounds — Bigger work needed

### F1. MCP conversion UX redesign

**Problem**: 9 conversions / 25k tool calls = 0.04% conversion rate.

**Root cause**: paywall returns 403 with generic upgrade CTA. No free
preview of the data, no clear value differential, no in-context
"this is what you'd get" demonstration.

**Scope**: dedicated round (~4-6 hours) to:
- Redesign paywall response to include a TRUNCATED preview
  (1 example result + count + upgrade ladder)
- Add an interactive `/playground` page where anon can run 5 MCP tool
  calls before being asked to claim a key
- A/B test 3 CTA copy variants

### F2. Visitor intel email backfill

Existing endpoint `/api/v1/admin/upgrade-pool/backfill-emails` exists
but has never been run. One-shot admin trigger to populate emails
from MCP paywall signals.

### F3. /ecosystem freshness

Currently stale — separate cron audit needed. /api/v1/brain/ecosystem
endpoints need a daily refresh cycle.

### F4. dc-hub-media distribution channels

LinkedIn live, X live (after token rotation), Bluesky pending token,
Data Center Dynamics + Data Center Frontier outreach pending.

---

## Quick health snapshot — what to look at this week

Every day, run:
```bash
curl -sS https://dchub.cloud/api/v1/brain/lifecycle/findings?force=1 | jq
```

If `composite_health` >= 0.85 and `findings_count` <= 3, **the platform
is healthy**. Otherwise the response tells you exactly what's weak +
how to fix it.

Daily morning check (3 minutes):
1. Hit the brain audit ↑
2. Hit `/api/v1/admin/integrations/health` — any token broken?
3. Look at https://dchub.cloud/lifecycle for the visual dashboard

---

Last updated: 2026-05-25 by r54 master shell
