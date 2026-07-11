"""Pure-logic tests for the cadence dead-man sentinel (2026-07-11).

Covers the decision core of routes/cadence_sentinel.py — no Flask app, no
DB, no network (and NEVER imports main: pre-merge pytest has no DB or
JWT_SECRET). The three motivating stalls each get a scenario test:

  • Bluesky publisher dark 29h with 50 approved posts queued
  • gridstatus ingest stuck 7 days (gap rule)
  • LinkedIn verdict queue backing up while sibling paths posted fine
    (queue-only lane: depth accumulates, queue drain = 0)
"""
import datetime as _dt

import routes.cadence_sentinel as cs


def _now():
    return _dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)


# ── parse_ts_defensive: social_media_posts.published_at is TEXT with
#    mixed formats written by several publisher generations ────────────

def test_parse_iso_t_with_micros():
    dt = cs.parse_ts_defensive("2026-07-10T14:23:11.123456")
    assert dt == _dt.datetime(2026, 7, 10, 14, 23, 11, 123456,
                              tzinfo=_dt.timezone.utc)


def test_parse_iso_space_no_micros():
    dt = cs.parse_ts_defensive("2026-07-10 14:23:11")
    assert dt == _dt.datetime(2026, 7, 10, 14, 23, 11,
                              tzinfo=_dt.timezone.utc)


def test_parse_trailing_z():
    dt = cs.parse_ts_defensive("2026-07-10T14:23:11Z")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset() == _dt.timedelta(0)


def test_parse_pg_short_offset():
    # PG emits '+00' (not '+00:00') when a timestamptz is cast to text.
    dt = cs.parse_ts_defensive("2026-07-10 14:23:11.5+00")
    assert dt is not None
    assert dt.utcoffset() == _dt.timedelta(0)


def test_parse_full_offset_preserved():
    dt = cs.parse_ts_defensive("2026-07-10T14:23:11-05:00")
    assert dt is not None
    assert dt.utcoffset() == _dt.timedelta(hours=-5)


def test_parse_bare_date():
    dt = cs.parse_ts_defensive("2026-07-10")
    assert dt == _dt.datetime(2026, 7, 10, tzinfo=_dt.timezone.utc)


def test_parse_datetime_passthrough_naive_becomes_utc():
    naive = _dt.datetime(2026, 7, 10, 8, 0, 0)
    dt = cs.parse_ts_defensive(naive)
    assert dt.tzinfo is not None and dt.hour == 8


def test_parse_garbage_and_empty_return_none():
    for bad in (None, "", "   ", "not-a-date", "07/10/2026 2pm", object()):
        assert cs.parse_ts_defensive(bad) is None


# ── latest_and_recent: one junk row must never blind the lane ─────────

def test_latest_and_recent_mixed_formats_and_junk():
    now = _now()
    values = [
        "2026-07-11T10:00:00",         # 2h ago — in 24h window
        "2026-07-09 12:00:00.000001",  # 48h ago — outside
        "garbage",                     # skipped
        None,                          # skipped
        "2026-07-11T11:30:00Z",        # 30m ago — newest, in window
    ]
    latest, recent = cs.latest_and_recent(values, now, 24)
    assert latest == _dt.datetime(2026, 7, 11, 11, 30,
                                  tzinfo=_dt.timezone.utc)
    assert recent == 2


def test_latest_and_recent_empty_and_all_junk():
    assert cs.latest_and_recent([], _now(), 24) == (None, 0)
    assert cs.latest_and_recent(["x", ""], _now(), 24) == (None, 0)


# ── evaluate_lane: the GAP rule ───────────────────────────────────────

def test_gap_rule_fires_gridstatus_7day_stall():
    spec = {"key": "grid_ext_ingest", "max_gap_hours": 48}
    v = cs.evaluate_lane(spec, age_hours=7 * 24.0)
    assert v["stalled"] is True
    assert any(r.startswith("gap:") for r in v["reasons"])


def test_gap_rule_quiet_within_gap():
    spec = {"key": "grid_ext_ingest", "max_gap_hours": 48}
    v = cs.evaluate_lane(spec, age_hours=12.0)
    assert v["stalled"] is False and v["reasons"] == []


def test_gap_rule_unknown_probe_is_not_a_stall():
    # A failed/NULL freshness probe reports UNKNOWN — never a false alarm.
    spec = {"key": "grid_ext_ingest", "max_gap_hours": 48}
    v = cs.evaluate_lane(spec, age_hours=None)
    assert v["stalled"] is False and v["unknown"] is True


# ── evaluate_lane: the QUEUE rule ─────────────────────────────────────

_BSKY = {"key": "bluesky_publish", "max_gap_hours": 36,
         "queue_sql": "x", "queue_threshold": 10, "drain_window_hours": 24}


def test_queue_rule_fires_bluesky_29h_50_queued():
    # The 07-10 stall: 29h dark (under the 36h gap) BUT 50 approved posts
    # queued with zero drained in 24h — the queue rule catches it early.
    v = cs.evaluate_lane(_BSKY, age_hours=29.0, queue_depth=50,
                         drained_recent=0)
    assert v["stalled"] is True
    assert any(r.startswith("queue:") for r in v["reasons"])


def test_queue_rule_quiet_when_draining():
    v = cs.evaluate_lane(_BSKY, age_hours=2.0, queue_depth=50,
                         drained_recent=3)
    assert v["stalled"] is False


def test_queue_rule_quiet_below_threshold():
    v = cs.evaluate_lane(_BSKY, age_hours=2.0, queue_depth=9,
                         drained_recent=0)
    assert v["stalled"] is False


def test_queue_rule_unknown_drain_never_fires():
    # drained_recent=None means the drain probe failed — must NOT
    # manufacture a stall (only an exact 0 fires the rule).
    v = cs.evaluate_lane(_BSKY, age_hours=2.0, queue_depth=50,
                         drained_recent=None)
    assert v["stalled"] is False


def test_queue_only_lane_linkedin_verdict_backlog():
    # The 07-10 LinkedIn verdict case: sibling paths posted fine, so no
    # freshness gap anywhere — the lane watches its OWN queue: 39 backlog,
    # nothing drained from it in 48h.
    spec = {"key": "smp_linkedin_queue", "queue_sql": "x",
            "queue_threshold": 10, "drain_window_hours": 48}
    v = cs.evaluate_lane(spec, age_hours=1.0, queue_depth=39,
                         drained_recent=0)
    assert v["stalled"] is True
    assert v["unknown"] is False  # no max_gap declared — queue-only


def test_gap_and_queue_can_both_fire():
    v = cs.evaluate_lane(_BSKY, age_hours=48.0, queue_depth=50,
                         drained_recent=0)
    assert v["stalled"] is True and len(v["reasons"]) == 2


# ── the seeded registry itself ────────────────────────────────────────

def test_seeded_registry_validates():
    assert cs.validate_lanes(cs.LANES) == []


def test_seeded_registry_covers_the_motivating_lanes():
    keys = {l["key"] for l in cs.LANES}
    for expected in ("linkedin_publish", "smp_other_publish",
                     "bluesky_publish", "smp_linkedin_queue",
                     "grid_ext_ingest", "iso_lmp_ingest",
                     "iso_queue_ingest", "dcpi_daily_snapshots",
                     "automerge_activity", "ai_citations"):
        assert expected in keys, f"missing seeded lane {expected}"


def test_lane_sql_is_literal_only():
    # Lane SQL runs through cursor.execute(sql) with NO params tuple; a
    # stray % would only be safe by accident. validate_lanes enforces it —
    # prove the enforcement works on a bad lane.
    bad = [{"key": "bad", "label": "x", "why": "x", "max_gap_hours": 1,
            "age_sql": "SELECT 1 WHERE a LIKE 'x%'"}]
    assert any("literal-only" in e for e in cs.validate_lanes(bad))


def test_validate_rejects_duplicate_keys_and_probeless_lanes():
    lanes = [
        {"key": "a", "label": "x", "why": "x", "max_gap_hours": 1,
         "age_sql": "SELECT 1"},
        {"key": "a", "label": "x", "why": "x", "max_gap_hours": 1,
         "age_sql": "SELECT 1"},
        {"key": "b", "label": "x", "why": "x"},
    ]
    errs = cs.validate_lanes(lanes)
    assert any("duplicate" in e for e in errs)
    assert any("no probe" in e for e in errs)


def test_validate_rejects_queue_without_threshold_or_drain_source():
    lanes = [{"key": "q", "label": "x", "why": "x",
              "queue_sql": "SELECT 1"}]
    errs = cs.validate_lanes(lanes)
    assert any("queue_threshold" in e for e in errs)
    assert any("drain" in e for e in errs)


# ── finding identity: stable (issue, url) so the canonical writer's
#    UPDATE-then-INSERT dedupe keys recurrences correctly ──────────────

def test_finding_identity_stable_and_prefixed():
    assert cs.finding_issue("bluesky_publish") == "cadence_stall_bluesky_publish"
    assert cs.finding_url("bluesky_publish") == "/admin/cadence-sentinel#bluesky_publish"
    assert cs.finding_issue("x" * 300).startswith(cs.FINDING_PREFIX)
    assert len(cs.finding_issue("x" * 300)) <= 200


# ── kill switch ───────────────────────────────────────────────────────

def test_kill_switch_env(monkeypatch):
    monkeypatch.setenv("CADENCE_SENTINEL_DISABLE", "1")
    assert cs._disabled() is True
    monkeypatch.setenv("CADENCE_SENTINEL_DISABLE", "0")
    assert cs._disabled() is False
    monkeypatch.delenv("CADENCE_SENTINEL_DISABLE")
    assert cs._disabled() is False
