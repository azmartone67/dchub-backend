"""r-capability-slot (rebuild 2026-07-18): pin the RESERVED 16:00 capability slot.

THE BUG this fixes: DC Hub Media builds 6 evergreen capability data-cards
(brain_capability_radar cap_* leads) that never post. They score 62-64 and lose
EVERY daily slot to agent_demand (~164) and live M&A deals (~98) because
media_editorial.editorial_decision() ranks GLOBALLY and ignores the slot for
selection. Only 2 slots fire/day (linkedin_quad_daily _ACTIVE_SLOT_HOURS={12,16}).

THE FIX (5 parts) these tests pin:
  (a) linkedin_quad_daily.SLOTS[16] is now topic=="capability" (was ai_capex_index).
  (b) editorial_decision("capability") RESTRICTS the ranked slate to capability
      leads only when any exist — a card wins the slot without out-scoring the news.
  (c) FALL-THROUGH: a bad-data day with zero capability leads keeps the full board
      (the reserved slot must never suppress into silence).
  (d) OTHER slots are NOT restricted and caps are NOT excluded elsewhere (the
      ★TRAP: an `elif slot:` exclusion would starve the X drumbeat of the angle).
  (e) the quad retire-guard + mark_capability_announced advance the baseline for a
      cap_* lead (the evergreen kind is cap_<key>, not capability_launch, so repost
      rotation was dead until the guard was widened).
  (f) linkedin_content_engine registers a capability_update story type that composes
      ONLY from the lead's own figures (honesty-fenced, puller skipped).

CI-SAFETY: the unit-tests job installs ONLY pytest (not requirements.txt), so the
source-level tests below are PURE (ast/string parsing, no imports). The functional
tests that must run the real editorial_decision / mark_capability_announced import
routes.* and are SKIPPED when Flask/psycopg2 are unavailable — same guard pattern
as the rest of the media suite. Locally (deps present) they RUN and must pass.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.join(ROOT, "routes", "media_editorial.py")
CE = os.path.join(ROOT, "routes", "linkedin_content_engine.py")
QD = os.path.join(ROOT, "routes", "linkedin_quad_daily.py")
RADAR = os.path.join(ROOT, "routes", "brain_capability_radar.py")


# ── AST / source helpers (pure — always run) ─────────────────────────

def _read(path):
    return open(path, encoding="utf-8").read()


def _slots(path):
    """ast.literal_eval the top-level SLOTS list-of-dicts."""
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SLOTS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("SLOTS not found in " + path)


def _dict_literal_keys(path, var_name):
    """String keys of a top-level dict assignment (values may be non-literal)."""
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == var_name for t in node.targets):
            assert isinstance(node.value, ast.Dict), f"{var_name} is not a dict"
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError(f"{var_name} not found in {path}")


def _extract(path, names, seed=None):
    """Exec just the named top-level Assign/FunctionDef nodes in isolation."""
    src = _read(path)
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            pieces.append(ast.get_source_segment(src, node))
    ns = dict(seed or {})
    exec(compile("\n\n".join(pieces), path, "exec"), ns)
    for n in names:
        assert n in ns, f"{n} not found at top level of {path}"
    return ns


# ── (a) SLOTS[16] is the capability slot ─────────────────────────────

def test_slot_16_is_capability():
    slots = _slots(QD)
    by_hour = {s["hour"]: s for s in slots}
    assert 16 in by_hour, "no 16:00 slot defined"
    assert by_hour[16]["topic"] == "capability", \
        f"16:00 slot topic is {by_hour[16]['topic']!r}, expected 'capability'"
    # the 12:00 NEWS slot is left intact (spec: leave it as-is)
    assert by_hour[12]["topic"] == "hyperscaler_deal"
    # the reserved topic must resolve in the quad's landing/OG maps (else run()
    # KeyErrors before the engine can override them)
    assert "capability" in _dict_literal_keys(QD, "LANDING_URL_MAP")
    assert "capability" in _dict_literal_keys(QD, "OG_IMAGE_MAP")


# ── (f) capability_update story type registered everywhere ───────────

def test_capability_update_story_type_registered():
    for var in ("LANDING_BY_TYPE", "OG_IMAGE_BY_TYPE", "_STORY_TYPE_TO_TOPIC"):
        assert "capability_update" in _dict_literal_keys(CE, var), \
            f"capability_update missing from {var}"
    src = _read(CE)
    # compose_story_post forces the type for a capability lead (mirrors the
    # agent_demand force-type pattern) ...
    assert 'story_type = "capability_update"' in src
    # ... builds data FROM the lead (skips the puller) ...
    assert '"type": "capability_update", "lead": lead' in src
    # ... and there is a dedicated _build_user_prompt branch ...
    assert 'story_type == "capability_update"' in src
    # ... honesty-fenced to the lead's own figures.
    assert "ONLY the figures above" in src or "ONLY numbers you may use" in src \
        or "ONLY the figures" in src


def test_compose_landing_overrides_to_lead_source_url():
    src = _read(CE)
    # landing points at the capability lead's own surface, not the generic default
    assert 'lead.get("source_url")' in src and "landing = _su" in src


# ── editorial helpers exist + the predicate is correct (pure) ────────

_me_helpers = _extract(ME, ["_CAPABILITY_SLOT_TOPICS", "_is_capability_lead"])


def test_capability_slot_topics_set():
    assert "capability" in _me_helpers["_CAPABILITY_SLOT_TOPICS"]


def test_is_capability_lead_predicate():
    f = _me_helpers["_is_capability_lead"]
    # evergreen moat/pillar cards
    assert f({"kind": "cap_tool_catalog"}) is True
    assert f({"kind": "cap_weekly_ledger"}) is True
    # launch / milestone announcements
    assert f({"kind": "capability_launch"}) is True
    assert f({"kind": "data_milestone"}) is True
    # NOT capability leads — the news kinds it must never claim
    assert f({"kind": "agent_demand"}) is False
    assert f({"kind": "hyperscaler_deal"}) is False
    assert f({"kind": "dcpi_build"}) is False
    assert f({}) is False
    assert f({"kind": ""}) is False


# ── (e) quad retire-guard widened to the cap_ prefix ─────────────────

def test_quad_retire_guard_matches_cap_prefix():
    src = _read(QD)
    # the widened guard: the evergreen cap_<key> kinds must trigger the retire
    assert '.startswith("cap_")' in src, \
        "retire guard not widened to match the evergreen cap_<key> kinds"
    # still matches the original launch/milestone kinds too
    assert '"capability_launch", "data_milestone"' in src
    # still calls the baseline writer
    assert "mark_capability_announced" in src


# ── (5) X amplification after a successful capability publish ────────

def test_x_amplify_after_capability_publish():
    src = _read(QD)
    assert "from routes.multiplatform_amplifier import amplify_to_all" in src
    assert 'platforms=["twitter"]' in src
    # gated on a SUCCESSFUL publish of the reserved slot
    assert 'target_slot.get("topic") == "capability"' in src


# ── functional tests (need Flask/psycopg2; skipped in pytest-only CI) ─

try:
    import routes.media_editorial as _me
    import routes.brain_capability_radar as _radar
    _HAVE_ROUTES = True
except Exception:  # pragma: no cover - CI installs pytest only
    _HAVE_ROUTES = False

_needs_routes = pytest.mark.skipif(
    not _HAVE_ROUTES, reason="routes.* need Flask/psycopg2 (pytest-only CI skips)")

# Fixtures: pre-sorted DESC by score, exactly what rank_data_events() returns.
_DEMAND = {"kind": "agent_demand", "headline_number": "273 agents queried DC Hub, up from 77 to 224",
           "trend": "", "so_what": "", "source_url": "https://dchub.cloud/ai",
           "dedup_key": "agent_demand:week", "score": 164.0}
_DEAL = {"kind": "hyperscaler_deal", "headline_number": "$10B data-center deal closed",
         "trend": "", "so_what": "", "source_url": "https://dchub.cloud/hyperscaler-deals",
         "dedup_key": "deal:kkr", "score": 98.0}
_CAP_A = {"kind": "cap_tool_catalog", "headline_number": "DC Hub now serves 79 live MCP tools",
          "trend": "", "so_what": "", "source_url": "https://dchub.cloud/mcp",
          "dedup_key": "cap:tool_catalog", "score": 64.0}
_CAP_B = {"kind": "cap_provenance_envelope", "headline_number": "Every DC Hub answer ships a provenance envelope",
          "trend": "", "so_what": "", "source_url": "https://dchub.cloud/transparency",
          "dedup_key": "cap:provenance_envelope", "score": 63.0}
_CAP_C = {"kind": "capability_launch", "headline_number": "New: international grid telemetry is live",
          "trend": "", "so_what": "", "source_url": "https://dchub.cloud/grid",
          "dedup_key": "capability:intl_grid_telemetry", "score": 62.0}

_MIXED = [_DEMAND, _DEAL, _CAP_A, _CAP_B, _CAP_C]
_NO_CAPS = [_DEMAND, _DEAL]


def _neutralize_db(monkeypatch, ranked):
    """Patch every DB/network helper editorial_decision touches so it runs purely
    on the injected `ranked` slate."""
    monkeypatch.setattr(_me, "rank_data_events", lambda: list(ranked))
    monkeypatch.setattr(_me, "_recently_posted_keys", lambda days=9: set())
    monkeypatch.setattr(_me, "recent_lead_ledger", lambda days=14: [])
    monkeypatch.setattr(_me, "_topic_mix_weights", lambda: {})
    monkeypatch.setattr(_me, "_semantic_repeat_predicate", lambda leads: (lambda l: False))


@_needs_routes
def test_editorial_decision_capability_only(monkeypatch):
    """(b) reserved slot returns ONLY capability leads when present."""
    _neutralize_db(monkeypatch, _MIXED)
    out = _me.editorial_decision("capability")
    assert out["post"] is True
    assert _me._is_capability_lead(out["lead"]), \
        f"reserved slot chose a non-capability lead: {out['lead']['kind']!r}"
    # the whole ranked slate the desk considered was restricted to caps —
    # the higher-scoring agent_demand (164) + deal (98) were dropped for THIS slot
    assert out["ranked"], "ranked slate empty"
    assert all(_me._is_capability_lead(l) for l in out["ranked"]), \
        "a non-capability lead survived the reserved-slot restriction"
    kinds = {l["kind"] for l in out["ranked"]}
    assert "agent_demand" not in kinds and "hyperscaler_deal" not in kinds


@_needs_routes
def test_editorial_decision_capability_fallthrough(monkeypatch):
    """(c) zero capability leads → fall through to the full board (never silence)."""
    _neutralize_db(monkeypatch, _NO_CAPS)
    out = _me.editorial_decision("capability")
    assert out["post"] is True, "reserved slot suppressed into silence on a no-cap day"
    assert out["lead"] is not None
    assert not _me._is_capability_lead(out["lead"]), \
        "expected the full-board fallback lead, got a capability lead"
    # the top news lead (agent_demand, 164) leads the fallback
    assert out["lead"]["kind"] == "agent_demand"


@_needs_routes
def test_editorial_decision_other_slot_not_restricted(monkeypatch):
    """(d) ★TRAP: a NON-reserved slot is NOT restricted and caps are NOT excluded."""
    _neutralize_db(monkeypatch, _MIXED)
    out = _me.editorial_decision("hyperscaler_deal")
    assert out["post"] is True
    # highest-scoring lead wins globally (no restriction) — that's the news, not a cap
    assert out["lead"]["kind"] == "agent_demand"
    # and crucially the capability leads are STILL on the board (not excluded) so the
    # X drumbeat can still reach them — this is the trap the spec calls out
    assert any(_me._is_capability_lead(l) for l in out["ranked"]), \
        "capability leads were excluded from a non-reserved slot (drumbeat starved)"


# ── (e) mark_capability_announced advances the baseline for a cap_* lead ──

class _FakeCur:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur(self._sink)

    def rollback(self):
        pass


@_needs_routes
def test_mark_capability_announced_advances_cap_lead(monkeypatch):
    """(e) a cap_<key> lead's dedup_key ('cap:<key>') resolves to a real evergreen
    registry source and WRITES an advancing baseline row. Before the retire-guard
    was widened this was never called for cap_* leads, so rotation was dead."""
    # tool_catalog is a real evergreen source in the radar REGISTRY
    assert any(s["key"] == "tool_catalog" and s.get("mode") == "evergreen"
               for s in _radar.REGISTRY), "tool_catalog evergreen source missing"

    sink = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(_radar.psycopg2, "connect",
                        lambda *a, **k: _FakeConn(sink))
    # avoid running the source's real metric_sql against a DB
    monkeypatch.setattr(_radar, "_metric",
                        lambda cur, src: ({"tool_catalog": 79}, 79.0))

    ok = _radar.mark_capability_announced("cap:tool_catalog")
    assert ok is True, "mark_capability_announced returned False for a cap_ lead"

    inserts = [(sql, p) for (sql, p) in sink
               if "INSERT INTO data_milestone_snapshots" in sql]
    assert inserts, "no baseline INSERT was executed"
    _, params = inserts[-1]
    assert params[0] == "tool_catalog", \
        f"baseline advanced for the wrong source_key: {params!r}"
    assert params[1] == 79.0


@_needs_routes
def test_mark_capability_announced_rejects_bad_key():
    # a dedup_key with no ':' is rejected (defensive contract)
    assert _radar.mark_capability_announced("nokey") is False
    assert _radar.mark_capability_announced("") is False


@_needs_routes
def test_reserved_slot_bypasses_novelty_gates(monkeypatch):
    """(g) 2026-07-24 unstarve: in prod the novelty/semantic gates judged the
    evergreen cards "not novel" every day, so the reserved slot claimed then
    SUPPRESSED (claimed_in_flight 2/2 days; the card never fired). With every
    novelty gate tripping, the reserved slot must STILL post a cap — rotation
    is owned by the radar's announced/repost ledger, not the desk's gates."""
    _neutralize_db(monkeypatch, _MIXED)
    monkeypatch.setattr(_me, "_semantic_repeat_predicate",
                        lambda leads: (lambda l: True))
    out = _me.editorial_decision("capability")
    assert out["post"] is True, "reserved slot suppressed into silence again"
    assert out.get("reserved_slot_bypass") is True
    assert _me._is_capability_lead(out["lead"])


@_needs_routes
def test_non_reserved_slot_still_suppresses(monkeypatch):
    """(h) the bypass is SCOPED to the reserved slate: a normal slot whose
    whole board is semantically repeated still suppresses (silent beats
    repetitive stays the desk's motto everywhere else)."""
    _neutralize_db(monkeypatch, _MIXED)
    monkeypatch.setattr(_me, "_semantic_repeat_predicate",
                        lambda leads: (lambda l: True))
    out = _me.editorial_decision("hyperscaler_deal")
    assert out["post"] is False
    assert out.get("reserved_slot_bypass") is None


@_needs_routes
def test_reserved_bypass_honors_kind_cooldown(monkeypatch):
    """(i) defense-in-depth: a card whose kind actually LED a post recently
    steps aside for the next due card even inside the bypass."""
    _neutralize_db(monkeypatch, _MIXED)
    monkeypatch.setattr(_me, "_semantic_repeat_predicate",
                        lambda leads: (lambda l: True))
    monkeypatch.setattr(_me, "recent_lead_ledger", lambda days=14: [
        {"kind": "cap_tool_catalog", "entity": "toolcatalog", "days_ago": 1}])
    out = _me.editorial_decision("capability")
    assert out["post"] is True
    assert out["lead"]["kind"] != "cap_tool_catalog", \
        "bypass re-posted the card that just led a post"
