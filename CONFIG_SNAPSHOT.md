# DC Hub — Config Snapshot (recoverable)

**Captured 2026-06-13 from the live Railway `dchub-backend` service** (`resourceful-essence`).
Non-secret values are recorded verbatim; secret values are NOT stored — only their NAMES, so you know what must be set for a clean restore.

> ### ★ Regenerate with `railway variables list -s dchub-backend`, NOT `railway run printenv`
>
> `railway run printenv` runs printenv **on your machine** with Railway's vars
> merged into your shell, so it dumps your local environment too. The
> 2026-06-13 capture did exactly that: 31 entries below were never Railway
> config at all — they were the operator's macOS shell (`HOME`, `PATH`, `PWD`,
> `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `SSH_AUTH_SOCK`, `XPC_*`) and the
> Claude Code desktop client that ran the command (`CLAUDE_CODE_*`,
> `CLAUDECODE`, `BAGGAGE`, `AI_AGENT`). They were removed on 2026-09-05.
>
> That is not a cosmetic problem in a PUBLIC repo: those entries published a
> username, a home-directory layout, an absolute path to the operator's Claude
> install, a client session id, and a Sentry trace — none of which describe the
> production service this file exists to restore. `railway variables list`
> returns the service's own variables and nothing else.
>
> The rule from the 2026-08-07 credential incident still applies and is the
> same rule: **filter by a KEY-NAME allowlist; never paste an environment dump
> wholesale.** A var can hold something private regardless of whether its name
> looks sensitive.
>
> The 59 entries below are what remained after the machine-local ones were
> removed. They have NOT been re-verified against the live service since
> 2026-06-13 — regenerate before trusting this for a restore.


## Brain / autonomy / publishing config (the settings that define behavior)

- `DCHUB_AUTOPUB_LEGACY` = `1`
- `DCHUB_BRAIN_1M_CONTEXT` = `1`
- `DCHUB_BRAIN_L5_CONF_MIN` = `0.75`
- `DCHUB_BRAIN_MODEL_CHALLENGER` = `claude-opus-4-8`
- `DCHUB_BRAIN_MODEL_INSPECTOR` = `claude-fable-5`
- `DCHUB_BRAIN_MODEL_REASONING` = `claude-fable-5`
- `DCHUB_BRAIN_STRATEGIC_DRAFT_PR` = `1`
- `DCHUB_TRIAL_TOOL_DAILY_FULL` = `8`

### Autonomy switches — current state + how to change

- **Inspector/Reasoning model** = `claude-fable-5` (`DCHUB_BRAIN_MODEL_INSPECTOR`/`_REASONING`); Challenger = `claude-opus-4-8`; 1M context ON. To move the brain to Opus, set those two to `claude-opus-4-8`.
- **`BRAIN_L22_HANDOFF_ENABLE`** — NOT set (Inspector→L22 auto-code handoff OFF). r79 wired the pipeline; set to `1` to let the brain draft route_alias_404 fixes. Kill globally with `BRAIN_AUTOPILOT_DISABLED=1`.
- **`DCHUB_L22_REAL_PR`** — NOT set (real GitHub PR writing OFF; brain can only open Issues / fork-branch drafts). Keep OFF until you've watched a few handoff cycles.
- **`DCHUB_BRAIN_STRATEGIC_DRAFT_PR=1`** — the SAFER Layer-5 backlog auto-PR path (ast.parse gate + daily cap) is already ON.
- **`DCHUB_AUTOPUB_LEGACY=1`** — Twitter/Bluesky/LinkedIn legacy auto-publishers ON (r78 leader-gated + terminal-reject).
- **`DCHUB_TRIAL_TOOL_DAILY_FULL=8`** — per-tool daily full-data cap for trial keys (upgrade-pressure dial; 0/unset = off).
- **`DCHUB_BRAIN_L5_CONF_MIN=0.75`** — Layer-5 proposal confidence floor.

## All other non-secret config (59 vars)

- `ADMIN_INBOX_EMAIL` = `jonathan@dchub.cloud`
- `API_TIMEOUT_MS` = `900000`
- `AUTOPOST_ENABLED` = `0`
- `AUTOPOST_SIZE` = `square`
- `AUTOPOST_THEME` = `rotate`
- `CF_ACCOUNT_ID` = `4bb33ec40ef02f9f4b41dc97668d5a52`
- `CLOUDFLARE_ACCOUNT_ID` = `4bb33ec40ef02f9f4b41dc97668d5a52`
- `CLOUDFLARE_KV_NAMESPACE` = `88f7d45862894495967d5f2e438b29c3`
- `CLOUDFLARE_PROJECT_NAME` = `dchub`
- `CLOUDFLARE_ZONE_ID` = `1cb22dda8d50546d6edf0c09a8be5128`
- `CONTENT_QUALITY_MIN` = `.7`
- `COREPACK_ENABLE_AUTO_PIN` = `0`
- `CRAWLER_SCHEDULE` = `once`
- `DAILY_SVC_URL` = `https://dchub-daily-production.up.railway.app`
- `DB_POOL_MAX` = `50`
- `DB_POOL_MIN` = `5`
- `DCHUB_API_BASE` = `https://dchub-backend-production.up.railway.app`
- `DCHUB_API_URL` = `https://dchub-backend-production.up.railway.app/v1`
- `DCHUB_BRIEFING_EMAIL` = `jonathan@dchub.cloud,azmartone@gmail.com`
- `DCHUB_FORCE_IPV4` = `0`
- `DCHUB_FROM_EMAIL` = `jonathan@dchub.cloud`
- `DCHUB_INTERNAL_API` = `https://dchub-backend-production.up.railway.app`
- `DCHUB_MEDIA_AI_IMAGES` = `1`
- `DCHUB_OUTREACH_FROM_EMAIL` = `jonathan@dchub.cloud`
- `DCM_CRAWL_ENABLED` = `true`
- `DRY_RUN` = `1`
- `EPA_AQS_API_EMAIL` = `jonathan@dchub.cloud`
- `ERCOT_CLIENT_ID` = `fec253ea-0d06-4272-a5e6-b478baeecd70`
- `ERCOT_USERNAME` = `jonathan@dchub.cloud`
- `FCC_BDC_USERNAME` = `jonathan@dchub.cloud`
- `FOUNDING_CUSTOMERS_CAP` = `25`
- `GOOGLE_CLIENT_ID` = `779226954476-a5j35ni6q07n86pj0dio9h7di8mdrg8k.apps.googleusercontent.com`
- `INTERNAL_AUTH_LEGACY_OK` = `0`
- `LINKEDIN_ATTACH_IMAGES` = `1`
- `LINKEDIN_CLIENT_ID` = `866i0gxj2ka74u`
- `LINKEDIN_COMPANY_ID` = `110894959`
- `LINKEDIN_ORG_ID` = `110894959`
- `LINKEDIN_PERSON_URN` = `Wy51ad4WPd`
- `LINKEDIN_REDIRECT_URI` = `https://dchub.cloud/api/linkedin/callback`
- `NEWS_NER_LLM` = `true`
- `NEWS_VIA_CRON` = `1`
- `NODE_USE_SYSTEM_CA` = `1`
- `OPENAI_BASE_URL` = `https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/openai`
- `OUTREACH_FROM_EMAIL` = `api@dchub.cloud`
- `OUTREACH_INTERVAL_MINUTES` = `720`
- `PJM_USERNAME` = `AZMARTONE `
- `R2_ACCOUNT_ID` = `4bb33ec40ef02f9f4b41dc97668d5a52`
- `R2_BUCKET` = `dchub-daily`
- `R2_BUCKET_NAME` = `dchub-backups`
- `R2_ENDPOINT_URL` = `https://4bb33ec40ef02f9f4b41dc97668d5a52.r2.cloudflarestorage.com`
- `R2_PUBLIC_BASE` = `https://daily.dchub.cloud`
- `SMTP_FROM_EMAIL` = `jonathan@dchub.cloud`
- `SMTP_FROM_NAME` = `DC Hub`
- `SMTP_HOST` = `smtpout.secureserver.net`
- `SMTP_PORT` = `587`
- `SMTP_USERNAME` = `jonathan@dchub.cloud`
- `USE_LOCAL_OAUTH` = ``
- `USE_STAGING_OAUTH` = ``
- `VAPID_EMAIL` = `mailto:azmartone@gmail.com`

## Secret vars that MUST be set for a clean restore (88 — values NOT stored here)

> `REDIS_URL` and `RENDER_DEPLOY_HOOK_URL` sat in the section above WITH their
> values from 2026-06-13 to 2026-08-07 — in a public repo, reachable forever via
> the old SHAs. Both credentials were rotated on 2026-08-07 and the values here
> replaced with names. CI now greps every tracked file for credential-shaped
> strings (`scripts/check_no_leaked_credentials.py`); a URL that embeds a
> password or key does not merge.

`ADMIN_API_KEY`, `ADMIN_SECRET`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `API_SECRET`, `BLUESKY_APP_PASSWORD`, `BLUESKY_HANDLE`, `BRAIN_ADMIN_KEY`, `CF_RULESET_TOKEN`, `CF_WAF_EDIT_TOKEN`, `CLOUDFLARE_API_TOKEN`, `COHERE_API_KEY`, `DAILY_ADMIN_KEY`, `DATABASE_URL`, `DCHUB_ADMIN_KEY`, `DCHUB_API_KEY`, `DCHUB_API_KEYS`, `DCHUB_ENT_KEY`, `DCHUB_INTERNAL_KEY`, `DCHUB_RESEND_API_KEY`, `DCHUB_SESSION_SECRET`, `DCHUB_SMOKE_ENTERPRISE_KEY`, `DCHUB_STRIPE_DEVELOPER_LINK`, `DCHUB_STRIPE_PRO_LINK`, `DCHUB_SYNC_KEY`, `EIA_API_KEY`, `ENTSOE_API_TOKEN`, `EPA_AQS_API_KEY`, `ERCOT_API_KEY`, `ERCOT_PASSWORD`, `FCC_BDC_TOKEN`, `FIRMS_MAP_KEY`, `GEMINI_API_KEY`, `GH_TOKEN`, `GITHUB_TOKEN`, `GOOGLE_AI_KEY`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GRIDSTATUS_API_KEY`, `GROQ_API_KEY`, `HUNTER_API_KEY`, `INTERNAL_SYNC_SECRET`, `IPINFO_TOKEN`, `JWT_SECRET`, `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_CLIENT_SECRET`, `MASTODON_ACCESS_TOKEN`, `MCP_API_KEY`, `MISTRAL_API_KEY`, `NEON_DATABASE_URL`, `OPENAI_API_KEY`, `PERPLEXITY_API_KEY`, `PJM_PASSWORD`, `PLAUSIBLE_API_KEY`, `PR_SUBMIT_TOKEN`, `PUBLISH_API_SECRET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `REDIS_URL`, `REFRESH_SECRET`, `RENDER_API_KEY`, `RENDER_DEPLOY_HOOK_URL`, `RESEND_API_KEY`, `SECRET_KEY`, `SENDGRID_API_KEY`, `SMITHERY_TOKEN`, `SMTP_PASSWORD`, `STRIPE_PRICE_DEV_MONTHLY`, `STRIPE_PRICE_PRO_A`, `STRIPE_PRICE_PRO_ANNUAL`, `STRIPE_PRICE_PRO_B`, `STRIPE_PRICE_RESEARCH_SEED_ANNUAL`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET_MCP`, `TOGETHER_API_KEY`, `TWITTER_ACCESS_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_BEARER_TOKEN`, `TWITTER_PUBLISHER_ENABLED`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `XAI_API_KEY`, `YOUR_API_KEY`, `YOU_API_KEY`


## Other config homes (not in Railway env)

- **Cloudflare** (zone `1cb22dda…`, acct `4bb33ec4…`): `dchubapiproxy` + `dchub-selfheal` workers (dashboard-only), `_routes.json`/`_redirects`/`_worker.js` (in `dchub-frontend`), WAF self-probe skip rule `5f9a1d83…`. See `reference_dchub_*` memories.
- **Neon**: project `cool-dream-61262894` (Launch, 7d PITR) + nightly pg_dump→R2. See `RESTORE_RUNBOOK.md`.
- **Render**: failover replica (read-only; `IS_FAILOVER=true`).
- **GitHub Actions**: ~120 workflow crons in `.github/workflows/` (the brain's heartbeat). NOTE: Actions has NO `DATABASE_URL` secret — DB one-offs need a Railway admin endpoint or the Neon console.
- **Health baseline + invariants**: `HEALTH_BASELINE.md` §1-8.