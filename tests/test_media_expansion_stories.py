"""Two-sided tests for the daily expansion-stories lane (2026-08-16).

The lane's contract: detect real expansion signals, compose deterministic
analyst-voice drafts from LIVE values only, route every draft through the
hardened publish guards, and queue survivors into media_story_queue
status='queued' for operator review. Never auto-send.

TWO-SIDED by design (a guard that cannot fail is not a guard):
  PASS side — a realistic draft for each story class clears leads_with_number
              (unit ADJACENT to the number — the #2372 trap), verify_claims,
              the quality bar, and the full _should_skip_publish gauntlet.
  FAIL side — a draft carrying a banned over-claim (the inflated
              fifty-thousand facility count, the unverified M&A dollar
              aggregate) is REJECTED, and the run records it status='rejected'
              with the reason. Banned literals are assembled from fragments so
              the static honest-numbers fence never sees them on a scannable
              line (same trick as routes/media_claim_verify.py itself).

WIRING (registered ≠ armed ≠ runs — each level asserted):
  * crawler_scheduler.SCHEDULE carries the tuple AND _RUNNERS maps the name
    (a SCHEDULE name missing from _RUNNERS silently no-ops — 2026-07-21 class);
  * run_expansion_scan self-stamps cron_last_run (job_name) so the run is
    verifiable from outside;
  * the pending-drafts digest queries media_story_queue with status 'queued'
    (the factory + this lane write 'queued'; the old 'pending'-only filter
    matched nothing any writer produces);
  * cron_heartbeat._DISPATCH drives the digest daily at 15:10+ UTC with
    re-fire suppression (its only prior scheduler was dead dchub-scheduler.py).

CI-SAFETY: no DB, no network. ANTHROPIC_API_KEY / DATABASE_URL are removed per
test so the editor LLM pass fail-opens to skip and canonical_stats uses floors.
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

pytest.importorskip("flask")

mes = pytest.importorskip("routes.media_expansion_stories")  # noqa: E402
cp = pytest.importorskip("content_publisher")  # noqa: E402


# Banned figures, fragment-assembled (mirrors media_claim_verify's own style)
# so the static honest-numbers source fence never sees a live banned literal.
_BANNED_FACILITY_COUNT = "50," + "000"
_BANNED_DOLLAR_AGG = "$" + "32" + "4B"


class _FakeCur:
    """Records SQL; returns empty result sets. Enough for the dedup query in
    _should_skip_publish and for _collect_pending's inventory queries."""

    def __init__(self, rows_by_marker=None):
        self.executed = []
        self.description = []
        self._rows_by_marker = rows_by_marker or {}
        self._last_rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last_rows = []
        for marker, rows in self._rows_by_marker.items():
            if marker in sql:
                self._last_rows = rows
                break

    def fetchall(self):
        return list(self._last_rows)

    def fetchone(self):
        return self._last_rows[0] if self._last_rows else None

    # context-manager + connection shims (run_expansion_scan uses them)
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def connection(self):
        return _FakeConn(cur=self)


class _FakeConn:
    def __init__(self, cur=None):
        self._cur = cur or _FakeCur()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture()
def no_llm_no_db(monkeypatch):
    """Editor LLM pass fail-opens (no key), canonical_stats uses floors."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)


def _stories():
    """Representative story dicts for all three classes (values shaped like
    the live reads observed 2026-08-16, incl. a GB operator for the
    entity-scope side)."""
    return [
        {"kind": "new_facilities_daily", "slug": "global-fleet",
         "name": "Global tracked fleet",
         "detail": {"added_24h": 104, "added_7d": 645,
                    "top_countries_24h": ["US", "DE", "MX"]}},
        {"kind": "operator_fleet_add", "slug": "penta-infra",
         "name": "Penta Infra",
         "detail": {"provider": "Penta Infra", "added_7d": 7,
                    "fleet_total": 21,
                    "new_places": ["Amsterdam", "Frankfurt", "Paris"]}},
        {"kind": "queue_capacity_move", "slug": "pjm", "name": "PJM",
         "detail": {"iso": "PJM", "latest_gw": 171.0, "prior_gw": 164.5,
                    "delta_gw": 6.5, "latest_as_of": "2026-08-16",
                    "prior_as_of": "2026-08-09"}},
        {"kind": "queue_capacity_move", "slug": "neso", "name": "NESO",
         "detail": {"iso": "NESO", "latest_gw": 604.5, "prior_gw": 611.2,
                    "delta_gw": -6.7, "latest_as_of": "2026-08-16",
                    "prior_as_of": "2026-08-01"}},
    ]


# ── PASS side: verifiable expansion stories clear the guards ─────────────────

def test_every_template_leads_with_number(no_llm_no_db):
    """The #2372 trap: leads_with_number needs the unit ADJACENT to the
    number. Every template must open that way or the draft can never be a
    selectable analyst post."""
    from routes.media_editorial import leads_with_number
    for story in _stories():
        draft = mes.compose_draft(story)
        assert draft, f"compose_draft returned nothing for {story['kind']}"
        assert leads_with_number(draft), (
            f"{story['kind']} draft does not lead with number+unit:\n"
            + draft.split("\n")[0])


def test_every_template_passes_claim_verify(no_llm_no_db):
    from routes.media_claim_verify import verify_claims
    for story in _stories():
        draft = mes.compose_draft(story)
        cv = verify_claims(draft)
        assert cv["ok"] and not cv["blocks"], (
            f"{story['kind']} draft blocked by claim-verify: {cv['blocks']}")


def test_every_template_clears_quality_bar(no_llm_no_db):
    """Stat + freshness + named subject + real link — the honest way past
    CONTENT_QUALITY_MIN (0.6 in prod; the factory's 12/12 died at 0.35)."""
    for story in _stories():
        draft = mes.compose_draft(story)
        q = cp._quality_score(draft)
        assert q >= 0.6, f"{story['kind']} draft quality {q} < 0.6:\n{draft}"


def test_every_template_survives_full_gauntlet(no_llm_no_db):
    """The whole _should_skip_publish chain (quality, zero-stat, number-lead,
    disparagement, entity-scope, dedup vs an empty feed, editor skip) plus the
    lane's own claim-verify + fact-check hooks."""
    for story in _stories():
        draft = mes.compose_draft(story)
        passed, reason = mes._guard_check(_FakeCur(), draft, story["detail"])
        assert passed, f"{story['kind']} draft rejected: {reason}\n{draft}"


def test_number_not_read_live_this_run_is_rejected(no_llm_no_db):
    """The pin-to-live-reads guarantee: the SAME draft that passes when its
    figures match the run's live reads is REJECTED when a figure appears that
    the detector did not read this run (a hallucinated/carried-over number —
    'liveness is the product')."""
    story = _stories()[2]  # PJM queue move — GW figures fail closed in
    draft = mes.compose_draft(story)  # verify_media_text unless corroborated
    ok, _ = mes._guard_check(_FakeCur(), draft, story["detail"])
    assert ok, "control: draft must pass with its own live-read detail"
    stale_detail = {"iso": "PJM", "latest_gw": 288.0, "prior_gw": 280.0,
                    "delta_gw": 8.0}
    ok, reason = mes._guard_check(_FakeCur(), draft, stale_detail)
    assert not ok, "a figure absent from this run's live reads must reject"
    assert "live read" in reason or "fact-check" in reason


def test_gb_operator_never_gets_us_framing(no_llm_no_db):
    """The post-100292 class: NESO is the Great Britain operator; its queue
    story must carry GB framing and no share-of-US claim."""
    story = [s for s in _stories() if s["name"] == "NESO"][0]
    draft = mes.compose_draft(story)
    assert "Great Britain" in draft
    assert "US queued" not in draft
    from routes.media_claim_verify import check_entity_scope
    assert check_entity_scope(draft) == []


# ── FAIL side: unverifiable claims are rejected (the guard CAN fail) ─────────

def test_banned_facility_overclaim_is_rejected(no_llm_no_db):
    draft = (f"{_BANNED_FACILITY_COUNT} facilities: DC Hub's tracked fleet "
             "grew again in the last 7 days.\n\nDetails at "
             "https://dchub.cloud/facilities")
    passed, reason = mes._guard_check(_FakeCur(), draft)
    assert not passed, "a banned facility over-claim must be rejected"
    assert "claim" in reason.lower() or "guard" in reason.lower()


def test_banned_dollar_aggregate_is_rejected(no_llm_no_db):
    draft = (f"7 facilities added this week as part of {_BANNED_DOLLAR_AGG} "
             "in tracked deals.\n\nhttps://dchub.cloud/facilities")
    passed, reason = mes._guard_check(_FakeCur(), draft)
    assert not passed, "the unverified M&A dollar aggregate must be rejected"


def test_zero_stat_draft_is_rejected(no_llm_no_db):
    draft = ("0 GW: nothing moved in any interconnection queue this week.\n\n"
             "https://dchub.cloud/markets")
    passed, reason = mes._guard_check(_FakeCur(), draft)
    assert not passed, "a zero-stat headline must be rejected"


def test_empty_draft_is_rejected(no_llm_no_db):
    passed, reason = mes._guard_check(_FakeCur(), "")
    assert not passed


# ── the run: queue on pass, rejected-with-reason on fail ─────────────────────

def _run_with(monkeypatch, story, draft_text):
    """Drive run_expansion_scan with one synthetic story + forced draft text
    against a fake connection; return (conn, cur, result)."""
    cur = _FakeCur()
    conn = _FakeConn(cur=cur)
    monkeypatch.setattr(mes, "detect_new_facilities", lambda c: story)
    monkeypatch.setattr(mes, "detect_operator_fleet_add", lambda c: None)
    monkeypatch.setattr(mes, "detect_queue_move", lambda c: None)
    monkeypatch.setattr(mes, "compose_draft", lambda s: draft_text)
    result = mes.run_expansion_scan(conn=conn)
    return conn, cur, result


def test_run_queues_a_passing_draft_and_stamps_cron(monkeypatch, no_llm_no_db):
    story = _stories()[0]
    good = mes.compose_draft(story)
    conn, cur, result = _run_with(monkeypatch, story, good)
    assert result["ok"] and result["queued"] == 1 and result["rejected"] == 0
    inserts = [s for s, _p in cur.executed if "INSERT INTO media_story_queue" in s]
    assert inserts, "no queue INSERT executed"
    q_params = [p for s, p in cur.executed
                if "INSERT INTO media_story_queue" in s][0]
    assert "queued" in q_params, f"row not queued: {q_params}"
    stamps = [p for s, p in cur.executed if "INSERT INTO cron_last_run" in s]
    assert stamps and stamps[0][0] == mes.JOB_NAME, (
        "run must self-stamp cron_last_run (registered != runs)")


def test_run_records_rejection_with_reason(monkeypatch, no_llm_no_db):
    story = _stories()[0]
    bad = (f"{_BANNED_FACILITY_COUNT} facilities tracked by DC Hub this week."
           "\n\nhttps://dchub.cloud/facilities")
    conn, cur, result = _run_with(monkeypatch, story, bad)
    assert result["queued"] == 0 and result["rejected"] == 1
    q_params = [p for s, p in cur.executed
                if "INSERT INTO media_story_queue" in s][0]
    assert "rejected" in q_params, f"row not marked rejected: {q_params}"
    reason = q_params[-1]
    assert reason, "rejection must carry an auditable reason"


def test_kill_switch_disables_the_lane(monkeypatch):
    monkeypatch.setenv("EXPANSION_STORIES_DISABLE", "1")
    result = mes.run_expansion_scan(conn=_FakeConn())
    assert result.get("skipped") == "disabled"


def test_unreviewed_queued_draft_blocks_a_stack(monkeypatch, no_llm_no_db):
    """Never queue a second unreviewed draft of the same (kind, slug)."""
    cur = _FakeCur(rows_by_marker={"status = 'queued'": [(1,)]})
    assert mes._on_cooldown(cur, "operator_fleet_add", "penta-infra") is True


# ── wiring: registered AND armed, in every layer ─────────────────────────────

def test_schedule_and_runners_both_carry_the_lane():
    """A SCHEDULE name missing from _RUNNERS silently no-ops (2026-07-21
    class) — assert BOTH halves, and that the runner is callable."""
    import crawler_scheduler as cs
    names = [s[2] for s in cs.SCHEDULE]
    assert "expansion_stories" in names, "SCHEDULE tuple missing"
    assert "expansion_stories" in cs._RUNNERS, (
        "_RUNNERS missing — the dispatch guard `name in _RUNNERS` would "
        "silently skip the lane forever")
    assert callable(cs._RUNNERS["expansion_stories"])
    entry = [s for s in cs.SCHEDULE if s[2] == "expansion_stories"][0]
    assert entry[3] == "_run_expansion_stories"
    # CRAWLER_SCHEDULE=once in prod → only hour-1 fires; it must be a real hour
    assert 0 <= entry[0] <= 23


def test_lane_is_armed_by_default(monkeypatch):
    """ARMED means no env flag is needed for the lane to run (queue-only, so
    the safe default is on). The kill switch is the opt-OUT."""
    monkeypatch.delenv("EXPANSION_STORIES_DISABLE", raising=False)
    assert mes._enabled() is True


def test_pending_digest_sees_queued_story_rows():
    """Both writers of media_story_queue produce status='queued'; the digest's
    old 'pending'-only filter surfaced NOTHING either writer creates."""
    mpd = pytest.importorskip("routes.media_pending_digest")
    cur = _FakeCur()
    mpd._collect_pending(cur)
    msq = [s for s, _p in cur.executed if "media_story_queue" in s]
    assert msq, "digest no longer inventories media_story_queue"
    assert any("'queued'" in s for s in msq), (
        "digest must include status='queued' rows or queued drafts never "
        "reach the operator email")


def test_heartbeat_drives_the_pending_digest():
    """The digest's only scheduler used to be dead dchub-scheduler.py. Assert
    the live driver: a _DISPATCH entry firing in the 15:10+ UTC window, with
    re-fire suppression (the send is an email, not idempotent)."""
    ch = pytest.importorskip("routes.cron_heartbeat")
    entries = {e[0]: e for e in ch._DISPATCH}
    assert "media_pending_drafts_digest" in entries, (
        "pending-drafts digest has no live scheduler — drafts queued for "
        "review reach nobody")
    label, url, method, pred = entries["media_pending_drafts_digest"]
    assert "/api/v1/media/pending-drafts/digest" in url and "send=true" in url
    assert method == "POST"
    assert pred(datetime.datetime(2026, 8, 16, 15, 12)) is True
    assert pred(datetime.datetime(2026, 8, 16, 15, 5)) is False
    assert pred(datetime.datetime(2026, 8, 16, 14, 30)) is False
    assert ch._MIN_REFIRE_S.get("media_pending_drafts_digest", 0) >= 3600
