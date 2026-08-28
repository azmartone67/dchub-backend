"""Platform attribution — the artifact for a licence conversation (2026-08-03).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

The paid base is 37 developer keys and 3 enterprise seats against ~1,236 tool
calls a day. A $49 key is the wrong instrument for a platform answering user
questions at volume — no agent holds a credit card. This artifact is what a
licence conversation is built on, which means every number in it will be
re-derived by someone who is not on our side.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── classification: the inflation this exists to avoid ────────────────

def test_tooling_is_never_an_assistant():
    """curl and postman are developer tooling, not a platform answering user
    questions. Folding them in inflates exactly the number a counterparty
    checks first."""
    from routes.platform_attribution import classify_platform
    for name in ("curl", "postman", "insomnia", "mcp-inspector", "node-script"):
        assert classify_platform(name) == "tooling", name
    for name in ("meta-ai", "chatgpt", "claude", "perplexity"):
        assert classify_platform(name) == "assistant", name


def test_an_unclassified_agent_is_unknown_not_assistant():
    """★A user-agent we have not classified is not evidence of a platform
    integration. A total that quietly absorbs it falls apart under scrutiny."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("some-new-crawler-9000") == "unknown"
    assert classify_platform("") == "unknown"
    assert classify_platform(None) == "unknown"


def test_our_own_traffic_is_never_demand():
    from routes.platform_attribution import classify_platform
    assert classify_platform("internal-dchub") == "internal"


def test_there_is_no_grand_total():
    """Summing assistants, tooling and untagged into one headline is the
    inflation this artifact exists to avoid — totals are per-KIND only."""
    src = _src("routes", "platform_attribution.py")
    assert '"by_kind"' in src
    assert '"total_calls"' not in src and '"grand_total"' not in src


# ── the numbers ───────────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows, has_view=True):
        self._rows, self._has, self._mode = rows, has_view, None

    def execute(self, sql, args=None):
        self._mode = "col" if "information_schema" in sql else "rows"

    def fetchone(self):
        return (1 if self._has else 0,) if self._mode == "col" else None

    def fetchall(self):
        return self._rows


def _row(platform, calls=100, agents=5, tools=4):
    return (platform, calls, agents, tools, "2026-07-04", "2026-08-02", 21)


def test_rows_are_classified_and_totalled_by_kind():
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([_row("meta-ai", 5000), _row("curl", 900)]), days=30)
    assert out["ok"] is True
    kinds = {p["platform"]: p["kind"] for p in out["platforms"]}
    assert kinds == {"meta-ai": "assistant", "curl": "tooling"}
    assert out["by_kind"]["assistant"]["calls"] == 5000
    assert out["by_kind"]["tooling"]["calls"] == 900


def test_agents_are_reported_as_a_floor_and_labelled_as_one():
    """★Identity hashes the first X-Forwarded-For hop, so a platform's users
    behind shared egress collapse into one agent. Claiming it as a user count
    would be an overstatement we cannot defend."""
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([_row("meta-ai")]), days=30)
    assert "agents_floor" in out["platforms"][0]
    assert "agents" not in out["platforms"][0]
    assert "FLOOR" in out["methodology"]["agents_floor"]


def test_no_fallback_to_raw_ip_when_the_canon_is_missing():
    """★A number quoted to a counterparty comes from the canonical grain or it
    does not exist. A second-best answer that looks canonical is how every
    surface in this codebase drifted."""
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([], has_view=False), days=30)
    assert out["ok"] is False
    assert "does NOT fall back" in out["error"]


def test_unmeasurable_is_not_zero():
    from routes.platform_attribution import platform_rows

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

        def fetchone(self):
            raise RuntimeError("no db")

        def fetchall(self):
            raise RuntimeError("no db")

    out = platform_rows(_Boom(), days=30)
    assert out["ok"] is False and "UNMEASURED" in out["error"]


# ── the statement handed to the counterparty ──────────────────────────

def test_the_statement_names_the_floor_and_the_exclusions():
    """An artifact that overstates once is never trusted again."""
    from routes.platform_attribution import platform_rows, statement_for
    out = platform_rows(_Cur([_row("meta-ai", 5000, 12, 9)]), days=30)
    s = statement_for("meta-ai", out["platforms"][0], 30)
    assert "5,000" in s
    assert "FLOOR" in s and "not a user count" in s
    assert "Cloudflare POP" in s
    assert "exact" in s.lower()


def test_no_traffic_says_so_plainly():
    from routes.platform_attribution import statement_for
    s = statement_for("meta-ai", None, 30)
    assert "No attributable" in s


def test_the_module_writes_nothing():
    src = _src("routes", "platform_attribution.py")
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE"):
        assert verb not in body.upper(), f"attribution should be read-only: {verb}"


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.platform_attribution import platform_attribution_bp" in src


# ── phase 4: the dead gate stays dead until someone decides ───────────

def test_the_unwired_tier_config_says_so_in_its_first_lines():
    """★Its header states the problem the business still has, written in March.
    Read as live code it says that problem is handled. It is not — and that
    reading is traceable to five months of 'we already gate that'."""
    src = _src("mcp_tier_config.py")
    head = src[:1600]
    assert "NOT WIRED" in head
    assert "tier_registry.py" in head, "the header must name the LIVE gate"


def test_the_dead_gate_is_still_unreachable_from_boot():
    """A pin, not a preference: if someone wires it, this fails and they have
    to do it deliberately rather than by importing a patch script."""
    src = _src("main.py")
    assert "mcp_qa_fixes_v7" not in src
    assert "crawler_scheduler_patch" not in src
    assert "gate_response" not in src


# ── the de-loop name-space bridge (r-platform-kind, 2026-08-27) ───────

# The 30d breakdown as `/api/v1/mcp/funnel` actually served it on 2026-08-27,
# measured keylessly. Pinned as (PLATFORM_CASE name, calls, expected kind).
# ★These are the names the FUNNEL emits, not the ones the canonical view does —
# that difference is the whole reason the bridge exists.
_LIVE_30D = (
    ("smithery connect", 5091, "tooling"),
    ("mcp-generic-client", 4962, "unattributed"),
    ("datacolo", 2560, "harvester"),
    ("unknown", 258, "unknown"),
    ("anthropic/api", 173, "assistant"),
    ("claude", 157, "assistant"),
    ("anthropic/claudeai", 79, "assistant"),
    ("fabrique-c3-idempotency", 64, "unknown"),
    ("connectors-manager", 52, "assistant"),
    ("claude-code", 46, "assistant"),
    ("chatgpt", 33, "assistant"),
    ("claude-ai", 19, "assistant"),
    ("mcphub", 15, "unknown"),
    ("arbor-hive-connector", 4, "unknown"),
    ("skeptic-verifier", 3, "unknown"),
    ("vouch-census", 2, "unknown"),
    ("robinsaige-verifier", 2, "unknown"),
    ("toolrouter", 2, "unknown"),
    ("actionist-apps-verification", 2, "unknown"),
)


def test_every_live_funnel_platform_classifies_as_measured():
    """★The defect this catches: the funnel groups by PLATFORM_CASE, whose names
    are NOT the canonical view's. classify_platform() alone returned `unknown`
    for 79.3% of live volume — including the top two rows, 74% of all calls —
    while looking like it had classified them."""
    from routes.platform_attribution import classify_deloop_platform
    for name, _calls, expected in _LIVE_30D:
        assert classify_deloop_platform(name) == expected, name


def test_the_bridge_leaves_under_three_percent_of_volume_unknown():
    """A share, not a row count: the two rows that mattered were 74% of traffic
    between them, so 'most names classify' can be true while most CALLS do not.
    Unbridged this was 79.3%."""
    from routes.platform_attribution import classify_deloop_platform
    total = sum(c for _n, c, _k in _LIVE_30D)
    unknown = sum(c for n, c, _k in _LIVE_30D
                  if classify_deloop_platform(n) == "unknown")
    assert unknown / total < 0.03, f"{100 * unknown / total:.1f}% unknown"


def test_the_harvester_is_never_folded_into_demand():
    """datacolo took 2,560 calls from 2 agents and sits #3 by volume on a PUBLIC
    breakdown. Any kind but `harvester` publishes a bulk scrape as demand."""
    from routes.platform_attribution import classify_deloop_platform
    assert classify_deloop_platform("datacolo") == "harvester"


def test_the_registry_crawl_is_tooling_not_an_assistant():
    """'smithery connect' is 37.6% of volume from ONE ip — an MCP registry's
    crawl, i.e. distribution plumbing. Called assistant it would nearly double
    every demand number on the dashboard."""
    from routes.platform_attribution import classify_deloop_platform
    assert classify_deloop_platform("smithery connect") == "tooling"


def test_the_generic_client_stays_unattributed_after_the_bucket_rename():
    """PLATFORM_CASE renamed 'mcp' to 'mcp-generic-client' on 2026-07-28 and
    UNATTRIBUTED_PLATFORMS was never updated. ★`unattributed` is a real answer —
    we know it is an MCP client and know we cannot say whose — so it must be
    neither silently demand nor silently noise."""
    from routes.platform_attribution import classify_deloop_platform
    assert classify_deloop_platform("mcp-generic-client") == "unattributed"


def test_the_bridge_does_not_invent_platforms_for_unknown_clients():
    """★The permissive failure this forbids. PLATFORM_CASE's third branch emits
    LOWER(client_name) verbatim, so its vocabulary is OPEN — anyone can mint a
    bucket by setting clientInfo.name. The separator-collapsing retry must not
    turn an unrecognised one into a known vendor."""
    from routes.platform_attribution import classify_deloop_platform
    for name in ("some-new-crawler-9000", "a.b.c", "c l a u d", "n/a", "-",
                 "totally-made-up-agent", "scraper_bot_2000"):
        assert classify_deloop_platform(name) == "unknown", name


def test_the_bridge_decides_names_and_never_kinds():
    """★Single source of truth: every kind must come out of classify_platform().
    If the bridge ever grows its own kind vocabulary the two can disagree, which
    is the drift that produced this bug in the first place."""
    src = _src("routes", "platform_attribution.py")
    bridge = src[src.index("def classify_deloop_platform"):]
    for kind in ("assistant", "tooling", "harvester", "unattributed",
                 "internal"):
        assert f'return "{kind}"' not in bridge, kind


def test_the_public_funnel_labels_every_platform_row():
    """The breakdown is public — this route is the dashboard's aggregate stats
    and carries no admin gate. An unlabelled row there is a harvester presented
    as the #3 platform.

    ★This docstring used to add a second surface, health_json's
    /data/growth.json. That was wrong (#3248) and the route has since been
    deleted in the follow-up to it — pinned below by
    test_the_growth_json_route_stays_deleted."""
    src = _src("flask_mcp_endpoints.py")
    i = src.index('out["calls_by_platform_30d"] = [')
    row = src[i:i + 260]
    assert '"kind"' in row, "calls_by_platform_30d rows must carry kind"
    assert "classify_deloop_platform" in src[i - 1400:i], \
        "kind must come from the bridged classifier, not a local rule"


def test_the_growth_json_route_stays_deleted():
    """★A pin against re-asserting a surface that does not exist.

    #3248 documented that `routes/health_json.py`'s GET /data/growth.json never
    reached Flask — dchub.cloud answers that path from a STATIC Cloudflare Pages
    asset with a different schema, because `_routes.json` `include` lists
    `/data`, the exact path, so the request never reaches the worker. The
    follow-up to #3248 deleted the route instead of documenting it, having
    found all seven routes in that blueprint shadowed the same way. A docstring warning only helps a
    reader who opens the file; deleting it helps the one who greps.

    Full guard, including the other six paths and the CDN cache entries, lives
    in tests/test_health_json_blueprint_stays_deleted.py."""
    import os
    assert not os.path.exists(os.path.join(ROOT, "routes", "health_json.py")), \
        "routes/health_json.py is back — every route it defines is shadowed"


def test_no_source_claims_growth_json_republishes_the_breakdown():
    """The specific false claim, forbidden by name in both files that carried
    it. Cheap to re-introduce by copying the old comment; this makes that
    fail."""
    for parts in (("flask_mcp_endpoints.py",),):
        src = _src(*parts)
        assert "republishes its top" not in src, parts
