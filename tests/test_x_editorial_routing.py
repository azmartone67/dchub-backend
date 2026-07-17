"""X/Twitter editorial routing — 2026-07-17 audit fix.

The 14-day verbatim audit found ALL 7 X posts were the byte-identical
'<City> (<ISO>) rates BUILD on the DC Hub Power Index' template (incl. a
Cheyenne repeat AFTER the 07-14 diversity fix, which only covered the
LinkedIn quad's editorial desk). Root causes locked here:

  1. The template shapers (_shape_twitter/_shape_bluesky) were dead code
     since 07-15 but their queued legacy rows kept draining to X — the
     publish gate now terminal-rejects the retired template on EVERY
     platform, burning off the backlog.
  2. The drumbeat never consulted the editorial desk — it now routes
     through editorial_decision() (kinds + cooldowns + agent_demand) and
     passes the chosen lead into the composer.
  3. X had NO (kind, entity) anti-repeat ledger (the desk's ledger only
     sees quad posts) — rows are now stamped lead_kind/lead_entity and X
     enforces a 14-day window over its own ledger.

DB-free (fake conns), never imports main.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

ce = pytest.importorskip("routes.content_enqueue")  # noqa: E402
cp = pytest.importorskip("content_publisher")  # noqa: E402


# The exact retired template class the audit flagged (X variant).
RETIRED_X_POST = (
    "Cheyenne (SPP) rates BUILD on the DC Hub Power Index this week — "
    "Excess-Power score 70/100 on our public 0-100 grid-headroom index. "
    "Real headroom, shorter interconnection timelines: a green light for "
    "AI siting.\n\nDaily score + methodology: https://dchub.cloud/dcpi/cheyenne"
)


# ── 1. template shapers are gone; the gate kills the queued backlog ──

def test_template_shapers_deleted():
    assert not hasattr(ce, "_shape_twitter")
    assert not hasattr(ce, "_shape_bluesky")


def test_retired_template_regex_matches_the_audit_posts():
    assert cp._RETIRED_TEMPLATE_RE.search(RETIRED_X_POST)
    # LinkedIn variant of the same template class (post 3929's copy)
    assert cp._RETIRED_TEMPLATE_RE.search(
        "Papillion (SPP) rates BUILD on the DC Hub Power Index this week, "
        "with an Excess Power score of 60/100")
    # composed analyst prose does NOT trip it
    assert not cp._RETIRED_TEMPLATE_RE.search(
        "427 GW now sit in ERCOT's interconnection queue — a build signal.")


def test_publish_gate_terminal_rejects_retired_template():
    # The retired-template block fires before any DB use → cur=None is safe.
    skip, why = cp._should_skip_publish(None, RETIRED_X_POST, "twitter")
    assert skip is True
    assert "retired-template" in why


def test_publish_gate_rejects_template_on_linkedin_too():
    skip, why = cp._should_skip_publish(None, RETIRED_X_POST, "linkedin")
    assert skip is True
    assert "retired-template" in why


# ── 2. drumbeat routes through the editorial desk ────────────────────

def _stub_modules(monkeypatch, decision, composed):
    calls = {}

    def _editorial_decision(slot=None):
        calls["slot"] = slot
        return decision

    def _compose_story_post(slot_topic=None, lead=None):
        calls["slot_topic"] = slot_topic
        calls["lead"] = lead
        return composed

    monkeypatch.setitem(
        sys.modules, "routes.media_editorial",
        types.SimpleNamespace(editorial_decision=_editorial_decision))
    monkeypatch.setitem(
        sys.modules, "routes.linkedin_content_engine",
        types.SimpleNamespace(compose_story_post=_compose_story_post))
    return calls


def test_desk_suppress_silences_the_whole_drumbeat(monkeypatch):
    calls = _stub_modules(monkeypatch, {"post": False, "lead": None}, {})
    assert ce._compose_linkedin_analytical({}, None) is None
    assert calls["slot"] == "content_drumbeat"
    # composer must never run on a suppressed slot
    assert "slot_topic" not in calls


def test_desk_lead_maps_kind_and_reaches_composer(monkeypatch):
    lead = {"kind": "deal", "dedup_key": "deal:kkr:nvidia",
            "headline_number": "$10.0B data-center transaction: KKR/Nvidia"}
    calls = _stub_modules(
        monkeypatch, {"post": True, "lead": lead},
        {"text": "x" * 250, "og_image_url": "https://og"})
    out = ce._compose_linkedin_analytical({}, None)
    assert calls["slot_topic"] == "hyperscaler_deal"   # deal → hyperscaler_deal
    assert calls["lead"] is lead                        # lead reaches the composer
    assert out["lead"] is lead                          # ...and rides the result


def test_kind_to_slot_topic_covers_the_desk_kinds():
    m = ce._LEAD_KIND_TO_SLOT_TOPIC
    assert m["agent_demand"] == "agent_demand"
    assert m["deal"] == "hyperscaler_deal"
    assert m["dcpi_mover"] == "dcpi_mover"
    assert m["interconnection"] == "industry_pulse"


def test_lead_entity_token_normalizes_like_the_desk():
    assert ce._lead_entity_token(
        {"dedup_key": "dcpi_mover:cheyenne"}) == "cheyenne"
    assert ce._lead_entity_token(
        {"dedup_key": "deal:kkr:nvidia"}) == "kkrnvidia"
    assert ce._lead_entity_token(None) == ""
    assert ce._lead_entity_token({}) == ""


# ── 3. the X (kind, entity) 14-day ledger ────────────────────────────

class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, row):
        self.cur = _FakeCursor(row)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_x_lead_recently_used_blocks_repeat(monkeypatch):
    conn = _FakeConn(row=(1,))
    monkeypatch.setattr(ce, "_db_conn", lambda: conn)
    assert ce._x_lead_recently_used("dcpi_build", "cheyenne") is True
    sql, params = conn.cur.executed[0]
    assert "platform = 'twitter'" in sql
    assert "make_interval" in sql
    assert params == ("dcpi_build", "cheyenne", 14)


def test_x_lead_recently_used_allows_fresh_lead(monkeypatch):
    conn = _FakeConn(row=None)
    monkeypatch.setattr(ce, "_db_conn", lambda: conn)
    assert ce._x_lead_recently_used("agent_demand", "wk202629") is False


def test_x_lead_recently_used_fails_open(monkeypatch):
    monkeypatch.setattr(ce, "_db_conn", lambda: None)
    assert ce._x_lead_recently_used("deal", "kkrnvidia") is False
    # unstamped rows can never block
    assert ce._x_lead_recently_used(None, None) is False
    assert ce._x_lead_recently_used("deal", "") is False


def test_enqueue_post_stamps_lead_columns(monkeypatch):
    class _InsertCursor(_FakeCursor):
        def fetchone(self):
            # COUNT(*) backlog probe returns 0; INSERT..RETURNING returns id
            last_sql = self.executed[-1][0] if self.executed else ""
            if "COUNT(*)" in last_sql:
                return [0]
            return [42]

    conn = _FakeConn(row=None)
    conn.cur = _InsertCursor(row=None)
    conn.commit = lambda: None
    monkeypatch.setattr(ce, "_db_conn", lambda: conn)
    new_id = ce._enqueue_post("body", "twitter", lead_kind="deal",
                              lead_entity="kkrnvidia")
    assert new_id == 42
    insert_sql, insert_params = conn.cur.executed[-1]
    assert "lead_kind" in insert_sql and "lead_entity" in insert_sql
    assert "deal" in insert_params and "kkrnvidia" in insert_params
