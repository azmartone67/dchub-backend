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
    from routes.mcp_registry_outreach import DISCOVERY_TARGETS
    for t in DISCOVERY_TARGETS:
        if t.get("url_verified") is False:
            assert not t.get("audit_url"), (
                f"{t['key']} has an unverified URL and an audit_url — nothing "
                f"automated should fetch a URL we are not sure about")


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
