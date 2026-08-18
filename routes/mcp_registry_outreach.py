"""mcp_registry_outreach.py
=================================
Phase r33-N (2026-05-21) — 24x7 outbound discovery engine.

User asked for: "the site has to be alive, and proactively telling
other agents and mcp servers about us... 24x7 always promoting,
solving problems, saving people time and money."

This module is the outbound half of the brain. The inbound half
(consistency_radar + autopilot) watches our site for problems. This
half watches our PRESENCE on the open web — making sure every AI
runtime that could discover DC Hub actually knows about us.

What it does:
  1. Knows the 7 major MCP registries / discovery surfaces
  2. Daily cron submits/refreshes our listing on each
  3. Audits whether each registry's public page actually lists us
  4. Logs every outbound action to outreach_submissions table
  5. Brain detector (check_outbound_distribution_health) flags any
     registry where we've fallen off or our manifest is stale

Admin endpoints:
  POST /api/v1/admin/outreach/mcp-registry/submit
       — kick a single registry submission cycle
  POST /api/v1/admin/outreach/mcp-registry/submit-all
       — submit to every known target (called by GH Actions cron)
  GET  /api/v1/admin/outreach/mcp-registry/status
       — last-submission timestamps + audit results per target

Auth: X-Admin-Key required (DCHUB_ADMIN_KEY env).
"""
from __future__ import annotations

import os
import json
import time
import logging
import datetime as _dt
from typing import Optional

from flask import Blueprint, request, jsonify

import psycopg2
import psycopg2.extras
from ai_surface_canon import canon_text

logger = logging.getLogger(__name__)

mcp_registry_outreach_bp = Blueprint("mcp_registry_outreach", __name__)


# ──────────────────────────────────────────────────────────────────
# Discovery targets. Each entry knows enough to either:
#   - POST our manifest to the registry's submit endpoint, OR
#   - Audit whether we're listed (HEAD/GET against a "find me" URL), OR
#   - Both.
#
# Where a registry doesn't have a public submission API yet, the
# 'submit_method' is "manual" and 'manual_url' points at the page
# where a human (or L22 PR-drafter) opens a PR/issue. The audit
# method still works against the registry's catalog page so we
# notice when our listing IS approved.
# ──────────────────────────────────────────────────────────────────

DISCOVERY_TARGETS = [
    {
        # r33-N+ (2026-05-21) — Verified live: Smithery API confirms
        # qualifiedName `azmartone67/dchub` + displayName "DC Hub -
        # Data Center Intelligence". Audit goes through the Smithery
        # registry API (returns JSON, easy signal match).
        "key":         "smithery",
        "name":        "Smithery",
        "homepage":    "https://smithery.ai/server/azmartone67/dchub",
        "submit_url":  "https://smithery.ai/server/azmartone67/dchub",
        "submit_method":"refresh_only",        # already listed; we only audit
        "manual_url":  "https://github.com/smithery-ai/registry/blob/main/CONTRIBUTING.md",
        "audit_url":   "https://registry.smithery.ai/servers?q=dchub",
        "audit_signal":"azmartone67/dchub",
        "description": "Largest community MCP registry. Already listed as azmartone67/dchub.",
    },
    {
        # 2026-07-18: mcp.so restructured /server/<name> → /servers/<slug>;
        # old /server/dc-hub 404s. Canonical listing is
        # /servers/dchub-mcp-server (title "Dc Hub — Data Center Intelligence");
        # signal "Data Center Intelligence" appears 22× on that page vs 1× for
        # the old "DC Hub MCP", so the audit survives page-copy tweaks.
        "key":         "mcpso",
        "name":        "mcp.so",
        "homepage":    "https://mcp.so/servers/dchub-mcp-server",
        "submit_url":  "https://mcp.so/submit",
        "submit_method":"refresh_only",
        "manual_url":  "https://mcp.so/submit",
        "audit_url":   "https://mcp.so/servers/dchub-mcp-server",
        "audit_signal":"Data Center Intelligence",
        "description": "Public MCP server directory. Already listed as /servers/dchub-mcp-server.",
    },
    {
        # Verified live at glama.ai/mcp/connectors/cloud.dchub/mcp-server (200 OK)
        "key":         "glama",
        "name":        "Glama AI",
        "homepage":    "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server",
        "submit_url":  "https://glama.ai/mcp/servers/submit",
        "submit_method":"refresh_only",
        "manual_url":  "https://glama.ai/mcp/servers/submit",
        "audit_url":   "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server",
        "audit_signal":"dchub",                # case-insensitive substring
        "description": "AI gateway with MCP aggregation. Already listed as cloud.dchub.",
    },
    {
        # r-fix 2026-07-02: root-caused the "listing vanished" alarm.
        # mcphub.io is a Next.js SPA whose data ALL comes from
        # https://registry.mcphub.io — and that origin is DOWN
        # (Cloudflare 525 on every endpoint), so the site renders an
        # empty shell for EVERY server, not just us. The old audit_url
        # (/servers/dchub) returns a 200 SPA shell with no server-side
        # content, so the signal check can never pass — repointed the
        # audit at their search API, which returns real JSON when (if)
        # their origin comes back. Also: the only archived snapshot of
        # their registry (2025-03-22, 276 entries) does NOT contain
        # dchub, so we were likely never listed. /submit 404s; their
        # sitemap points at mcphub.net, which now redirects to an ad
        # domain (parked/hijacked). No working submission path.
        "key":         "mcphub",
        "name":        "MCPHub",
        "homepage":    "https://mcphub.io",
        "submit_url":  None,
        "submit_method":"manual",
        "manual_url":  "https://mcphub.io",
        "audit_url":   "https://registry.mcphub.io/search?q=dchub",
        "audit_signal":"dchub",
        "audit_browser_ua": True,
        "description": "MCP server hub (mcphub.io). Data backend registry.mcphub.io DOWN (CF 525) as of 2026-07-02; no submit path; never confirmed listed.",
    },
    {
        # PulseMCP serves 403 to bare curl (bot protection). Audit via
        # their JSON API/sitemap if possible, otherwise treat audit as
        # informational.
        "key":         "pulsemcp",
        "name":        "PulseMCP",
        "homepage":    "https://www.pulsemcp.com",
        "submit_url":  "https://www.pulsemcp.com/servers/submit",
        "submit_method":"manual",
        "manual_url":  "https://www.pulsemcp.com/servers/submit",
        "audit_url":   "https://www.pulsemcp.com/servers/dchub",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,              # send a real browser UA
        "description": "Curated MCP pulse. Bot-protected; submission status pending.",
    },
    {
        # Confirmed NOT in README — needs PR.
        "key":         "awesome_mcp",
        "name":        "awesome-mcp-servers (GitHub)",
        "homepage":    "https://github.com/punkpeye/awesome-mcp-servers",
        "submit_url":  None,
        "submit_method":"github_pr",
        "manual_url":  "https://github.com/punkpeye/awesome-mcp-servers/pulls",
        "audit_url":   "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
        # r78: the merged listing line reads "azmartone67/dchub-mcp-server"
        # — it does NOT contain "dchub.cloud", so the old signal was a
        # permanent FALSE NEGATIVE (we've been listed all along while the
        # brain filed outbound_distribution_health against this target).
        "audit_signal":"dchub-mcp-server",
        # 2026-08-05: state made explicit so the manual-queue digest stops
        # listing a submission that r78 already proved was merged.
        "outreach_state": "listed",
        "description": "Canonical curated README. LISTED (azmartone67/dchub-mcp-server).",
    },
    {
        "key":         "anthropic_directory",
        "name":        "Anthropic MCP Connector Directory",
        "homepage":    "https://claude.ai/settings/connectors",
        "submit_url":  None,
        "submit_method":"anthropic_form",
        "manual_url":  "https://www.anthropic.com/contact-sales",
        "audit_url":   None,
        "audit_signal":None,
        # 2026-08-05: owner has made contact. Recorded here so the manual-queue
        # digest stops listing it as untouched — a to-do list that keeps
        # renaming work you have already done is a list you stop reading.
        # Move to "listed" once it appears in the directory; move back to
        # "not_started" if the thread dies, rather than leaving it parked here
        # forever on the strength of one email.
        "outreach_state": "awaiting_response",
        "outreach_note": "Owner contacted Anthropic 2026-08-05; awaiting reply.",
        "description": "Anthropic's curated directory. No public submission API; owner outreach sent 2026-08-05.",
    },
    # r36 (2026-05-25): Added the 4 registries L23 lifecycle audit
    # flagged missing. Submission methods are best-effort — most of
    # these directories don't expose a programmatic submit endpoint, so
    # 'manual' is the default and the cron logs a 'manual_pending'
    # outcome rather than blowing up.
    {
        "key":         "lobehub",
        "name":        "Lobehub",
        "homepage":    "https://lobehub.com/mcp",
        # r-fix 2026-06-10: lobehub.com/mcp/submit 404s. Real path is a
        # GitHub issue on lobehub/lobehub. Filed issue #15667.
        "submit_url":  None,
        # was github_issue; that channel is gone (auto-closed since 2026-08).
        "submit_method":"manual",
        # ⚠ the guide URL is where the CLI instructions live, but lobehub.com is
        # unreachable from CI's egress (policy denial on CONNECT), so nothing
        # automated here has verified it — the commands in the comment above
        # came from lobehub's own auto-reply script on GitHub, which IS
        # reachable and is the more trustworthy source anyway.
        "manual_url":  "https://lobehub.com/publish-mcp/skill.md",
        "submit_url_verified": False,
        # r-fix 2026-07-02: LobeHub moved listings to market.lobehub.com;
        # the old lobehub.com/mcp/<slug> URL 302s and the auditor read
        # signal_missing on a live listing. Audit the market page directly.
        # r-fix 2026-08-15: /s/plugins/azmartone67-dchub-backend 404s — the
        # CLI-published plugin is still status=unpublished (the republish was
        # never run) and is now moot: LobeHub's marketplace listing for us is
        # LIVE at /s/plugins/azmartone67-dchub-mcp-server (verified 200 with
        # full DC Hub identity; a nonsense control slug 404s, so 200 vs 404
        # discriminates). Audit the listing that exists — auditing the
        # unpublished duplicate reported "not listed" for a marketplace we
        # are live on, the exact false negative this URL was last fixed for.
        "audit_url":   "https://market.lobehub.com/s/plugins/azmartone67-dchub-mcp-server",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        # ★ 2026-08-07 (corrected 2026-08-08). Two claims here were wrong and
        # are fixed rather than quietly edited, because both changed what a
        # human would do next.
        #
        # WRONG #1: "the issue channel is closed." lobehub's auto-handler
        # (.github/scripts/auto-handle-mcp-submission.ts) DOES auto-close
        # listing issues with a redirect to self-serve — but the owner reports
        # #15667 is OPEN. This session cannot verify that (the GitHub API is
        # scoped to azmartone67/*), so this records the OWNER'S OBSERVATION,
        # not a check we ran. Either way the issue is not the path: the bot
        # redirects to the CLI, and we have not run it.
        #
        # WRONG #2, and the one that mattered: "remote servers may be declined."
        # That came from their ISSUE classifier ("remote URL-only ... go to
        # humans"), which routes GitHub issues — NOT from the CLI. The CLI
        # itself ships:
        #     --url <url>   "Inspect a running Streamable HTTP MCP server"
        # DC Hub is exactly that. It is supported. The caveat was discouraging
        # an attempt that should work.
        #
        # ★ THE COMMANDS — corrected 2026-08-08 AFTER a live run failed.
        # The owner ran what was recorded here and got:
        #     error: unknown option '--identifier'
        #     error: unknown command 'submit'
        # Both were mine. I had assembled flags from a grep across the WHOLE
        # market-cli bundle instead of reading which options belong to which
        # subcommand, and took "plugin submit" from lobehub's auto-reply text —
        # which is out of sync with their own shipped CLI. Verified against
        # @lobehub/market-cli 0.0.40 (the version npx resolves) by parsing the
        # command tree, not by grepping strings:
        #
        #   plugin init                 --dir --force --stdio --url   (ONLY these)
        #   plugin publish <gitUrl>     --dir --output
        #   plugin claim <identifier>
        #
        # There is no `submit`, and init takes NO --identifier/--name/
        # --description. Everything is inferred:
        #     identifier  <- "<gh-owner>-<gh-repo>" from the git remote, else
        #                    package.json name  (throws if neither)
        #     name        <- serverInfo.title | package.json displayName |
        #                    serverInfo.name | package name | repo name
        #     description <- serverInfo.description | package.json description
        #                    ★ THROWS if neither. Ours has neither: the
        #                    advertised serverInfo is {name, version} only
        #                    (main.py ~11008) and this repo has no package.json.
        #     homepage/tags/version <- serverInfo, else package.json
        #
        # ★ SO RUN IT FROM A SCRATCH DIRECTORY, NOT THE REPO. Both init and
        # publish take --dir, so the manifest never has to live here. A root
        # package.json in THIS repo would be a genuine deploy risk: Railway
        # builds with NIXPACKS (railway.json), which detects the language from
        # the files present, and main is push-to-deploy. Not worth it for a
        # marketplace listing.
        #
        #   mkdir -p ~/dchub-lobehub && cd ~/dchub-lobehub
        #   # package.json supplying what serverInfo does not — see PR body
        #   npx -y @lobehub/market-cli plugin init --url https://dchub.cloud/mcp
        #   npx -y @lobehub/market-cli plugin publish https://github.com/azmartone67/dchub-backend
        #   npx -y @lobehub/market-cli plugin list --output json
        #
        # init writes lhm.plugin.json (requires non-empty identifier, name,
        # version; adds cloudEndpoint for a --url server and inspects the live
        # tool/prompt/resource list). Generate it against the LIVE endpoint
        # rather than hand-writing it — a hand-written tool list goes stale the
        # first time a tool ships.
        #
        # Sources: lobehub's own auto-reply script on GitHub and the published
        # @lobehub/market-cli 0.0.40 bundle from npm — both reachable. The
        # guide at lobehub.com/publish-mcp/skill.md is NOT: this session's
        # egress proxy denies lobehub.com (connect_rejected, policy denial), so
        # it has never been read and manual_url stays flagged unverified.
        # ★ 2026-08-07 14:32Z — PUBLISHED. `plugin publish` succeeded:
        #     ✓ Published azmartone67-dchub-backend@2.11.1
        # and `plugin list` confirms isClaimed=true. But it is NOT live yet:
        #     "status": "unpublished"
        # The CLI has exactly two statuses — unpublish() sets "unpublished"
        # (下架, delisted) and republish() sets "published" (上架, listed). So
        # one action remains and this stays on the queue until it is done:
        #     npx -y @lobehub/market-cli plugin republish azmartone67-dchub-backend
        #
        # ★ AND DO NOT USE THE WEB "REFRESH METADATA" BUTTON. It fails with
        # "workflow trigger is not configured" — reproducibly, both on the
        # GitHub-badge claim and on a metadata refresh (owner, 2026-08-07/08).
        # That string appears in NO public lobehub code (checked their repo and
        # the published CLI bundle; GitHub-wide code search returns 65 hits,
        # none theirs), so it is server-side at market.lobehub.com and we
        # cannot diagnose it. Not guessing at a cause.
        #
        # The CLI does the same job WITHOUT that mechanism: `plugin update`
        # reads lhm.plugin.json and calls publishPluginVersion through the
        # authenticated SDK — no workflow trigger anywhere in the path.
        #
        #   cd ~/dchub-lobehub
        #   npx -y @lobehub/market-cli plugin init --url https://dchub.cloud/mcp --force
        #   npx -y @lobehub/market-cli plugin update
        #
        # So the badge is cosmetic and the web refresh is avoidable; neither
        # blocks the listing. If the badge still matters later, ask lobehub
        # with the identifier attached — a far easier question than the one we
        # could have asked before publishing.
        #
        # ★ AND THE AUDIT URL WAS WRONG. It pointed at
        # /s/plugins/azmartone67-dchub-mcp-server; lobehub assigned
        # azmartone67-dchub-backend (identifier = "<gh-owner>-<gh-repo>", so it
        # follows the REPO name, not the server name). The old URL would 404
        # forever and the nightly audit would report us permanently "not
        # listed" — the identical false negative r78 found on awesome_mcp,
        # where a wrong signal had the brain filing distribution findings
        # against a listing that was live the whole time. Corrected from the
        # identifier the CLI actually returned, not from a guess.
        #
        # Version 2.11.1 came from the LIVE endpoint's serverInfo (init --url
        # inspected it), which also means the upstream MCP server returns a
        # richer serverInfo than main.py's GET capabilities branch advertises
        # ({name, version}, version 1.27.0 there) — worth reconciling
        # separately; two different versions on two surfaces is its own bug.
        # ★ 2026-08-15: LISTED — via the marketplace listing at
        # /s/plugins/azmartone67-dchub-mcp-server (the re-slug survivor the
        # registry watcher verifies), NOT via the CLI publish below. The
        # 08-07 CLI publish (PUBLISHED as azmartone67-dchub-backend@2.11.1,
        # isClaimed=true, status=unpublished) is deliberately left
        # unpublished: republishing it now would create a SECOND listing for
        # the same server. The queue item this entry used to carry
        # ("run `plugin republish azmartone67-dchub-backend`") is therefore
        # retired as an anti-goal, not completed.
        "outreach_state": "listed",
        "outreach_note": ("LISTED 2026-08-15 via market.lobehub.com/s/plugins/"
                          "azmartone67-dchub-mcp-server (verified 200, full DC "
                          "Hub identity). Do NOT run `plugin republish "
                          "azmartone67-dchub-backend` — the 08-07 CLI publish "
                          "stays unpublished on purpose; republishing would "
                          "create a duplicate listing next to the live one."),
        "description": "Lobehub MCP directory. Self-service via @lobehub/market-cli; remote Streamable HTTP servers ARE supported (plugin init --url). Owner reports issue #15667 still open. Fallback if self-publish is refused: 'Request a Server' at lobehub.com/mcp.",
    },
    {
        "key":         "mcp_hive",
        "name":        "MCP Hive",
        "homepage":    "https://mcphive.com",
        # r-fix 2026-06-10: real form is /submit.html (not /submit, which 404s).
        # r-fix 2026-07-02: submission FILED via the form's own endpoint
        # (multipart POST to /scripts/save_submission.php, exactly what
        # js/submit.js sends) — the endpoint returned the host's 404
        # page. The form is broken for browser users too; there is no
        # backing GitHub repo and no contact email anywhere on the
        # site. Their directory is a static CSV (data/servers.csv)
        # that is a stale scrape of punkpeye/awesome-mcp-servers
        # (verbatim emoji descriptions + identical category slugs) —
        # and we ARE in awesome-mcp-servers, so if they ever re-scrape
        # we appear for free. Audit repointed at the CSV (the old
        # /servers/<slug> pages never existed on this static site).
        "submit_url":  "https://mcphive.com/submit.html",
        "submit_method":"manual",
        "manual_url":  "https://mcphive.com/submit.html",
        "audit_url":   "https://mcphive.com/data/servers.csv",
        "audit_signal":"dchub",
        "audit_browser_ua": True,
        "description": "MCP Hive directory. Submission attempted 2026-07-02: form backend (save_submission.php) 404s — dead/abandoned scrape of awesome-mcp-servers; no working path.",
    },
    {
        "key":         "toolhive",
        "name":        "ToolHive",
        # r-fix 2026-06-10: toolhive.io is DEAD (301 -> compliancehive.eu).
        # The real ToolHive is Stacklok's GitHub registry (repo renamed
        # toolhive-registry -> toolhive-catalog). PR #1252 filed.
        # r-fix 2026-07-02: PR #1252 was CLOSED 2026-06-16 by a Stacklok
        # collaborator — REJECTED on curated-registry fit, not technical
        # grounds ("handshakes cleanly and the tools match the entry
        # exactly"): single-maintainer project, no tagged releases,
        # limited community traction, proprietary hosted backend. See
        # https://github.com/stacklok/toolhive-catalog/pull/1252 and
        # their docs/registry-criteria.md#community-health. They invited
        # a fresh submission once the project matures (tagged releases,
        # contributor base, adoption signal) — do NOT re-file before
        # that bar is met; it reads as spam and burns goodwill.
        "homepage":    "https://github.com/stacklok/toolhive-catalog",
        "submit_url":  "https://github.com/stacklok/toolhive-catalog/pulls",
        "submit_method":"github_pr",
        "manual_url":  "https://github.com/stacklok/toolhive-catalog/pull/1252",
        "audit_url":   "https://raw.githubusercontent.com/stacklok/toolhive-catalog/main/registries/toolhive/servers/dchub/server.json",
        "audit_signal":"dchub",
        "audit_browser_ua": True,
        "description": "Stacklok ToolHive registry. PR #1252 REJECTED 2026-06-16 on curated-fit criteria (maturity/traction, not quality). Re-apply only after tagged releases + community traction.",
    },
    {
        "key":         "yellowmcp",
        "name":        "Yellowmcp",
        # r-fix 2026-06-10: AUTO-DISCOVERS remote servers — DC Hub is
        # ALREADY listed (not a submission gap). BUT flagged -100%
        # "Declining Reliability" because their uptime probe does
        # GET /mcp -> 405 (the MCP POST initialize handshake returns 200).
        # Action: claim the listing + make GET /mcp return 200.
        # r-fix 2026-07-02: the "missing" alarm was a SLUG CHANGE, not a
        # drop — yellowmcp moved us from /servers/dchub (now 404) to
        # /servers/cloud-dchub-mcp-server (verified live, linked from
        # their homepage, 83.3% uptime / probes "reachable"). GET /mcp
        # now returns 200 so the old 405-reliability penalty is gone.
        # Audit repointed at the new slug; flipped to refresh_only
        # (listed — audit-only, same as Smithery/mcp.so). Claiming the
        # listing still requires an interactive login at /claim —
        # owner action, can't be automated.
        # ⚠ 2026-07-02: yellowmcp ALSO carries a stale DUPLICATE at
        # /servers/dc-hub-data-center-intelligence — 4.4% uptime, an
        # ancient description (15 tools / $49/mo / "50,000 facilities"),
        # probing the DEAD Smithery gateway
        # server.smithery.ai/@azmartone67/dchub-nexus/mcp (the defunct
        # dchub-nexus slug). No self-serve delete exists (claim only
        # grants edit of description/docs/category), so removal was
        # requested from the operator (GitHub avib99 — the handle
        # behind yellowmcp.com) via
        # https://github.com/avib99/yellowmcp/issues/1 (2026-07-02).
        "homepage":    "https://yellowmcp.com/servers/cloud-dchub-mcp-server",
        "submit_url":  "https://yellowmcp.com/claim",
        "submit_method":"refresh_only",
        "manual_url":  "https://yellowmcp.com/claim",
        "audit_url":   "https://yellowmcp.com/servers/cloud-dchub-mcp-server",
        "audit_signal":"DC Hub",
        "audit_browser_ua": True,
        "description": "Yellowmcp reliability directory. LISTED at /servers/cloud-dchub-mcp-server (slug changed 2026; 83.3% uptime). Claim-listing still pending (needs interactive login).",
    },
    {
        # 2026-08-05 — PLATFORM CATALOG, not a community registry, and that
        # distinction is the point. 30d attribution: datacolo + smithery =
        # 5,078 calls from 3 agents (crawlers), while every branded assistant
        # combined = ~775 calls (5.1%). Another community directory buys more
        # crawl. A platform connector catalog is where a real user question
        # arrives, which is why this and anthropic_directory are the only two
        # entries on the manual queue with a path to a licence.
        #
        # ⚠ SUBMISSION URL IS UNVERIFIED. I could not confirm a canonical
        # Mistral connector-submission endpoint from inside this repo, and
        # inventing a plausible-looking one is worse than admitting it — the
        # digest prints a warning against any target with url_verified False
        # so whoever opens it knows to find the real path first and correct
        # this entry. audit_url is deliberately None: nothing automated should
        # fetch a URL we are not sure about.
        "key":         "mistral_connectors",
        "name":        "Mistral (Le Chat connectors)",
        "homepage":    "https://mistral.ai",
        "submit_url":  None,
        "submit_method":"manual",
        "manual_url":  "https://mistral.ai/contact",
        "submit_url_verified": False,
        "audit_url":   None,
        "audit_signal":None,
        "outreach_state": "not_started",
        "description": "Mistral's Le Chat connector catalog. PLATFORM CATALOG — the channel that produces user questions rather than crawls. Owner-requested 2026-08-05.",
    },
]


# r36 (2026-05-25): exposed for lifecycle L23 audit (cross-references
# the live ledger instead of relying on a hardcoded noted-list).
# r49.7 (2026-05-25): registries with verified-dead submission URLs
# (404 on /submit, no programmatic submit path). Excluded from the
# L23 registry_presence audit until/unless they re-emerge. Keeping
# them in DISCOVERY_TARGETS as a historical record but filtering on
# read so the brain dashboard doesn't keep flagging us as "missing
# from five 404 pages."
_DEAD_REGISTRY_KEYS = {
    # r-fix 2026-06-10: re-verified all five. Only mcphub.io still has no
    # working manual path (JS-only SPA; /submit 404s; no GitHub repo).
    # The others had their REAL paths found and are now live/actioned:
    #   lobehub   -> GitHub issue lobehub/lobehub#15667
    #   yellowmcp -> LISTED at /servers/cloud-dchub-mcp-server (07-02)
    # r-fix 2026-07-02: mcp_hive + toolhive re-verified and EXCLUDED:
    #   mcp_hive  -> form backend (save_submission.php) 404s; abandoned
    #                static scrape of awesome-mcp-servers; no contact.
    #                Submission attempted 07-02, endpoint dead.
    #   toolhive  -> NOT dead, but PR #1252 was REJECTED 06-16 on
    #                curated-fit criteria (maturity/traction). Excluded
    #                so L23 stops flagging a gap we deliberately won't
    #                re-file until the project meets their bar.
    "mcphub",     # data backend registry.mcphub.io down (CF 525); no submit path
    "mcp_hive",   # form backend dead (404); abandoned site
    "toolhive",   # rejected on fit 2026-06-16; re-apply when matured
}


def get_target_names() -> list[str]:
    """Names of all discovery targets known to this module.

    r49.7: filter out _DEAD_REGISTRY_KEYS — registries whose submit
    URLs return 404 and have no alternative submission path. They
    remain in DISCOVERY_TARGETS for historical reference but are
    excluded here so the L23 audit doesn't keep flagging us against
    dead URLs.
    """
    return [t["name"] for t in DISCOVERY_TARGETS
            if t.get("key") not in _DEAD_REGISTRY_KEYS]


def get_submitted_target_names() -> list[str]:
    """Names of targets we're confident are LIVE on the registry.

    Two sources combined:
      1. Targets marked `submit_method='refresh_only'` are
         pre-confirmed listings (we manually verified live + the
         DISCOVERY_TARGETS comment block records the listing URL).
      2. Any target with a ledger row whose outcome signals presence
         (success | verified | listed | audit_pass | refresh_ok).

    Best-effort — returns refresh_only set on DB error so the audit
    still emits useful pending vs. submitted ratios.
    """
    confirmed = {t["name"] for t in DISCOVERY_TARGETS
                 if t.get("submit_method") == "refresh_only"}

    conn = _db()
    if not conn:
        return sorted(confirmed)
    try:
        with conn, conn.cursor() as cur:
            # r49.6 (2026-05-25): expand the "submitted" outcome set to
            # include `pr_filed` and `issue_filed`. These are real,
            # auditable submission states (an open PR or filed issue
            # is a submission — the maintainer hasn't merged yet, but
            # we DID submit). Previously they sat in limbo; the
            # registry_presence audit flagged us as "missing from
            # awesome-mcp-servers" even though PR #6820 has been
            # OPEN with all checks passing since 2026-05-23.
            cur.execute(
                """SELECT DISTINCT target_name
                   FROM outreach_submissions
                   WHERE outcome IN ('success', 'verified', 'listed',
                                     'audit_pass', 'refresh_ok',
                                     'pr_filed', 'issue_filed')"""
            )
            for (name,) in cur.fetchall():
                confirmed.add(name)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return sorted(confirmed)


# ──────────────────────────────────────────────────────────────────
# Submission ledger — track every outbound attempt so we can audit
# what's been sent, what succeeded, what's stale.
# ──────────────────────────────────────────────────────────────────

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS outreach_submissions (
    id              SERIAL PRIMARY KEY,
    target_key      TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    action          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    http_code       INTEGER,
    detail          TEXT,
    payload_sha     TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS outreach_submissions_target_idx
    ON outreach_submissions (target_key, submitted_at DESC);
"""


def _db():
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not url: return None
    try:
        return psycopg2.connect(url, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _admin_authorized() -> bool:
    """Round 25 (2026-05-23): bridge to internal_auth.is_valid_internal_key
    so the legacy hardcoded key + DCHUB_INTERNAL_KEY env both work,
    matching the auth chain used by /api/v1/admin/heal/purge-stale and
    /api/v1/admin/dedup/run. Previously only accepted exact match of the
    DCHUB_ADMIN_KEY env, which made the registry submit-all unreachable
    when only the legacy key was known."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    # First-class path: internal_auth chain (legacy fallback + env match)
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    # Fallback path: direct env-var match (in case internal_auth fails)
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


def _record(target_key: str, target_name: str, action: str,
             outcome: str, http_code: Optional[int] = None,
             detail: Optional[str] = None,
             payload_sha: Optional[str] = None) -> None:
    """Log to outreach_submissions. Defensive — never raises."""
    conn = _db()
    if conn is None: return
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                INSERT INTO outreach_submissions
                    (target_key, target_name, action, outcome, http_code,
                     detail, payload_sha)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (target_key, target_name, action, outcome, http_code,
                  (detail or "")[:2000], payload_sha))
        conn.commit()
    except Exception as e:
        logger.warning("outreach _record failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass


def _audit_target(target: dict) -> dict:
    """HEAD or GET the audit_url. If the response body contains
    audit_signal (case-insensitive substring match), we're listed.
    Otherwise we've fallen off (or were never listed yet).

    r33-N+ (2026-05-21): support audit_browser_ua flag for registries
    that 403 bot UAs (PulseMCP). Falls back to "informational" status
    when bot-blocked — operator can verify manually."""
    import urllib.request as _ur, urllib.error as _ue
    audit_url = target.get("audit_url")
    signal = target.get("audit_signal")
    if not audit_url or not signal:
        return {"listed": None, "reason": "no_audit_url"}
    use_browser_ua = target.get("audit_browser_ua", False)
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0.0.0 Safari/537.36"
          if use_browser_ua
          else "DCHub-OutreachAudit/1.0 (+https://dchub.cloud)")
    try:
        req = _ur.Request(audit_url, headers={
            "User-Agent": ua,
            "Accept":     "text/html,application/json,*/*;q=0.8",
        })
        with _ur.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            # Case-insensitive substring match — registries often
            # title-case the listing differently than we expect.
            listed = signal.lower() in body.lower()
            return {"listed": listed,
                    "http_code": resp.getcode(),
                    "reason": "ok" if listed else "signal_missing"}
    except _ue.HTTPError as he:
        if he.code == 404:
            return {"listed": False, "http_code": 404,
                    "reason": "page_404 — likely not yet submitted"}
        if he.code == 403 and not use_browser_ua:
            return {"listed": None, "http_code": 403,
                    "reason": "bot_blocked — set audit_browser_ua=True"}
        return {"listed": False, "http_code": he.code,
                "reason": f"http_{he.code}"}
    except Exception as e:
        return {"listed": None, "reason": f"err:{type(e).__name__}"}


# ──────────────────────────────────────────────────────────────────
# Safe-arm guards for the ONLY outward-facing branch (submit_method
# 'form' → live network POST). Mirrors the brain_automerge pattern:
# master OFF-switch + dry-run + kill-switch + per-run cap + a dedup
# cooldown — all read LIVE from env (a Railway flip is honored with no
# redeploy). Default posture is SAFE: the network POST is OFF until
# REGISTRY_SUBMIT_ENABLED=1, and even armed a per-registry cooldown
# blocks re-POSTing the same target within N days. No-op today (no
# 'form' targets exist) — this is future-proofing so adding/repointing
# a 'form' target can never spam a registry. The read-only audit
# (_audit_target) is NEVER gated, so listing visibility is unaffected.
# ──────────────────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

def _submit_enabled() -> bool:   # master gate for the live network POST
    return _truthy(os.environ.get("REGISTRY_SUBMIT_ENABLED"))

def _submit_dry_run() -> bool:   # log "would submit", skip the POST
    return _truthy(os.environ.get("REGISTRY_SUBMIT_DRY_RUN"))

def _submit_disabled() -> bool:  # kill-switch — beats everything
    return _truthy(os.environ.get("REGISTRY_SUBMIT_DISABLE"))

def _submit_cooldown_days() -> int:
    try: return max(0, int(os.environ.get("REGISTRY_SUBMIT_COOLDOWN_DAYS", "7")))
    except Exception: return 7

def _submit_max_per_run() -> int:
    try: return max(0, int(os.environ.get("REGISTRY_SUBMIT_MAX_PER_RUN", "2")))
    except Exception: return 2

def _recently_submitted(target_key: str, days: int) -> bool:
    """True if a live submit POST for this target was already attempted
    within `days` (the dedup cooldown). FAIL-CLOSED: any DB error / no
    DB returns True so we DON'T re-POST when we can't verify — anti-spam
    beats availability here (the audit half still runs regardless)."""
    if days <= 0:
        return False
    conn = _db()
    if conn is None:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                SELECT 1 FROM outreach_submissions
                 WHERE target_key = %s
                   AND action = 'submit'
                   AND outcome IN ('submitted','rejected','http_error','exception')
                   AND submitted_at >= NOW() - (INTERVAL '1 day' * %s)
                 LIMIT 1
            """, (target_key, days))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("outreach cooldown check failed (fail-closed): %s", e)
        return True
    finally:
        try: conn.close()
        except Exception: pass


def _canonical_short_desc() -> str:
    """Derive the outbound pitch's tool count from the canon so it can never
    go stale here again (this was a hardcoded "48 tools" while the live
    tools/list served 73 — the exact drift the honest-numbers guard now
    watches). Falls back to a count-free phrasing if the canon import fails."""
    try:
        from ai_surface_canon import PINNED
        tools = PINNED.get("tools_advertised")
        pub = PINNED.get("public") or {}
        facs = pub.get("facilities") or canon_text("{canon_facilities}")
        if tools:
            return (f"Data center intelligence MCP server. {tools} tools. "
                    f"{facs} facilities.")
    except Exception:
        pass
    return "Data center intelligence MCP server — live facility, grid, fiber & M&A data."


def _submit_target(target: dict, run_state: Optional[dict] = None) -> dict:
    """Submit DC Hub to a single registry. Most registries today
    require manual/PR submission — for those we just LOG the intent
    (so we have a record) and the operator (or L22) opens the PR.

    For form-based or POST-based registries with public submit
    endpoints, we send the actual manifest via POST.

    Always logs to the ledger regardless of outcome."""
    key = target["key"]
    name = target["name"]
    method = target.get("submit_method")

    # Manifest payload from /.well-known/mcp.json — single source of
    # truth, never re-stated in multiple places.
    manifest_url = "https://dchub.cloud/.well-known/mcp.json"

    if method == "refresh_only":
        # We're already listed; the daily cron just exists to AUDIT
        # (verify the listing didn't get pulled). The submit half is
        # a no-op for these.
        _record(key, name, action="refresh_only",
                outcome="noop",
                detail=f"Already listed at {target.get('homepage')}. Audit only.")
        return {"target": key, "outcome": "noop",
                "method": method,
                "listing_url": target.get("homepage"),
                "next_step": "Already listed — audit-only mode"}

    if method == "manual" or method == "github_pr" or method == "anthropic_form":
        # We CAN'T auto-submit. Log the intent so the dashboard
        # surfaces "you owe a PR to this registry" until the audit
        # shows we're listed.
        _record(key, name, action="manual_submit_queued",
                outcome="queued",
                detail=f"Manual submission required at {target.get('manual_url')}. "
                       f"Manifest: {manifest_url}")
        return {"target": key, "outcome": "queued",
                "method": method,
                "manual_url": target.get("manual_url"),
                "next_step": f"Open a PR / fill the form at {target.get('manual_url')}"}

    if method == "form":
        # Some directories accept a JSON POST to /submit even though
        # the user-facing form is HTML. Try POSTing the manifest URL
        # and see what happens.
        import urllib.request as _ur, urllib.error as _ue
        submit_url = target.get("submit_url")
        if not submit_url:
            _record(key, name, "submit", "skipped",
                    detail="no submit_url configured")
            return {"target": key, "outcome": "skipped"}

        # ── safe-arm guards (this is the ONLY branch that POSTs to an
        #    external registry — bound the blast radius before sending) ──
        if _submit_disabled():
            _record(key, name, "submit", "skipped_killswitch",
                    detail="REGISTRY_SUBMIT_DISABLE set")
            return {"target": key, "outcome": "skipped", "reason": "kill_switch"}
        cd = _submit_cooldown_days()
        if _recently_submitted(key, cd):
            _record(key, name, "submit", "skipped_cooldown",
                    detail=f"already submitted within {cd}d — dedup (no re-POST)")
            return {"target": key, "outcome": "skipped",
                    "reason": "cooldown", "cooldown_days": cd}
        if (not _submit_enabled()) or _submit_dry_run():
            why = ("REGISTRY_SUBMIT_ENABLED not set" if not _submit_enabled()
                   else "REGISTRY_SUBMIT_DRY_RUN set")
            _record(key, name, "submit", "dry_run",
                    detail=f"would POST manifest to {submit_url} ({why})")
            return {"target": key, "outcome": "dry_run",
                    "reason": why, "would_post_to": submit_url}
        if run_state is not None:
            cap = _submit_max_per_run()
            if cap > 0 and run_state.get("posts", 0) >= cap:
                _record(key, name, "submit", "skipped_cap",
                        detail=f"per-run network-POST cap {cap} reached")
                return {"target": key, "outcome": "skipped",
                        "reason": "rate_cap", "cap": cap}
            run_state["posts"] = run_state.get("posts", 0) + 1

        try:
            payload = json.dumps({
                "name":        "DC Hub",
                "description": _canonical_short_desc(),
                "url":         "https://dchub.cloud/mcp",
                "manifest":    manifest_url,
                "homepage":    "https://dchub.cloud",
                "transport":   "streamable-http",
                "category":    "data",
                "submitter":   "jonathan@dchub.cloud",
            }).encode("utf-8")
            req = _ur.Request(submit_url, data=payload, method="POST",
                              headers={
                                  "Content-Type": "application/json",
                                  "User-Agent":   "DCHub-Outreach/1.0",
                              })
            with _ur.urlopen(req, timeout=12) as resp:
                code = resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")[:400]
                outcome = "submitted" if 200 <= code < 400 else "rejected"
                _record(key, name, "submit", outcome, http_code=code,
                        detail=body[:400])
                return {"target": key, "outcome": outcome, "http_code": code,
                        "body_preview": body[:200]}
        except _ue.HTTPError as he:
            body = ""
            try: body = he.read().decode("utf-8", errors="replace")[:400]
            except Exception: pass
            _record(key, name, "submit", "http_error",
                    http_code=he.code, detail=body)
            return {"target": key, "outcome": "http_error",
                    "http_code": he.code}
        except Exception as e:
            _record(key, name, "submit", "exception",
                    detail=f"{type(e).__name__}: {str(e)[:200]}")
            return {"target": key, "outcome": "exception",
                    "detail": str(e)[:200]}

    _record(key, name, "submit", "skipped",
            detail=f"unknown method: {method}")
    return {"target": key, "outcome": "skipped",
            "detail": f"unknown method {method}"}


# ──────────────────────────────────────────────────────────────────
# r-escalate (2026-07-03): close the loop the module always promised.
# The submit half can only QUEUE manual/PR/form debts — nothing ever
# CLOSED them (they sat as ledger entries on a dashboard nobody
# reads). Now any live-registry debt that stays not-listed past
# REGISTRY_ESCALATE_DAYS auto-files a GitHub issue on our own repo,
# which lands it in the brain's issue queue — the one loop that
# demonstrably drains (870 closed). Dead/rejected registries
# (_DEAD_REGISTRY_KEYS) never escalate.
# Kill switch: REGISTRY_ESCALATE_DISABLE=1. Window: REGISTRY_ESCALATE_DAYS.
# ──────────────────────────────────────────────────────────────────
_ESCALATE_REPO = "azmartone67/dchub-backend"
_ESCALATE_LABEL = "registry-outreach"
_ESCALATABLE_METHODS = ("manual", "github_pr", "github_issue", "anthropic_form")


def _escalate_days() -> int:
    try: return max(1, int(os.environ.get("REGISTRY_ESCALATE_DAYS", "7")))
    except Exception: return 7


def _recently_escalated(target_key: str, days: int) -> bool:
    """FAIL-CLOSED like _recently_submitted: no DB / error → True so we
    never spam issues when we can't verify the cooldown."""
    conn = _db()
    if conn is None:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                SELECT 1 FROM outreach_submissions
                 WHERE target_key = %s
                   AND action = 'escalated'
                   AND submitted_at >= NOW() - (INTERVAL '1 day' * %s)
                 LIMIT 1
            """, (target_key, days))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("escalation cooldown check failed (fail-closed): %s", e)
        return True
    finally:
        try: conn.close()
        except Exception: pass


def _gh_api(path: str, payload: Optional[dict] = None, method: str = "GET"):
    """Minimal GitHub API call via urllib (matches this module's style).
    Returns (status_code, parsed_json_or_None). Never raises."""
    import urllib.request as _ur, urllib.error as _ue
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return 0, None
    req = _ur.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "DCHub-Outreach/1.0",
            "Content-Type": "application/json",
        })
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8", errors="replace") or "null")
    except _ue.HTTPError as he:
        try: body = json.loads(he.read().decode("utf-8", errors="replace") or "null")
        except Exception: body = None
        return he.code, body
    except Exception as e:
        logger.warning("escalation gh api %s %s failed: %s", method, path, e)
        return 0, None


def _escalate_stale_debts(results: list) -> list:
    """Called at the end of a submit-all cycle with the per-run results
    (each carries a FRESH audit verdict). Files one deduped GitHub
    issue per live registry that is still not listed."""
    if _truthy(os.environ.get("REGISTRY_ESCALATE_DISABLE")):
        return [{"escalation": "disabled"}]
    targets_by_key = {t["key"]: t for t in DISCOVERY_TARGETS}
    days = _escalate_days()
    out = []
    open_issues = None   # lazy-fetched once per run
    for r in results:
        key = r.get("target")
        t = targets_by_key.get(key)
        if not t or key in _DEAD_REGISTRY_KEYS:
            continue
        if t.get("submit_method") not in _ESCALATABLE_METHODS:
            continue
        if r.get("audit", {}).get("listed") is not False:
            continue
        if _recently_escalated(key, days):
            out.append({"target": key, "escalation": "cooldown"})
            continue
        title = f"[registry-debt] {t['name']}: not listed — manual submission owed"
        if open_issues is None:
            # exact-title dedupe via list (NOT search — GitHub search
            # strips the [brackets], same lesson as the fixpack guard)
            code, body = _gh_api(
                f"repos/{_ESCALATE_REPO}/issues?labels={_ESCALATE_LABEL}&state=open&per_page=100")
            open_issues = {i.get("title"): i.get("html_url") for i in (body or [])} if code == 200 else {}
            # ensure the label exists (422 already_exists is fine)
            _gh_api(f"repos/{_ESCALATE_REPO}/labels",
                    {"name": _ESCALATE_LABEL, "color": "1d76db",
                     "description": "auto-filed registry-listing debts (mcp_registry_outreach)"},
                    method="POST")
        if title in open_issues:
            _record(key, t["name"], "escalated", "issue_exists", detail=open_issues[title])
            out.append({"target": key, "escalation": "issue_exists", "issue": open_issues[title]})
            continue
        body_md = (
            f"Auto-filed by the outbound discovery engine (`mcp_registry_outreach.py`).\n\n"
            f"**Registry:** {t['name']} — {t.get('homepage')}\n"
            f"**Submit here:** {t.get('manual_url')}\n"
            f"**Manifest:** https://dchub.cloud/.well-known/mcp.json\n"
            f"**Last audit:** {r.get('audit', {}).get('reason', 'not listed')}\n\n"
            f"This registry is alive but our listing audit shows **not listed** and the debt "
            f"is older than {days}d. Close by submitting (then the daily audit auto-confirms) "
            f"or by adding the key to `_DEAD_REGISTRY_KEYS` with a dated reason if the "
            f"registry is dead/rejected.\n\n"
            f"Ledger: `GET /api/v1/admin/outreach/mcp-registry/status`"
        )
        code, issue = _gh_api(f"repos/{_ESCALATE_REPO}/issues",
                              {"title": title, "body": body_md, "labels": [_ESCALATE_LABEL]},
                              method="POST")
        if code == 201 and issue:
            _record(key, t["name"], "escalated", "issue_filed", detail=issue.get("html_url"))
            out.append({"target": key, "escalation": "issue_filed", "issue": issue.get("html_url")})
        else:
            _record(key, t["name"], "escalated", "issue_error", http_code=code or None,
                    detail=str(issue)[:300] if issue else "no token or network error")
            out.append({"target": key, "escalation": "issue_error", "http_code": code})
    return out


# ──────────────────────────────────────────────────────────────────
# Public endpoints
# ──────────────────────────────────────────────────────────────────


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/submit-all",
    methods=["POST"])
def outreach_submit_all():
    """Run a full outbound cycle: for every target, submit (or queue
    if manual) + audit. Logs everything to outreach_submissions."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401

    results = []
    run_state = {"posts": 0}   # per-run live-POST counter for the cap
    for target in DISCOVERY_TARGETS:
        sub = _submit_target(target, run_state)
        audit = _audit_target(target)
        # Record audit separately so the timeline is visible
        _record(target["key"], target["name"], action="audit",
                outcome=(
                    "listed" if audit.get("listed") is True
                    else "not_listed" if audit.get("listed") is False
                    else "unknown"),
                http_code=audit.get("http_code"),
                detail=audit.get("reason"))
        results.append({
            "target": target["key"],
            "name":   target["name"],
            "submit": sub,
            "audit":  audit,
        })
        # Be a polite outbound citizen — half-second pacing
        time.sleep(0.5)

    # r-escalate: stale live-registry debts become GitHub issues in the
    # brain's queue instead of dying in the ledger (see helper above).
    escalations = _escalate_stale_debts(results)

    summary = {
        "ran_at":     _dt.datetime.utcnow().isoformat() + "Z",
        "targets":    len(results),
        "listed":     sum(1 for r in results if r["audit"].get("listed") is True),
        "not_listed": sum(1 for r in results if r["audit"].get("listed") is False),
        "queued":     sum(1 for r in results if r["submit"].get("outcome") == "queued"),
        "submitted":  sum(1 for r in results if r["submit"].get("outcome") == "submitted"),
        "dry_run":    sum(1 for r in results if r["submit"].get("outcome") == "dry_run"),
        "skipped":    sum(1 for r in results if r["submit"].get("outcome") == "skipped"),
        "escalated":  escalations,
        "guards": {
            "network_post_enabled": _submit_enabled(),
            "dry_run":              _submit_dry_run(),
            "disabled":             _submit_disabled(),
            "cooldown_days":        _submit_cooldown_days(),
            "max_per_run":          _submit_max_per_run(),
            "posts_this_run":       run_state["posts"],
        },
        "results":    results,
    }
    return jsonify(ok=True, summary=summary), 200


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/submit",
    methods=["POST"])
def outreach_submit_one():
    """Submit to a single target. Body: {"target": "smithery"}."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    key = (body.get("target") or "").strip().lower()
    target = next((t for t in DISCOVERY_TARGETS if t["key"] == key), None)
    if not target:
        return jsonify(error="unknown_target",
                       known=[t["key"] for t in DISCOVERY_TARGETS]), 400
    sub = _submit_target(target)
    audit = _audit_target(target)
    # r-fix 2026-07-02: persist the audit like submit-all does — the
    # single-target path returned a live audit but never wrote the
    # ledger row, so /status kept showing the previous cron's verdict.
    _record(target["key"], target["name"], action="audit",
            outcome=(
                "listed" if audit.get("listed") is True
                else "not_listed" if audit.get("listed") is False
                else "unknown"),
            http_code=audit.get("http_code"),
            detail=audit.get("reason"))
    return jsonify(ok=True, submit=sub, audit=audit), 200


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/status",
    methods=["GET"])
def outreach_status():
    """Public read — last-submission timestamps + audit results
    per target. Powers the /distribute page badge row."""
    conn = _db()
    if conn is None:
        return jsonify(error="no_database",
                       targets=[{
                           "key": t["key"], "name": t["name"],
                           "homepage": t["homepage"],
                           "description": t["description"],
                       } for t in DISCOVERY_TARGETS]), 200
    rows_by_target: dict = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LEDGER_DDL)
            cur.execute("""
                SELECT target_key, action, outcome, http_code, detail,
                       submitted_at
                  FROM outreach_submissions
                 WHERE submitted_at > NOW() - INTERVAL '14 days'
                 ORDER BY submitted_at DESC
            """)
            for r in cur.fetchall():
                k = r["target_key"]
                if k not in rows_by_target:
                    rows_by_target[k] = []
                rows_by_target[k].append(dict(r))
    except Exception as e:
        logger.warning("outreach status: %s", e)
    finally:
        try: conn.close()
        except Exception: pass

    out = []
    for t in DISCOVERY_TARGETS:
        recent = rows_by_target.get(t["key"], [])
        last_submit = next((r for r in recent if r["action"] in
                            ("manual_submit_queued", "submit")), None)
        last_audit  = next((r for r in recent if r["action"] == "audit"), None)
        out.append({
            "key":          t["key"],
            "name":         t["name"],
            "homepage":     t["homepage"],
            "description":  t["description"],
            "manual_url":   t.get("manual_url"),
            "submit_method":t.get("submit_method"),
            "last_submit":  {
                "at":       last_submit["submitted_at"].isoformat() if last_submit else None,
                "outcome":  last_submit["outcome"] if last_submit else None,
            } if last_submit else None,
            "last_audit":   {
                "at":       last_audit["submitted_at"].isoformat() if last_audit else None,
                "listed":   last_audit["outcome"] == "listed" if last_audit else None,
                "detail":   last_audit["detail"] if last_audit else None,
            } if last_audit else None,
            "recent_events": len(recent),
        })

    return jsonify(ok=True,
                   targets=out,
                   total_targets=len(DISCOVERY_TARGETS)), 200


# ── the manual queue, made impossible to ignore (2026-08-05) ──────────
#
# ★ WHY THIS EXISTS. Of 12 registry targets, FOUR are refresh_only (already
# listed — the nightly cron just re-audits them). The other eight cannot be
# submitted by any program: a web form, a GitHub PR, a GitHub issue, a sales
# contact. For those, `_submit_target` logs `manual_submit_queued` and returns.
#
# The cron has been doing that every night since 2026-05-21. Nothing ever
# surfaced the queue to a person, so it is a to-do list that regenerates itself
# and that nobody works — the same failure as a fix that is built and never
# wired, wearing a scheduler.
#
# ★ AND MOST OF IT IS NOT THE PRIORITY. 30d attribution: datacolo + smithery =
# 5,078 calls from 3 agents (crawlers); every branded assistant combined = ~775
# calls, 5.1%. Another community directory buys more crawl. Only the PLATFORM
# CATALOGS (Anthropic, Mistral) sit on the path to a user question and
# therefore a licence, so the digest ranks them first instead of listing eight
# equal-looking chores.

_PLATFORM_CATALOG_KEYS = ("anthropic_directory", "mistral_connectors")
_HUMAN_METHODS = ("manual", "github_pr", "github_issue", "anthropic_form")


def manual_queue(rows_by_key: dict | None = None) -> dict:
    """Targets that need a HUMAN, ranked by whether they can produce demand.

    `rows_by_key` maps target_key -> {"first_queued": datetime|None}. Pure
    given that input so the ranking is testable without a database.
    """
    rows_by_key = rows_by_key or {}
    pending, awaiting, done, blocked = [], [], [], []
    for t in DISCOVERY_TARGETS:
        if t.get("submit_method") not in _HUMAN_METHODS:
            continue
        state = t.get("outreach_state", "not_started")
        # ★ _DEAD_REGISTRY_KEYS are documented dead ends: mcphub's data backend
        # has been down since 2026-07-02, mcp_hive's form posts to a 404, and
        # toolhive REJECTED PR #1252 on curated-fit criteria. Each is a chore
        # nobody can complete, and three impossible items in a seven-item list
        # is how the list stops being read. They are SEPARATED, not dropped —
        # a queue that silently loses entries is the failure this replaces.
        if t["key"] in _DEAD_REGISTRY_KEYS:
            state = "blocked"
        row = rows_by_key.get(t["key"]) or {}
        entry = {
            "key": t["key"],
            "name": t["name"],
            "method": t["submit_method"],
            "url": t.get("manual_url") or t.get("submit_url"),
            "url_verified": t.get("submit_url_verified", True),
            "platform_catalog": t["key"] in _PLATFORM_CATALOG_KEYS,
            "state": state,
            "note": t.get("outreach_note"),
            "days_queued": row.get("days_queued"),
        }
        if state == "blocked":
            entry["state"] = "blocked"
            blocked.append(entry)
        elif state == "listed":
            done.append(entry)
        elif state == "awaiting_response":
            awaiting.append(entry)
        else:
            pending.append(entry)

    # Platform catalogs first, then longest-queued — an eight-item list where
    # everything looks equally urgent is a list nobody starts.
    pending.sort(key=lambda e: (not e["platform_catalog"],
                                -(e["days_queued"] or 0)))
    return {
        "ok": True,
        "pending": pending,
        "awaiting_response": awaiting,
        "listed": done,
        "blocked": blocked,
        "counts": {"pending": len(pending), "awaiting": len(awaiting),
                   "listed": len(done), "blocked": len(blocked)},
        "how_to_read": (
            "pending = nobody has submitted it; each is a ONE-TIME human "
            "action that adds a permanent discovery surface. "
            "platform_catalog = Anthropic / Mistral — the only entries whose "
            "payoff is a user question rather than another crawler. "
            "awaiting_response = contacted, no reply yet; it is NOT done. "
            "blocked = no working submission path exists (dead form, rejected "
            "PR); shown so the queue is not silently shorter than reality. "
            "url_verified false = confirm the submission URL before using it, "
            "it was recorded without verification."),
    }


def _queue_ages() -> dict:
    """{target_key: {"days_queued": int}} from the ledger. Never raises;
    a DB failure yields ages of None, which the digest prints as unknown
    rather than as zero."""
    conn = _db()
    if conn is None:
        return {}
    out: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT target_key,
                       EXTRACT(DAY FROM NOW() - MIN(submitted_at))::int
                  FROM outreach_submissions
                 WHERE action = 'manual_submit_queued'
                 GROUP BY target_key
            """)
            for key, days in (cur.fetchall() or []):
                out[key] = {"days_queued": int(days or 0)}
    except Exception as e:  # noqa: BLE001
        logger.warning("manual-queue ages unavailable: %s", str(e)[:120])
    finally:
        try: conn.close()
        except Exception: pass
    return out


def queue_markdown(q: dict) -> str:
    """The digest as markdown, for a GitHub issue body."""
    lines = []
    c = q["counts"]
    lines.append(f"**{c['pending']} registry submissions need a human.** "
                 f"{c['awaiting']} awaiting reply · {c['listed']} listed · "
                 f"{c.get('blocked', 0)} blocked.")
    lines.append("")
    if not q["pending"]:
        lines.append("Nothing pending — every human-submittable target is "
                     "either listed or awaiting a reply.")
    for e in q["pending"]:
        tag = " **← platform catalog, do this one first**" if e["platform_catalog"] else ""
        age = (f" · queued {e['days_queued']}d" if e.get("days_queued")
               else " · never queued")
        warn = ("  \n  ⚠ submission URL is UNVERIFIED — find the real path "
                "before using it, then correct the entry in "
                "`routes/mcp_registry_outreach.py`."
                if not e["url_verified"] else "")
        lines.append(f"- [ ] **{e['name']}** ({e['method']}){age}{tag}  \n"
                     f"  {e['url']}{warn}")
    if q["awaiting_response"]:
        lines.append("")
        lines.append("**Awaiting a reply** (contacted, not done):")
        for e in q["awaiting_response"]:
            lines.append(f"- {e['name']} — {e.get('note') or 'contacted'}")
    if q.get("blocked"):
        lines.append("")
        lines.append("**Blocked** — no working submission path; not chores:")
        for e in q["blocked"]:
            lines.append(f"- {e['name']} — see `description` in "
                         f"`routes/mcp_registry_outreach.py`")
    lines.append("")
    lines.append("_Each pending item is a ONE-TIME action that adds a "
                 "permanent discovery surface. Tick it off by setting "
                 "`outreach_state` on the target in "
                 "`routes/mcp_registry_outreach.py`._")
    return "\n".join(lines)


@mcp_registry_outreach_bp.route(
    "/api/v1/admin/outreach/mcp-registry/manual-queue",
    methods=["GET"])
def outreach_manual_queue():
    """The submissions no program can make. ?format=md for the issue body."""
    if not _admin_authorized():
        return jsonify(ok=False, error="forbidden"), 403
    q = manual_queue(_queue_ages())
    if (request.args.get("format") or "").lower() == "md":
        return queue_markdown(q), 200, {"Content-Type": "text/plain; charset=utf-8"}
    return jsonify(q), 200
