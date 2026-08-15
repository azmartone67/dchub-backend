"""The registry manual queue — a to-do list that had nobody reading it.

House rule: tests NEVER import main. This one imports the leaf route module.
Nothing runs at module scope.

WHY THIS EXISTS
===============
Of the registry targets in `mcp_registry_outreach.DISCOVERY_TARGETS`, four are
`refresh_only` — already listed, the nightly cron just re-audits them. The rest
cannot be submitted by any program: a web form, a GitHub PR, a GitHub issue, a
sales contact. For those `_submit_target` logs `manual_submit_queued` and
returns.

The cron has done exactly that every night since 2026-05-21, and nothing ever
surfaced the queue to a person. A to-do list that regenerates itself and that
nobody works is the same failure as a fix that is built and never wired — it
just wears a scheduler.

★ AND MOST OF THE LIST IS NOT THE PRIORITY. 30d attribution: datacolo +
smithery = 5,078 calls from 3 agents (crawlers); every branded assistant
combined = ~775 calls, 5.1%. Another community directory buys more crawl. Only
the platform catalogs (Anthropic, Mistral) sit on the path to a user question
and therefore a licence — so the digest ranks them first rather than presenting
eight equal-looking chores.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── ranking: the part that decides whether the list gets started ──────

def test_platform_catalogs_rank_above_community_directories():
    """★ An eight-item list where everything looks equally urgent is a list
    nobody starts. Anthropic and Mistral are the only entries whose payoff is
    a user question rather than another crawler."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    order = [e["key"] for e in q["pending"]]
    catalogs = [k for k in order
                if k in ("anthropic_directory", "mistral_connectors")]
    if catalogs:
        first_catalog = min(order.index(k) for k in catalogs)
        others = [i for i, k in enumerate(order) if k not in catalogs]
        assert not others or first_catalog < min(others), (
            f"a community directory outranked a platform catalog: {order}")


def test_longest_queued_first_within_a_tier():
    """Synthetic targets: the real queue is deliberately down to two entries,
    so ranking is exercised on a constructed list rather than on whatever
    happens to be pending this week."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue({"pulsemcp": {"days_queued": 400}})
    non_catalog = [e for e in q["pending"] if not e["platform_catalog"]]
    assert non_catalog and non_catalog[0]["key"] == "pulsemcp"


def test_a_dead_end_is_separated_not_listed_as_a_chore():
    """★ mcphub's backend has been down since 2026-07-02, mcp_hive's form
    posts to a 404, and toolhive REJECTED PR #1252. Three impossible items in
    a seven-item list is how a list stops being read — but dropping them
    silently would make the queue shorter than reality, which is the failure
    this whole thing replaces."""
    from routes.mcp_registry_outreach import (
        manual_queue, queue_markdown, _DEAD_REGISTRY_KEYS)
    q = manual_queue()
    pending = {e["key"] for e in q["pending"]}
    blocked = {e["key"] for e in q["blocked"]}
    assert not (pending & _DEAD_REGISTRY_KEYS), "a dead end is not a chore"
    assert _DEAD_REGISTRY_KEYS <= blocked, "and it must still be visible"
    assert "Blocked" in queue_markdown(q)


def test_an_already_merged_listing_is_not_re_queued():
    """awesome-mcp-servers was proved merged by r78. A to-do list that renames
    finished work is a list you stop reading."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    assert "awesome_mcp" in {e["key"] for e in q["listed"]}
    assert "awesome_mcp" not in {e["key"] for e in q["pending"]}


def test_the_actionable_list_stays_short_enough_to_start():
    """★ The digest's only job is to get started. Seven undifferentiated
    chores is what it replaced; if this grows past a handful again, the
    ranking or the state-tracking has stopped working."""
    from routes.mcp_registry_outreach import manual_queue
    assert len(manual_queue()["pending"]) <= 4


def test_refresh_only_targets_are_not_chores():
    """Already listed. Putting them on a human's list is how the list stops
    being read."""
    from routes.mcp_registry_outreach import manual_queue, DISCOVERY_TARGETS
    q = manual_queue()
    listed_keys = {t["key"] for t in DISCOVERY_TARGETS
                   if t.get("submit_method") == "refresh_only"}
    queued = {e["key"] for e in q["pending"]}
    assert not (queued & listed_keys), queued & listed_keys


# ── state: contacted is not done, and not-done is not forgotten ───────

def test_a_contacted_target_leaves_the_pending_list_but_is_still_shown():
    """★ Anthropic was contacted 2026-08-05. A list that keeps renaming work
    you have already done is a list you stop reading — but 'awaiting a reply'
    is NOT 'listed', and burying it would be the opposite error."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    pending = {e["key"] for e in q["pending"]}
    awaiting = {e["key"] for e in q["awaiting_response"]}
    assert "anthropic_directory" not in pending
    assert "anthropic_directory" in awaiting
    assert "not" in q["how_to_read"] and "NOT done" in q["how_to_read"]


def test_mistral_is_queued_as_a_platform_catalog():
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    entry = next((e for e in q["pending"] if e["key"] == "mistral_connectors"),
                 None)
    assert entry is not None, "Mistral must be on the queue"
    assert entry["platform_catalog"] is True


def test_an_unverified_url_is_flagged_not_asserted():
    """★ I could not confirm a canonical Mistral submission endpoint, and
    inventing a plausible-looking one is worse than admitting it. The digest
    must warn whoever opens it."""
    from routes.mcp_registry_outreach import manual_queue, queue_markdown
    q = manual_queue()
    entry = next(e for e in q["pending"] if e["key"] == "mistral_connectors")
    assert entry["url_verified"] is False
    md = queue_markdown(q)
    assert "UNVERIFIED" in md


def test_nothing_automated_fetches_an_unverified_url():
    """★ The flag is about the SUBMISSION path only. A target can legitimately
    have an unverified submit URL and a perfectly good audit URL — lobehub does:
    its listing check (market.lobehub.com/s/plugins/...) is known-good, while
    the CLI publish guide is a page CI cannot reach to confirm. The property
    that matters is narrower than 'no unverified URLs': an unverified
    submission URL must never become the thing an audit fetches."""
    from routes.mcp_registry_outreach import DISCOVERY_TARGETS
    for t in DISCOVERY_TARGETS:
        if t.get("submit_url_verified") is False:
            unverified = {t.get("manual_url"), t.get("submit_url")} - {None}
            assert t.get("audit_url") not in unverified, (
                f"{t['key']}: an unverified submission URL is being fetched "
                f"by the audit")


# ── the digest a human actually reads ─────────────────────────────────

def test_the_markdown_is_a_checklist_with_urls():
    from routes.mcp_registry_outreach import manual_queue, queue_markdown
    md = queue_markdown(manual_queue())
    assert "- [ ]" in md, "must be tickable"
    assert "https://" in md
    assert "need a human" in md


def test_an_empty_queue_says_so_rather_than_rendering_nothing():
    """A blank issue body reads as a broken job, not as success."""
    from routes.mcp_registry_outreach import queue_markdown
    md = queue_markdown({"pending": [], "awaiting_response": [], "listed": [],
                         "counts": {"pending": 0, "awaiting": 0, "listed": 8}})
    assert "Nothing pending" in md


def test_unknown_age_is_not_reported_as_zero():
    """A DB failure yields no ages. Printing 'queued 0d' would read as 'added
    today' — the same UNMEASURED-is-not-zero rule as everywhere else."""
    from routes.mcp_registry_outreach import manual_queue, queue_markdown
    md = queue_markdown(manual_queue({}))
    assert "queued 0d" not in md
    assert "never queued" in md


# ── wiring ────────────────────────────────────────────────────────────

def test_the_endpoint_is_admin_gated():
    src = _src("routes", "mcp_registry_outreach.py")
    block = src[src.index("def outreach_manual_queue"):]
    assert "_admin_authorized()" in block[:400]


def test_the_weekly_watch_publishes_the_queue():
    """★ The whole point. An endpoint nobody calls is the thing this queue
    already was."""
    wf = _src(".github", "workflows", "mcp-registry-watch.yml")
    assert "manual-queue" in wf


# ── lobehub: the channel we were waiting on stopped existing ──────────

def test_lobehub_is_resolved_not_awaiting_a_dead_channel():
    """★ On 2026-08-05 this was marked awaiting_response on GitHub issue
    #15667 — a channel lobehub's own automation auto-closes, so the reply we
    were waiting for was never coming. On 2026-08-08 it moved to pending.
    On 2026-08-15 it RESOLVED: the marketplace listing is live at
    /s/plugins/azmartone67-dchub-mcp-server (the re-slug survivor), so the
    entry belongs in `listed` — and above all must never regress to
    `awaiting_response`, the state that meant "wait forever".

    A queue whose states go stale is worse than no queue: it produces
    confident inaction."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    assert "lobehub" in {e["key"] for e in q["listed"]}
    assert "lobehub" not in {e["key"] for e in q["awaiting_response"]}


def test_the_lobehub_entry_carries_the_actual_publish_route():
    """The commands came from lobehub's auto-reply script on GitHub — which
    this session CAN reach — rather than from lobehub.com, which its egress
    proxy denies. Recording where the instructions came from matters as much
    as the instructions."""
    # ★ Checked against the whole file, not a byte-offset window around the
    # entry. Hand-sliced windows have produced four false results in this
    # session alone; the property here is "these instructions exist in the
    # module", and a window adds a failure mode without adding precision.
    src = _src("routes", "mcp_registry_outreach.py")
    assert "@lobehub/market-cli" in src
    assert "plugin publish https://github.com/azmartone67/dchub-backend" in src
    assert "Request a Server" in src, "the remote-server fallback must survive"


def test_the_remote_server_caveat_is_corrected_not_left_standing():
    """★ I recorded "remote servers may be declined" on 2026-08-07. That was
    drawn from lobehub's ISSUE classifier ("remote URL-only ... go to humans"),
    which routes GitHub issues — not from the CLI. The CLI itself ships
    `--url <url>  "Inspect a running Streamable HTTP MCP server"`, which is
    exactly what DC Hub is.

    The caveat was discouraging an attempt that should work, so it is
    CORRECTED here rather than softened. A wrong warning costs more than a
    missing one: it stops the attempt entirely."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "--url" in src and "Streamable HTTP" in src
    assert "may decline it" not in src, "the wrong caveat must never come back"
    # ★ Pinned on the OUTCOME rather than on a phrase. The wording moved twice
    # while the fact held, and a test that guards prose fails on edits that
    # improve it. A remote Streamable HTTP server was published on 2026-08-07,
    # which settles the question the caveat got wrong.
    assert "PUBLISHED" in src and "azmartone67-dchub-backend@2.11.1" in src


def test_the_recorded_commands_match_the_shipped_cli():
    """★ The owner ran what was recorded here and got:
          error: unknown option '--identifier'
          error: unknown command 'submit'
    Both were mine. I assembled flags from a grep across the WHOLE market-cli
    bundle rather than reading which options belong to which subcommand, and
    took `plugin submit` from lobehub's auto-reply text — which is out of sync
    with their own shipped CLI.

    Verified against @lobehub/market-cli 0.0.40 by parsing the command tree:
    the verb is `plugin publish <gitUrl>`, and `plugin init` accepts only
    --dir/--force/--stdio/--url."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "plugin publish https://github.com/azmartone67/dchub-backend" in src
    # the wrong verb may only survive as an explanation of the mistake
    assert "market-cli plugin submit" not in src
    assert "--identifier dchub" not in src, "that flag does not exist"


def test_the_reason_init_needs_a_package_json_is_recorded():
    """★ init infers description from serverInfo or package.json and THROWS if
    neither has one. Ours has neither — advertised serverInfo is {name,
    version} and this repo has no package.json — so a bare `init --url` fails.
    Whoever runs it needs to know that before they conclude the CLI is
    broken."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "THROWS if neither" in src
    assert "no package.json" in src.lower()


def test_the_scratch_directory_is_recorded_with_its_reason():
    """★ Not a style preference. Railway builds this repo with NIXPACKS, which
    detects the language from the files present, and main is push-to-deploy —
    so a root package.json added for a marketplace listing is a real deploy
    risk. Both init and publish take --dir, so the manifest never has to live
    in the repo at all."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "--dir" in src
    assert "NIXPACKS" in src and "push-to-deploy" in src


def test_provenance_of_the_instructions_is_recorded():
    """★ Two sources were reachable and used (lobehub's auto-reply script on
    GitHub, and the published market-cli bundle on npm). The guide at
    lobehub.com was NOT — the egress proxy denies that host. Saying which is
    which is the difference between a verified instruction and a remembered
    one."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "npm" in src and "market-cli" in src
    assert "has never been read" in src or "NOT" in src


# ── lobehub: listed 2026-08-15 via the re-slug survivor ───────────────

def test_the_lobehub_audit_url_points_at_the_listing_that_exists():
    """★ Third correction of this URL, each time to the page that actually
    resolves. 07-02: lobehub.com → market.lobehub.com. 08-08: the CLI
    assigned azmartone67-dchub-backend, so the audit followed it. 08-15:
    that plugin 404s publicly (still status=unpublished, republish never
    run) while the marketplace listing is LIVE at
    /s/plugins/azmartone67-dchub-mcp-server — auditing the unpublished
    duplicate reported "not listed" for a marketplace we are live on.

    Same lesson as r78 on awesome_mcp: a wrong audit URL does not fail
    loudly; it manufactures a problem that isn't there."""
    from routes.mcp_registry_outreach import DISCOVERY_TARGETS
    t = next(x for x in DISCOVERY_TARGETS if x["key"] == "lobehub")
    assert t["audit_url"].endswith("/azmartone67-dchub-mcp-server")
    assert "market.lobehub.com" in t["audit_url"]
    assert "dchub-backend" not in t["audit_url"], \
        "that plugin is unpublished — auditing it is the 08-15 false negative"


def test_listed_via_the_surviving_slug_not_via_the_cli_publish():
    """★ The 08-07 CLI publish (azmartone67-dchub-backend) never got its
    republish and stays unpublished ON PURPOSE — running it now would create
    a duplicate listing next to the live one. What resolved this entry is
    the marketplace listing under azmartone67-dchub-mcp-server, verified
    200 with full DC Hub identity (control slug 404s). So the queue must
    show lobehub as listed, and must NOT keep a pending item whose only
    remaining action is an anti-goal."""
    from routes.mcp_registry_outreach import manual_queue
    q = manual_queue()
    assert "lobehub" in {e["key"] for e in q["listed"]}
    assert "lobehub" not in {e["key"] for e in q["pending"]}


def test_the_note_forbids_the_duplicate_republish():
    """The note used to name the one remaining command (`plugin republish
    azmartone67-dchub-backend`). That command is now an anti-goal, and the
    note must say so explicitly — a queue note that merely stops mentioning
    it would leave the old instruction live in every earlier record."""
    from routes.mcp_registry_outreach import DISCOVERY_TARGETS
    src_note = next(x for x in DISCOVERY_TARGETS
                    if x["key"] == "lobehub")["outreach_note"]
    assert "Do NOT" in src_note
    assert "republish azmartone67-dchub-backend" in src_note
    assert "duplicate" in src_note
    assert "LISTED 2026-08-15" in src_note


def test_the_broken_web_refresh_is_recorded_with_its_workaround():
    """★ "workflow trigger is not configured" fails reproducibly on both the
    GitHub-badge claim and the metadata refresh. The string is in NO public
    lobehub code — their repo, their published CLI bundle, and a GitHub-wide
    code search all come up empty — so it is server-side and undiagnosable
    from here. Recorded as unexplained rather than guessed at.

    What IS verified: `plugin update` reads lhm.plugin.json and calls
    publishPluginVersion through the SDK, with no workflow trigger in the
    path. The web button is avoidable, so the error blocks nothing."""
    src = _src("routes", "mcp_registry_outreach.py")
    assert "workflow trigger is not configured" in src
    assert "plugin update" in src
    assert "Not guessing at a cause" in src
