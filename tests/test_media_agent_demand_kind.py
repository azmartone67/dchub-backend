"""r-agent-demand (2026-07-17): guard the agent-demand editorial kind.

The diagnosed media failure: ANALYST_VOICE demands a first line = metric + how
it MOVED, and never a downgrade lead — but DCPI yields ONE distinct value in 41
daily snapshots, so the writer had no compliant material. agent_demand is the
fix: distinct-agent counts + per-tool distinct callers, which move and move UP.

A PARTIALLY-registered kind is this codebase's known failure mode (pattern in
one registry, missing from the puller/rotation → the kind silently never
posts). These tests pin:
  1. the kind is registered in EVERY registry it must appear in
     (desk: patterns/topic-map/score-seed/cooldown; composer: pullers/
     landing/OG/topic-map/preferred rotation; callers: quad maps + drumbeat),
  2. the lead builder produces a first line with a digit AND a comparative
     ('up from X to Y') that clears leads_with_number, classifies back to
     agent_demand, and never exposes a sub-k-anonymity tool,
  3. missing/flat data → NO lead (honest skip; no fabricated numbers).

NOTE: the CI unit-tests job installs ONLY pytest (not requirements.txt), so we
never import routes.media_editorial / linkedin_content_engine (they import
Flask/psycopg2). We AST-extract the pure pieces and exec them in an isolated
namespace — same pattern as tests/test_media_editorial_classify.py.
"""
import ast
import datetime
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.join(ROOT, "routes", "media_editorial.py")
CE = os.path.join(ROOT, "routes", "linkedin_content_engine.py")
QD = os.path.join(ROOT, "routes", "linkedin_quad_daily.py")
EQ = os.path.join(ROOT, "routes", "content_enqueue.py")


def _extract(path, names, seed=None):
    """Exec just the named top-level Assign/FunctionDef nodes from `path` in an
    isolated namespace (seeded with stdlib deps only). Returns the namespace."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names
                for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            pieces.append(ast.get_source_segment(src, node))
    ns = dict(seed or {})
    exec(compile("\n\n".join(pieces), path, "exec"), ns)
    for n in names:
        assert n in ns, f"{n} not found at top level of {path}"
    return ns


def _dict_literal_keys(path, var_name):
    """String keys of a top-level dict assignment (values may be non-literal,
    e.g. function refs in _PULLERS)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == var_name
                for t in node.targets):
            assert isinstance(node.value, ast.Dict), f"{var_name} is not a dict"
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError(f"{var_name} not found in {path}")


# ── shared extracted namespaces ──────────────────────────────────────
_desk = _extract(
    ME,
    ["_KIND_PATTERNS", "_classify_kind",
     "_YEAR_ONLY", "_HAS_METRIC", "leads_with_number",
     "_KIND_TO_TOPIC", "_KIND_SCORE_SEED", "_KIND_COOLDOWN_OVERRIDES",
     "_AGENT_DEMAND_K_MIN", "_AGENT_DEMAND_MIN_AGENTS",
     "_AGENT_DEMAND_TOOL_LABELS",
     "_agent_demand_metrics", "_agent_demand_lead"],
    seed={"re": re, "_dt": datetime},
)

# ── fixtures mirroring the live payload shapes (verified 2026-07-17) ─
FUNNEL = {"paid_tool_demand_30d": [
    {"tool": "get_grid_intelligence", "calls": 1400, "users": 202},
    {"tool": "get_fiber_intel",       "calls": 900,  "users": 170},
    {"tool": "analyze_site",          "calls": 500,  "users": 110},
    # below the k=5 anonymity floor → must NEVER surface (would identify a customer)
    {"tool": "compare_sites",         "calls": 12,   "users": 3},
]}
REACH = {"distinct_agents_7d": 273, "distinct_platforms": 6}
RETENTION = {"ip_cohort": [
    {"week": "2026-06-29", "distinct_ips": 90,  "new_ips": 77,  "returning_ips": 13},
    {"week": "2026-07-06", "distinct_ips": 160, "new_ips": 140, "returning_ips": 20},
    {"week": "2026-07-13", "distinct_ips": 250, "new_ips": 224, "returning_ips": 26},
]}


# ── 1. registered in EVERY registry ──────────────────────────────────

def test_desk_registries_have_agent_demand():
    assert any(k == "agent_demand" for k, _ in _desk["_KIND_PATTERNS"]), \
        "agent_demand missing from _KIND_PATTERNS"
    assert _desk["_KIND_TO_TOPIC"].get("agent_demand") == "industry_pulse"
    # seeded HIGH while DCPI is flat; dcpi kinds nudged DOWN a notch
    assert _desk["_KIND_SCORE_SEED"].get("agent_demand", 0) >= 0.5
    assert _desk["_KIND_SCORE_SEED"].get("dcpi_build", 1) <= 0.25
    assert _desk["_KIND_SCORE_SEED"].get("dcpi_mover", 2) <= 1.0
    # weekly-refresh data → 3-4 day cooldown
    assert 3 <= _desk["_KIND_COOLDOWN_OVERRIDES"].get("agent_demand", 0) <= 4


def test_composer_registries_have_agent_demand():
    for var in ("_PULLERS", "LANDING_BY_TYPE", "OG_IMAGE_BY_TYPE",
                "_STORY_TYPE_TO_TOPIC"):
        assert "agent_demand" in _dict_literal_keys(CE, var), \
            f"agent_demand missing from {var}"
    src = open(CE, encoding="utf-8").read()
    # rotation: selectable via _pick_story_type's preferred sets
    assert src.count('"agent_demand"') >= 6
    # desk lead forces the matching story type in compose_story_post
    assert 'lead.get("kind") == "agent_demand"' in src
    # honest skip, not a static fallback, when demand data is missing
    assert "skip_no_data" in src


def test_callers_have_agent_demand():
    for var in ("OG_IMAGE_MAP", "LANDING_URL_MAP"):
        assert "agent_demand" in _dict_literal_keys(QD, var), \
            f"agent_demand missing from quad {var}"
    assert '"agent_demand"' in open(QD, encoding="utf-8").read()
    eq_src = open(EQ, encoding="utf-8").read()
    assert 'slot_topic=_drum_topic' in eq_src and '"agent_demand"' in eq_src, \
        "drumbeat (content_enqueue) does not rotate agent_demand"


# ── 2. the lead: digit + comparative first line, gate + classifier ───

def test_lead_first_line_has_digit_and_comparative():
    lead = _desk["_agent_demand_lead"](FUNNEL, REACH, RETENTION)
    assert lead is not None
    assert lead["kind"] == "agent_demand"
    first_line = lead["headline_number"].split("\n", 1)[0]
    assert any(ch.isdigit() for ch in first_line)
    assert any(cmp in first_line.lower()
               for cmp in ("up from", "vs", "triple")), first_line
    # the exact live numbers, honestly labeled
    assert "273" in first_line and "up from 77 to 224" in first_line
    # clears the hard number-lead publish gate
    assert _desk["leads_with_number"](lead["headline_number"]), \
        f"headline bounced off leads_with_number: {lead['headline_number']!r}"
    # engagement attribution round-trips to its own kind (bandit label truth)
    assert _desk["_classify_kind"](lead["headline_number"]) == "agent_demand"
    # top ask surfaces with its distinct-caller count
    assert "get_grid_intelligence" in lead["trend"] and "202" in lead["trend"]
    # score = agents * seed (HIGH — out-ranks flat-DCPI leads)
    assert lead["score"] >= 100


def test_k_anonymity_tool_never_surfaces():
    lead = _desk["_agent_demand_lead"](FUNNEL, REACH, RETENTION)
    blob = (lead["headline_number"] + " " + lead["trend"] + " "
            + lead["so_what"])
    assert "compare_sites" not in blob, \
        "a sub-k-anonymity tool (3 distinct callers) leaked into the lead"


# ── 3. skip on missing / non-moving data (no fabricated numbers) ─────

def test_skip_on_missing_data():
    f = _desk["_agent_demand_lead"]
    assert f({}, {}, {}) is None
    assert f(FUNNEL, REACH, {}) is None                      # no retention
    assert f(FUNNEL, {}, RETENTION) is None                  # no reach
    assert f({}, REACH, RETENTION) is None                   # no funnel
    assert f({"paid_tool_demand_30d": [
        {"tool": "compare_sites", "users": 3}]}, REACH, RETENTION) is None  # all sub-k
    assert f(FUNNEL, {"distinct_agents_7d": 4}, RETENTION) is None  # thin reach


def test_skip_when_trend_is_flat_or_down():
    down = {"ip_cohort": [
        {"week": "2026-06-29", "new_ips": 224},
        {"week": "2026-07-06", "new_ips": 150},
        {"week": "2026-07-13", "new_ips": 77},
    ]}
    flat = {"ip_cohort": [
        {"week": "2026-07-06", "new_ips": 100},
        {"week": "2026-07-13", "new_ips": 100},
    ]}
    # POSITIVE-RESULTS MANDATE: never lead with a decline — suppress instead.
    assert _desk["_agent_demand_lead"](FUNNEL, REACH, down) is None
    assert _desk["_agent_demand_lead"](FUNNEL, REACH, flat) is None
