#!/usr/bin/env python3
"""Tests for the QA super-user harness.

These guard the DISCIPLINE, not the plumbing. Every assertion here corresponds to
a way this platform has previously fooled itself, and the point of the harness is
that it cannot repeat them:

  * BLIND must never be counted as a failure.
  * A run whose must-fail control did not fire must not publish reassurance.
  * A check with no stated failure condition must not be constructible.
  * A gauge must not be able to vote on the outcome.

Offline by construction — no probe here touches the network. Nothing runs at
module scope (a module-scope exit aborts collection and silently yields a zero-test
"green" run, which shipped twice on 2026-07-28).
"""
from __future__ import annotations

import base64
import datetime
import json

import pytest

from tools.qa_superuser import board
from tools.qa_superuser.finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR,
                                        MINOR, PASS, RED, SEAT_ANON, SEAT_NONE,
                                        Finding, blind, stable_key, summarize)
from tools.qa_superuser.http import MCPSession, body_text
from tools.qa_superuser.probe_data import (parse_interval_hours, parse_when,
                                           walk_dates)
from tools.qa_superuser import probe_web
from tools.qa_superuser.probe_mcp import _is_envelope, seat_comparison_verdict
from tools.qa_superuser.run import invalidate


def _f(**kw):
    base = dict(key="k", surface="mcp", seat=SEAT_ANON, title="t",
                verdict=PASS, severity=INFO, evidence="e",
                basis="anon MCP tools/call X, structuredContent.y",
                red_when="never")
    base.update(kw)
    return Finding(**base)


# ── rule 1: BLIND is not RED ────────────────────────────────────────────────
class TestBlindIsNotFailure:
    def test_blind_finding_never_counts_as_failure(self):
        b = blind(key="x", surface="mcp", seat=SEAT_ANON, title="unreachable",
                  why="timeout", basis="GET /x")
        assert b.verdict == BLIND
        assert b.counts_as_failure is False

    def test_summarize_reports_blind_separately_from_red(self):
        items = [
            blind(key="a", surface="web", seat=SEAT_NONE, title="a", why="w",
                  basis="b"),
            _f(key="b", verdict=RED, severity=MAJOR),
        ]
        s = summarize(items)
        assert s["blind"] == 1
        assert s["red"] == 1
        assert s["failures"] == 1, "only the RED may count as a failure"

    def test_a_wall_of_blind_produces_zero_failures(self):
        # A total outage of the probe must not read as a total outage of dchub.
        items = [blind(key=f"k{i}", surface="web", seat=SEAT_NONE, title="t",
                       why="w", basis="b") for i in range(20)]
        assert summarize(items)["failures"] == 0


# ── rule 2: every check must be able to say RED ─────────────────────────────
class TestRedWhenIsMandatory:
    def test_missing_red_when_is_rejected(self):
        with pytest.raises(ValueError, match="red_when"):
            _f(red_when="   ")

    def test_missing_basis_is_rejected(self):
        # An absence proven with the wrong auth or the wrong field is not an
        # absence — so the seat/field must be recorded to construct a finding.
        with pytest.raises(ValueError, match="basis"):
            _f(basis="")

    def test_bad_verdict_is_rejected(self):
        with pytest.raises(ValueError, match="verdict"):
            _f(verdict="GREENISH")


# ── rule 3: a gauge reports, it never votes ─────────────────────────────────
class TestGaugesDoNotVote:
    @pytest.mark.parametrize("sev", [CRITICAL, MAJOR])
    def test_gauge_cannot_carry_voting_severity(self, sev):
        with pytest.raises(ValueError, match="GAUGE"):
            _f(verdict=GAUGE, severity=sev)

    def test_gauge_is_not_a_failure(self):
        assert _f(verdict=GAUGE, severity=INFO).counts_as_failure is False

    def test_minor_red_is_not_a_failure(self):
        # Severity gates what interrupts a human; a cosmetic red should not.
        assert _f(verdict=RED, severity=MINOR).counts_as_failure is False


# ── the must-fail control ───────────────────────────────────────────────────
class TestCanaryInvalidation:
    def test_passes_are_demoted_when_the_canary_did_not_fire(self):
        items = [
            _f(key="p", verdict=PASS),
            _f(key="r", verdict=RED, severity=MAJOR),
        ]
        out = {f.key: f for f in invalidate(items)}
        assert out["p"].verdict == BLIND, \
            "an unproven PASS must be reported as unobserved, never as green"
        assert out["r"].verdict == RED, \
            "an observed failure is still evidence even from a suspect harness"

    def test_harness_findings_are_left_alone(self):
        items = [_f(key="c", surface="harness", verdict=PASS)]
        assert invalidate(items)[0].verdict == PASS


# ── delta classification ────────────────────────────────────────────────────
def _run(findings):
    return {"generated_at": "2026-08-03T00:00:00+00:00", "canary_fired": True,
            "edge": "https://dchub.cloud",
            "counts": summarize([Finding.from_dict(f) for f in findings]),
            "findings": findings}


class TestDeltaClassification:
    def _state(self, key, failing, transitions=0):
        return {"runs": 3, "findings": {key: {"failing": failing,
                                              "transitions": transitions,
                                              "first_seen": "2026-08-01T00:00:00+00:00"}}}

    def test_unseen_failing_finding_is_new(self):
        run = _run([_f(key="k", verdict=RED, severity=MAJOR).to_dict()])
        assert board.classify(run, {"findings": {}})["k"] == board.NEW

    def test_pass_to_fail_is_regressed(self):
        run = _run([_f(key="k", verdict=RED, severity=MAJOR).to_dict()])
        assert board.classify(run, self._state("k", False))["k"] == board.REGRESSED

    def test_fail_to_pass_is_recovered(self):
        run = _run([_f(key="k", verdict=PASS).to_dict()])
        assert board.classify(run, self._state("k", True))["k"] == board.RECOVERED

    def test_persistent_failure_is_still(self):
        run = _run([_f(key="k", verdict=RED, severity=MAJOR).to_dict()])
        assert board.classify(run, self._state("k", True))["k"] == board.STILL

    def test_repeated_crossings_become_flapping(self):
        run = _run([_f(key="k", verdict=RED, severity=MAJOR).to_dict()])
        state = self._state("k", False, transitions=board.FLAP_THRESHOLD)
        assert board.classify(run, state)["k"] == board.FLAPPING

    def test_only_real_changes_are_announced(self):
        run = _run([_f(key="k", verdict=PASS).to_dict()])
        deltas = board.classify(run, self._state("k", False))
        assert deltas["k"] == board.UNCHANGED
        assert board.changed_lines(run, deltas) == [], \
            "an unchanged board must stay silent — an alarm that fires every " \
            "tick is one nobody reads"

    def test_transitions_increment_on_a_crossing(self):
        run = _run([_f(key="k", verdict=RED, severity=MAJOR).to_dict()])
        merged = board.merge_state(run, self._state("k", False), {})
        assert merged["findings"]["k"]["transitions"] == 1
        assert merged["findings"]["k"]["failing"] is True
        assert merged["findings"]["k"]["first_seen"] == "2026-08-01T00:00:00+00:00"


# ── date handling: the future-date class ────────────────────────────────────
class TestDateParsing:
    @pytest.mark.parametrize("raw", [
        "2026-08-03",
        "2026-09-21T11:00:00",
        "2026-08-03T23:21:46.377617+00:00",
        "Mon, 03 Aug 2026 10:17:55 GMT",
    ])
    def test_every_format_the_platform_actually_emits_parses(self, raw):
        # All four are in simultaneous live use across get_news,
        # get_backup_status and the press feed.
        assert parse_when(raw) is not None

    def test_naive_datetimes_are_treated_as_utc(self):
        dt = parse_when("2026-09-21T11:00:00")
        assert dt.tzinfo is not None, \
            "a naive datetime compared against an aware now() raises; assuming " \
            "local time would also invent hours of drift"

    def test_non_dates_are_rejected(self):
        for junk in ["2.11.1", "", "healthy", None, 12345]:
            assert parse_when(junk) is None

    def test_walk_finds_future_dates_by_field_name(self):
        payload = {"articles": [{"published_at": "2026-09-21T11:00:00",
                                 "title": "Data Center World POWER"}]}
        found = walk_dates(payload)
        assert len(found) == 1
        path, raw, _dt = found[0]
        assert path == "articles[0].published_at"
        assert raw == "2026-09-21T11:00:00"

    def test_version_like_strings_are_not_mistaken_for_dates(self):
        # Flagging a version string as a future date would be exactly the
        # invented-evidence failure this harness exists to prevent.
        assert walk_dates({"version": "2.11.1", "name": "DC Hub"}) == []


class TestIntervalParsing:
    @pytest.mark.parametrize("text,hours", [
        ("6 hours", 6.0),
        ("5 minutes (via autopilot)", 5 / 60),
        ("daily", 24.0),
        ("weekly", 168.0),
    ])
    def test_declared_intervals_parse(self, text, hours):
        assert parse_interval_hours(text) == pytest.approx(hours)

    def test_unstated_interval_returns_none_rather_than_a_default(self):
        # Substituting a default here would invent the very threshold the
        # harness refuses to invent.
        assert parse_interval_hours("on-demand") is None
        assert parse_interval_hours("") is None


# ── envelope classification ─────────────────────────────────────────────────
class TestPaidVersusAnonymous:
    """The check must be able to detect a paying caller getting LESS.

    The live board printed "paid: 9 data fields — anon: 11 data fields" and
    reported PASS, because the verdict was `paid_fields > anon_fields OR
    paid_bytes > anon_bytes` — and bytes are inflated by the very envelope this
    tool discounts. Reverting that OR passed the entire suite, which is why the
    logic is now a pure function with tests.
    """

    def test_more_data_for_the_paying_seat_passes(self):
        verdict, _sev, _t = seat_comparison_verdict(9, 6)
        assert verdict == PASS

    def test_fewer_data_fields_for_the_paying_seat_is_critical(self):
        verdict, sev, title = seat_comparison_verdict(9, 11)
        assert verdict == RED, "paying for less must be detectable"
        assert sev == CRITICAL
        assert "FEWER" in title

    def test_an_identical_field_set_makes_no_claim(self):
        # Values may still be deeper; this probe does not compare depth, so it
        # reports the number rather than inventing a verdict.
        verdict, sev, _t = seat_comparison_verdict(7, 7)
        assert verdict == GAUGE
        assert sev == INFO

    def test_bytes_cannot_rescue_a_worse_field_count(self):
        # The signature takes no byte counts at all — the old failure mode is
        # now unrepresentable rather than merely unused.
        import inspect
        params = inspect.signature(seat_comparison_verdict).parameters
        assert list(params) == ["paid_n", "anon_n"]


class TestEnvelopeClassification:
    @pytest.mark.parametrize("key", [
        "upgrade", "starter_pack", "for_your_human", "quota",
        "_entity", "_gated", "_recent_facilities_total_in_pro",
        # 2026-08-21: the MPP pay offer a gated anon answer carries; a paying
        # seat never gets it, and counting it as data filed a false CRITICAL.
        "machine_pay",
    ])
    def test_selling_and_meta_keys_are_envelope(self, key):
        assert _is_envelope(key) is True

    @pytest.mark.parametrize("key", ["market", "stats", "recent_facilities",
                                     "by_status", "top_providers"])
    def test_answer_keys_are_data(self, key):
        assert _is_envelope(key) is False

    def test_how_much_more_in_pro_is_a_sales_figure_not_data(self):
        # Counting it as data would flatter the ratio with the exact fields the
        # ratio exists to expose.
        assert _is_envelope("_related_intel_total_in_pro") is True


# ── MCP transport ───────────────────────────────────────────────────────────
class TestMCPTransport:
    def test_sse_frame_is_parsed(self):
        body = 'event: message\ndata: {"result":{"ok":true}}\n\n'
        assert MCPSession._parse(body) == {"result": {"ok": True}}

    def test_bare_json_is_parsed(self):
        assert MCPSession._parse('{"result":1}') == {"result": 1}

    def test_unparseable_body_returns_none_rather_than_raising(self):
        assert MCPSession._parse("<html>502</html>") is None

    def test_url_is_stripped(self):
        # A trailing newline in a Railway env value became %0a and raised
        # InvalidURL at urlopen() — twelve patched call sites ago.
        s = MCPSession("https://dchub.cloud/mcp\n  ")
        assert s.url == "https://dchub.cloud/mcp"

    def test_accept_header_carries_both_media_types(self):
        # Streamable HTTP 400s with only one of them — this is why a raw curl
        # initialize fails and a plain-Python client was believed impossible.
        h = MCPSession("https://x/mcp")._headers()
        assert "application/json" in h["Accept"]
        assert "text/event-stream" in h["Accept"]

    def test_probe_traffic_self_identifies(self):
        # Self-exclusion downstream MUST key on the User-Agent: the MCP server
        # overwrites the platform tag, so a platform-based filter excludes
        # nothing while appearing to work.
        assert "dchub-qa-superuser" in MCPSession("https://x/mcp")._headers()["User-Agent"]


class TestBodyDecoding:
    """The MCP endpoint answers text/event-stream with NO charset parameter.

    requests then falls back to ISO-8859-1 per RFC 2616, which latin-1-mangles
    every multi-byte character. Live consequence: a 342 KB tools/list body died
    in json.loads with "Unterminated string" 276 KB in, and the probe reported
    BLIND — but only for the largest responses, which reads as flakiness rather
    than a bug. urllib did not behave this way; the requests migration introduced
    it, so this guards the fix.
    """

    class _Resp:
        def __init__(self, ctype, payload: bytes):
            self.headers = {"Content-Type": ctype}
            self.content = payload
            # What requests would have produced: latin-1 for a bare text/*.
            self.text = payload.decode("iso-8859-1")

    # ★ ensure_ascii=False is load-bearing. json.dumps escapes non-ASCII to \uXXXX
    # by default, which puts NO multi-byte bytes on the wire — so utf-8 and
    # latin-1 decode identically and both tests below pass while proving nothing.
    # The first draft did exactly that: the corruption test failed (correctly
    # exposing the vacuum) and the "decoded as utf8" test passed vacuously beside
    # it. The real server does not escape; it sends raw UTF-8.
    @staticmethod
    def _wire(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def test_charsetless_event_stream_is_decoded_as_utf8(self):
        r = self._Resp("text/event-stream",
                       self._wire({"description": "grid — 20 MW · Ashburn"}))
        assert json.loads(body_text(r))["description"] == "grid — 20 MW · Ashburn"

    def test_the_naive_path_really_does_corrupt(self):
        # Proves the guard is guarding something real rather than restating it.
        r = self._Resp("text/event-stream", self._wire({"d": "—·"}))
        assert json.loads(r.text)["d"] != "—·"

    def test_a_declared_charset_is_respected(self):
        payload = "plain".encode()
        r = self._Resp("text/plain; charset=utf-8", payload)
        assert body_text(r) == "plain"


class TestFrontDoorFieldNames:
    """Locks the field names execute_plan really uses.

    The first version read `steps`/`plan`, found neither, and published a
    CRITICAL "front door failed to produce a plan" against a call that had just
    returned 44 KB and executed three steps. That is the shell-#49 error —
    an absence proven by reading the wrong field — committed by the very harness
    written to prevent it. These assertions are against the live envelope shape.
    """

    @staticmethod
    def _env(sc):
        return {"structuredContent": sc, "content": []}

    def test_executed_is_the_step_list(self):
        from tools.qa_superuser import probe_mcp
        sc = {"executed": [1, 2, 3], "totals": {"steps_run": 3}, "ok": True,
              "replay": {"decisions": []}}
        out = []
        probe_mcp._check_front_door(_StubSession(self._env(sc)), out)
        assert out[0].verdict == PASS
        assert out[0].value == 3

    def test_zero_steps_is_still_red(self):
        from tools.qa_superuser import probe_mcp
        out = []
        probe_mcp._check_front_door(
            _StubSession(self._env({"executed": [], "ok": True})), out)
        assert out[0].verdict == RED, "a genuinely empty plan must still fail"

    def test_declared_not_ok_is_red_even_with_steps(self):
        from tools.qa_superuser import probe_mcp
        out = []
        probe_mcp._check_front_door(
            _StubSession(self._env({"executed": [1], "ok": False})), out)
        assert out[0].verdict == RED


class _StubSession:
    """Minimal stand-in for MCPSession — keeps these tests offline."""

    def __init__(self, env):
        self._env = env

    def call(self, _name, _args):
        return self._env


class TestDeltaHandlesBlindnessAndAbsence:
    """The delta layer must not turn "we could not look" into good news.

    Shipped bug: classify() asked only "is it failing?", which answers NO for a
    BLIND finding — so a RED that became unobservable was announced as
    **RECOVERED**, the single most reassuring output the board can produce, at
    the exact moment the probe went blind. Rule 1 (BLIND is not RED) was
    enforced in finding.counts_as_failure and then quietly broken one layer up
    by reusing that two-way logic for comparison.
    """

    def _prior(self, failing=True, **extra):
        rec = {"failing": failing, "transitions": 0,
               "first_seen": "2026-07-01T00:00:00+00:00",
               "failing_since": "2026-07-01T00:00:00+00:00"}
        rec.update(extra)
        return {"runs": 5, "findings": {"K": rec}}

    def _run(self, findings):
        return {"generated_at": "2026-08-04T01:00:00+00:00", "canary_fired": True,
                "edge": "e",
                "counts": summarize([Finding.from_dict(f) for f in findings]),
                "findings": findings}

    def test_a_red_that_becomes_unobservable_is_not_a_recovery(self):
        b = blind(key="K", surface="mcp", seat=SEAT_ANON, title="unreachable",
                  why="timeout", basis="x").to_dict()
        run = self._run([b])
        deltas = board.classify(run, self._prior())
        assert deltas["K"] != board.RECOVERED, \
            "going blind on a live red must never be reported as a fix"
        assert deltas["K"] == board.WENT_BLIND

    def test_losing_sight_of_a_red_is_announced(self):
        b = blind(key="K", surface="mcp", seat=SEAT_ANON, title="unreachable",
                  why="timeout", basis="x").to_dict()
        run = self._run([b])
        deltas = board.classify(run, self._prior())
        assert board.changed_lines(run, deltas), \
            "an unwatched failure reads as an absent one — say it out loud"

    def test_going_blind_on_something_already_passing_is_quiet(self):
        b = blind(key="K", surface="mcp", seat=SEAT_ANON, title="u", why="w",
                  basis="x").to_dict()
        run = self._run([b])
        deltas = board.classify(run, self._prior(failing=False))
        assert deltas["K"] == board.UNCHANGED
        assert board.changed_lines(run, deltas) == []

    def test_a_blind_run_does_not_erase_a_live_red_from_history(self):
        b = blind(key="K", surface="mcp", seat=SEAT_ANON, title="u", why="w",
                  basis="x").to_dict()
        merged = board.merge_state(self._run([b]), self._prior(), {})
        assert merged["findings"]["K"]["failing"] is True
        assert merged["findings"]["K"]["failing_since"] == "2026-07-01T00:00:00+00:00", \
            "one blind run must not reset how long this has been red"

    def test_a_finding_absent_from_the_run_is_not_a_recovery(self):
        other = _f(key="OTHER", verdict=RED, severity=MAJOR).to_dict()
        run = self._run([other])
        deltas = board.classify(run, self._prior())
        assert deltas["K"] == board.DISAPPEARED, \
            "a red whose probe stopped producing it looks exactly like a fix"
        assert any("VANISHED" in line for line in board.changed_lines(run, deltas))

    def test_an_absent_findings_history_survives(self):
        other = _f(key="OTHER", verdict=RED, severity=MAJOR).to_dict()
        merged = board.merge_state(self._run([other]), self._prior(), {})
        assert "K" in merged["findings"], "history must not be silently deleted"
        assert merged["findings"]["K"]["absent_runs"] == 1

    def test_a_long_absent_finding_is_eventually_forgotten(self):
        other = _f(key="OTHER", verdict=RED, severity=MAJOR).to_dict()
        prior = self._prior(absent_runs=board.ABSENT_RUNS_BEFORE_FORGET)
        merged = board.merge_state(self._run([other]), prior, {})
        assert "K" not in merged["findings"], \
            "a genuinely retired check must not accumulate forever"

    def test_a_gauge_is_not_evidence_that_a_red_was_fixed(self):
        # Observed live: the anon quota check flips RED <-> GAUGE with the
        # runner's trial state. Treating GAUGE as PASSING published
        # "**RECOVERED** — No numeric quota meter exposed to an anonymous
        # caller" — a fix announced for a check that had merely stopped
        # asserting. A gauge makes no pass/fail claim; that is its definition.
        g = _f(key="K", verdict=GAUGE, severity=INFO).to_dict()
        run = self._run([g])
        assert board.classify(run, self._prior())["K"] != board.RECOVERED
        assert board.classify(run, self._prior())["K"] == board.WENT_BLIND

    def test_a_gauge_that_was_never_failing_stays_quiet(self):
        g = _f(key="K", verdict=GAUGE, severity=INFO).to_dict()
        run = self._run([g])
        deltas = board.classify(run, self._prior(failing=False))
        assert deltas["K"] == board.UNCHANGED
        assert board.changed_lines(run, deltas) == []

    def test_an_observed_pass_is_still_a_real_recovery(self):
        run = self._run([_f(key="K", verdict=PASS).to_dict()])
        assert board.classify(run, self._prior())["K"] == board.RECOVERED, \
            "the fix must not suppress genuine good news"


class TestFlappingIsAnnouncedOnce:
    """A check that genuinely oscillates must not notify on every flip.

    Observed live: the anon quota probe flips RED <-> GAUGE with the runner IP's
    anonymous-trial state, so it would comment on the board every few hours
    forever — the "alarm nobody reads" failure this board exists to avoid. Its
    current verdict stays in the rendered body every run; the NOTIFICATION fires
    once, when we learn the check is unstable.
    """

    def _run(self, findings):
        return {"generated_at": "2026-08-04T03:00:00+00:00", "canary_fired": True,
                "edge": "e",
                "counts": summarize([Finding.from_dict(f) for f in findings]),
                "findings": findings}

    def _flappy(self, announced):
        return {"runs": 9, "findings": {"K": {
            "failing": False, "transitions": board.FLAP_THRESHOLD,
            "flap_announced": announced,
            "first_seen": "2026-07-01T00:00:00+00:00"}}}

    def test_first_flap_is_announced(self):
        run = self._run([_f(key="K", verdict=RED, severity=MAJOR).to_dict()])
        deltas = board.classify(run, self._flappy(False))
        assert deltas["K"] == board.FLAPPING
        assert board.changed_lines(run, deltas, self._flappy(False))

    def test_subsequent_flaps_stay_silent(self):
        run = self._run([_f(key="K", verdict=RED, severity=MAJOR).to_dict()])
        state = self._flappy(True)
        deltas = board.classify(run, state)
        assert deltas["K"] == board.FLAPPING
        assert board.changed_lines(run, deltas, state) == [], \
            "an oscillating check must not notify on every flip"

    def test_the_announcement_is_remembered(self):
        run = self._run([_f(key="K", verdict=RED, severity=MAJOR).to_dict()])
        state = self._flappy(False)
        merged = board.merge_state(run, state, {"K": board.FLAPPING})
        assert merged["findings"]["K"]["flap_announced"] is True

    def test_an_unstable_red_is_still_visible_on_the_board(self):
        # Silence in the comment stream must not mean silence on the board.
        f = _f(key="K", verdict=RED, severity=MAJOR).to_dict()
        body = board.render(self._run([f]), self._flappy(True), {},
                            memory=(True, "ok"))
        assert "UNSTABLE" in body


class TestUnreadableStateIsNeverOverwritten:
    def test_merge_is_not_saved_when_the_prior_read_failed(self, monkeypatch):
        # state["findings"] is empty because the READ failed, not because there
        # is no history. Saving that would overwrite a real board with one run.
        saved = []
        monkeypatch.setattr(board, "ensure_state_branch", lambda: None)
        monkeypatch.setattr(board, "load_state",
                            lambda: {"unreadable": "boom", "findings": {}, "runs": 0})
        monkeypatch.setattr(board, "save_state",
                            lambda s: (saved.append(s), (True, "ok"))[1])
        monkeypatch.setattr(board, "upsert_issue", lambda *a, **k: None)
        f = _f(key="K", verdict=RED, severity=MAJOR).to_dict()
        board.actuate({"generated_at": "2026-08-04T01:00:00+00:00",
                       "canary_fired": True, "edge": "e",
                       "counts": summarize([Finding.from_dict(f)]),
                       "findings": [f]})
        assert saved == [], \
            "losing real history to a transient API blip is worse than " \
            "skipping one write"


class TestStatePersistence:
    """The memory layer, which was dead on arrival and looked fine.

    Shipped bug: save_state() stripped underscore keys BEFORE reading `_sha` from
    the stripped dict, so the sha was always None and the update degraded to a
    create -> HTTP 422 "sha wasn't supplied" on every run after the first. The
    fallback _current_sha() failed too, because `gh api <path> -f ref=<branch>`
    makes gh switch to POST. load_state() had the same `-f ref=` bug, and its
    error text contained "404", so it took the first-run branch EVERY run.

    Net effect: no run ever had memory, every red was re-announced as NEW, and
    the only symptom was one "non-fatal" line in a 200-line log.
    """

    def test_sha_is_read_before_underscore_keys_are_stripped(self, monkeypatch):
        seen = {}

        def fake_gh(args, input_text=None):
            seen["args"] = args
            return 0, "{}"

        monkeypatch.setattr(board, "_gh", fake_gh)
        monkeypatch.setattr(board, "_current_sha", lambda: None)
        ok, _detail = board.save_state({"runs": 2, "findings": {}, "_sha": "abc123"})
        assert ok
        assert "sha=abc123" in seen["args"], \
            "the sha carried in state must reach the PUT, or the update " \
            "degrades to a create and 422s once the file exists"

    def test_underscore_keys_are_not_written_to_the_file(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (seen.setdefault("args", a), (0, "{}"))[1])
        monkeypatch.setattr(board, "_current_sha", lambda: None)
        board.save_state({"runs": 2, "findings": {}, "_sha": "abc"})
        content = next(a.split("content=", 1)[1] for a in seen["args"]
                       if a.startswith("content="))
        assert "_sha" not in base64.b64decode(content).decode()

    def test_a_failed_write_is_reported_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (1, '{"status":"422"}'))
        monkeypatch.setattr(board, "_current_sha", lambda: None)
        ok, detail = board.save_state({"runs": 1, "findings": {}})
        assert ok is False
        assert "422" in detail, "the caller must receive the reason, not just False"

    def test_state_reads_use_GET_not_an_implicit_POST(self, monkeypatch):
        calls = []

        def fake_gh(args, input_text=None):
            calls.append(args)
            return 1, "Not Found"

        monkeypatch.setattr(board, "_gh", fake_gh)
        board.load_state()
        args = calls[0]
        assert "--method" in args and args[args.index("--method") + 1] == "GET"
        assert not any(a == "-f" for a in args), \
            "`gh api -f ref=...` makes gh POST; the ref must be a query param"
        assert any("?ref=" in a for a in args)

    def test_only_a_real_absence_counts_as_a_first_run(self, monkeypatch):
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (1, "HTTP 404: Not Found"))
        assert board.load_state().get("first_run") is True

    def test_an_unrelated_error_mentioning_404_is_unreadable_not_first_run(
            self, monkeypatch):
        # The shipped bug: a POST rejection whose text merely CONTAINED "404"
        # was read as "no prior state", silently wiping history every run.
        monkeypatch.setattr(
            board, "_gh",
            lambda a, input_text=None: (1, 'gateway error, upstream returned 404x'))
        st = board.load_state()
        assert st.get("first_run") is not True
        assert st.get("unreadable"), \
            "an ambiguous failure must be reported as unreadable history, " \
            "never as an empty history"


class TestBoardRendersItsOwnHealth:
    def _run(self):
        f = _f(key="k", verdict=PASS).to_dict()
        return {"generated_at": "2026-08-04T00:00:00+00:00", "canary_fired": True,
                "edge": "https://dchub.cloud",
                "counts": summarize([Finding.from_dict(f)]), "findings": [f]}

    def test_a_failed_memory_write_is_declared_on_the_board(self):
        body = board.render(self._run(), {"runs": 3}, {},
                            memory=(False, '422 sha wasn\'t supplied'))
        assert "NO MEMORY" in body
        assert "422" in body, "the operator needs the actual reason on the board"

    def test_a_healthy_run_carries_no_memory_warning(self):
        body = board.render(self._run(), {"runs": 3}, {}, memory=(True, "ok"))
        assert "NO MEMORY" not in body

    def test_unreadable_history_suppresses_delta_claims_visibly(self):
        body = board.render(self._run(), {"runs": 0, "unreadable": "boom"}, {},
                            memory=(True, "ok"))
        assert "unreadable" in body.lower()


class TestDashboardBeat:
    """Posting the board to the backend must never cost us the real report.

    The GitHub issue is authoritative and is already written by the time the
    beat runs. This convenience view is hosted on the very backend being probed,
    so a dchub outage must not turn into a failed probe run — the probe exists to
    observe dchub outages.
    """

    def _run(self):
        f = _f(key="K", verdict=RED, severity=MAJOR).to_dict()
        return {"generated_at": "2026-08-04T03:00:00+00:00", "canary_fired": True,
                "edge": "https://dchub.cloud",
                "counts": summarize([Finding.from_dict(f)]), "findings": [f]}

    def test_no_admin_key_is_handled_not_raised(self, monkeypatch):
        monkeypatch.setattr(board.C, "ADMIN_KEY", "")
        ok, note = board.beat_dashboard(self._run(), {"findings": {}})
        assert ok is False and "admin key" in note

    def test_a_transport_failure_is_reported_not_raised(self, monkeypatch):
        import requests
        monkeypatch.setattr(board.C, "ADMIN_KEY", "k")

        def boom(*a, **k):
            raise requests.ConnectionError("dchub is down")

        monkeypatch.setattr(requests, "post", boom)
        ok, note = board.beat_dashboard(self._run(), {"findings": {}})
        assert ok is False
        assert "ConnectionError" in note, \
            "a dchub outage must be reported, and must not raise"

    def test_the_beat_targets_the_origin_not_the_edge(self, monkeypatch):
        # ★ The CF zone's 15s route timeout 503s admin POSTs — verified on the
        # same request: edge 503, Railway origin 200. Sending this through
        # dchub.cloud would fail on every single run.
        import requests
        seen = {}
        monkeypatch.setattr(board.C, "ADMIN_KEY", "k")

        class R:
            status_code = 200
            text = "{}"

        monkeypatch.setattr(requests, "post",
                            lambda url, **kw: (seen.update(url=url, kw=kw), R())[1])
        board.beat_dashboard(self._run(), {"findings": {}})
        assert "railway" in seen["url"], seen["url"]
        assert "dchub.cloud" not in seen["url"]
        assert seen["kw"]["headers"]["X-Admin-Key"] == "k"

    def test_history_is_attached_so_the_page_can_show_age(self, monkeypatch):
        import requests
        seen = {}
        monkeypatch.setattr(board.C, "ADMIN_KEY", "k")

        class R:
            status_code = 200
            text = "{}"

        monkeypatch.setattr(requests, "post",
                            lambda url, **kw: (seen.update(kw=kw), R())[1])
        merged = {"findings": {"K": {"failing_since": "2026-07-01T00:00:00+00:00",
                                     "transitions": 6}}}
        board.beat_dashboard(self._run(), merged)
        sent = json.loads(seen["kw"]["data"])
        assert sent["findings"][0]["failing_since"] == "2026-07-01T00:00:00+00:00"
        assert sent["findings"][0]["transitions"] == 6


class TestDashboardRoute:
    def _client(self, monkeypatch):
        import flask
        from routes import qa_superuser_dashboard as mod
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "secret")
        app = flask.Flask(__name__)
        app.register_blueprint(mod.qa_superuser_dashboard_bp)
        return app.test_client(), mod

    def test_the_page_requires_a_key(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        assert client.get("/api/v1/qa-superuser/dashboard").status_code == 401

    def test_the_beat_endpoint_requires_a_key(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.post("/api/v1/admin/qa-superuser/beat", json={"a": 1})
        assert r.status_code == 401

    def test_a_valid_key_renders_the_page(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.get("/api/v1/qa-superuser/dashboard?admin_key=secret")
        assert r.status_code == 200
        assert b"QA super-user board" in r.data

    def test_the_page_states_it_is_not_the_source_of_truth(self, monkeypatch):
        # This is a correctness property, not copy. A board hosted inside the
        # thing it watches must say so, or its silence during an outage reads as
        # good news.
        client, _ = self._client(monkeypatch)
        body = client.get("/api/v1/qa-superuser/dashboard?admin_key=secret").data
        assert b"not the source of truth" in body
        assert b"unreachable is itself a signal" in body
        assert b"issues/2186" in body

    def test_live_data_is_never_edge_cached(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.get("/api/v1/qa-superuser/dashboard?admin_key=secret")
        assert r.headers.get("Cache-Control") == "no-store"

    def test_a_replayed_beat_updates_instead_of_duplicating(self):
        # A run is identified by when it was generated, so a retried beat — a
        # workflow re-run, a network retry, an operator re-dispatch — must
        # replace that run, not append a second copy. Duplicates would grow
        # phantom bars in the trend and overstate how many runs have happened,
        # the same quiet inflation this whole tool exists to catch.
        from routes import qa_superuser_dashboard as mod
        import inspect
        src = inspect.getsource(mod.qa_superuser_beat)
        assert "ON CONFLICT (generated_at) DO UPDATE" in src
        ensure = inspect.getsource(mod._ensure)
        assert "CREATE UNIQUE INDEX" in ensure and "generated_at" in ensure, \
            "ON CONFLICT needs a unique index on the conflict target or it errors"

    def test_the_beat_rejects_a_payload_missing_its_verdicts(self, monkeypatch):
        client, _ = self._client(monkeypatch)
        r = client.post("/api/v1/admin/qa-superuser/beat?admin_key=secret",
                        json={"generated_at": "2026-08-04T00:00:00+00:00"})
        assert r.status_code == 400


class TestBoardActions:
    """The buttons route a finding into an existing human-gated lane.

    None of them fix anything. The line that has held since the autonomy core
    was written — propose, never execute — is not moved by adding buttons, and
    these tests are what keep that true as the page grows.
    """

    def _mod(self, monkeypatch):
        from routes import qa_superuser_dashboard as mod
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "secret")
        return mod

    def _client(self, monkeypatch):
        import flask
        mod = self._mod(monkeypatch)
        app = flask.Flask(__name__)
        app.register_blueprint(mod.qa_superuser_dashboard_bp)
        return app.test_client(), mod

    # -- the brain question -------------------------------------------------
    def test_the_question_carries_its_own_evidence(self, monkeypatch):
        mod = self._mod(monkeypatch)
        q = mod.derive_question({
            "title": "Quota meter VANISHES between calls", "seat": "anon",
            "surface": "mcp",
            "evidence": "call 1 published remaining_full_today=0; call 2 none"})
        assert "remaining_full_today=0" in q, \
            "the investigator refuted 70% of its own drafts for citing evidence " \
            "it was never given — the question must carry it"
        assert "anon" in q and "mcp" in q

    def test_the_question_stays_short_enough_to_complete(self, monkeypatch):
        # A long derived question timed out the REASON step outright; a
        # 182-char one completed. Length is functional here.
        mod = self._mod(monkeypatch)
        q = mod.derive_question({"title": "T" * 400, "seat": "paid",
                                 "surface": "mcp", "evidence": "E" * 900})
        assert len(q) <= 320

    # -- acknowledgements ---------------------------------------------------
    def test_an_ack_is_bound_to_the_evidence_it_was_given_for(self, monkeypatch):
        mod = self._mod(monkeypatch)
        a = mod.evidence_sha("2 future-dated records")
        b = mod.evidence_sha("47 future-dated records")
        assert a != b, \
            "if the hash ignored the evidence, one ack would mute every future, " \
            "worse version of the same finding"

    def test_a_changed_evidence_makes_the_ack_stale_not_absent(self, monkeypatch):
        mod = self._mod(monkeypatch)
        monkeypatch.setattr(mod, "_ensure_acks", lambda cur: None)

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self):
                return [("K", mod.evidence_sha("OLD"), "looking into it",
                         datetime.datetime(2026, 8, 1,
                                           tzinfo=datetime.timezone.utc))]

        class Conn:
            def cursor(self): return Cur()
            def close(self): pass

        monkeypatch.setattr(mod, "_conn", lambda: Conn())
        latest = {"findings": [{"key": "K", "evidence": "NEW"}]}
        mod._attach_acks(latest)
        assert latest["findings"][0]["ack"]["state"] == "stale", \
            "'you acknowledged this, but what it says has changed' is a " \
            "different and more useful message than either state alone"

    def test_an_unchanged_evidence_keeps_the_ack_current(self, monkeypatch):
        mod = self._mod(monkeypatch)
        monkeypatch.setattr(mod, "_ensure_acks", lambda cur: None)

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self):
                return [("K", mod.evidence_sha("SAME"), "", None)]

        class Conn:
            def cursor(self): return Cur()
            def close(self): pass

        monkeypatch.setattr(mod, "_conn", lambda: Conn())
        latest = {"findings": [{"key": "K", "evidence": "SAME"}]}
        mod._attach_acks(latest)
        assert latest["findings"][0]["ack"]["state"] == "current"

    # -- auth ---------------------------------------------------------------
    @pytest.mark.parametrize("path", ["/api/v1/admin/qa-superuser/ack",
                                      "/api/v1/admin/qa-superuser/investigate"])
    def test_every_action_requires_a_key(self, monkeypatch, path):
        client, _ = self._client(monkeypatch)
        assert client.post(path, json={"key": "K"}).status_code == 401

    def test_investigate_refuses_a_key_not_in_the_latest_run(self, monkeypatch):
        client, mod = self._client(monkeypatch)
        monkeypatch.setattr(mod, "_load", lambda limit=1: {"latest": {"findings": []}})
        r = client.post("/api/v1/admin/qa-superuser/investigate?admin_key=secret",
                        json={"key": "nope"})
        assert r.status_code == 404

    def test_investigate_says_dispatched_not_finished(self, monkeypatch):
        # ★ A fast 200 means DISPATCHED. If the response implied a result, the
        # next reader would take a green button for an answer.
        client, mod = self._client(monkeypatch)
        monkeypatch.setattr(mod, "_load", lambda limit=1: {
            "latest": {"findings": [{"key": "K", "title": "t", "seat": "anon",
                                     "surface": "mcp", "evidence": "e"}]}})
        started = []
        import threading
        # ★ Accepts `args` too. The double used to take only (target, daemon),
        #   so the moment the handler passed args= it raised inside the request
        #   and the endpoint 500'd — a test double agreeing with an OLD
        #   signature, which is the same failure that hid the registries crash.
        monkeypatch.setattr(threading, "Thread",
                            lambda target=None, args=(), daemon=None: type(
                                "T", (), {"start": lambda s: started.append(1)})())
        r = client.post("/api/v1/admin/qa-superuser/investigate?admin_key=secret",
                        json={"key": "K"})
        body = r.get_json()
        assert r.status_code == 200 and body["dispatched"] is True
        assert "not finished" in body["note"]
        assert started, "the LLM call must be threaded, never awaited in-request"

    # -- the page -----------------------------------------------------------
    def test_actions_render_only_on_red_or_an_instrument_fault(self, monkeypatch):
        """Two things must hold at once, and they pull in opposite directions.

        A gauge makes no claim to act on and an unobserved PLATFORM surface is
        a request to look again — neither is a defect to route, and offering
        the brain either one manufactures a defect (rule 1).

        But an instrument fault is OUR bug, visible only here. RED-only is why
        `registries` crashed on every run for two days with no card offering a
        single action. So the gate widened by exactly one term, and this test
        pins both halves: faults in, everything else still out.
        """
        client, _ = self._client(monkeypatch)
        page = client.get(
            "/api/v1/qa-superuser/dashboard?admin_key=secret").data.decode()
        assert "f.verdict === 'RED' || !!f.instrument_fault" in page, \
            "an instrument fault must be routable — it is our bug and this " \
            "board is the only place it appears"
        assert "const acts = !actionable ? '' :" in page, \
            "a gauge and an unobserved platform surface must still get no " \
            "actions — widening past instrument faults would invent defects"

    def test_the_issue_link_is_client_side_with_no_token(self, monkeypatch):
        # The backend holds no GH_TOKEN by design; adding one to open issues
        # would be a new secret for a job the browser does for free, and the
        # human pressing Submit on GitHub keeps the gate by construction.
        client, _ = self._client(monkeypatch)
        page = client.get(
            "/api/v1/qa-superuser/dashboard?admin_key=secret").data.decode()
        assert "issues/new" in page and "encodeURIComponent" in page


class TestEdgeStaleness:
    """The check that closes the hole the status-code comparison left.

    On 2026-08-04 the edge served a 13,886-byte page while the origin served
    19,243 bytes of the SAME URL. Both returned 200, so the edge/origin check
    called it PASS while a public page was 40 minutes stale.

    The fix asserts on a SELF-CONTRADICTION — the response's own Cache-Control
    says do not store this, and the same response came back from the edge as a
    stored copy with a non-zero age. No body comparison, so no false alarm on
    timestamps; no threshold, so nothing invented.
    """

    def _obs(self, cache_control="", cf="MISS", age="0", cdn="",
             e_len=1000, o_len=1000, path="/x"):
        headers = {"Cache-Control": cache_control, "cf-cache-status": cf,
                   "age": age}
        if cdn:
            headers["CDN-Cache-Control"] = cdn
        return [(path, headers, e_len, o_len)]

    def _run(self, obs):
        out = []
        probe_web._check_stale_edge_cache(obs, out)
        return {f.key.split("::")[1].split("#")[0]: f for f in out}

    def test_a_private_response_served_from_cache_is_red(self):
        # The exact shape measured on /api/v1/health: age 2296 against
        # "private, max-age=0, must-revalidate".
        got = self._run(self._obs("private, max-age=0, must-revalidate",
                                  cf="HIT", age="2296"))
        f = got["edge-honours-no-store"]
        assert f.verdict == RED
        assert "2296" in f.evidence and "38 min" in f.evidence

    def test_a_no_store_response_served_from_cache_is_red(self):
        got = self._run(self._obs("no-store, no-cache, must-revalidate",
                                  cf="HIT", age="957", e_len=13886, o_len=19243))
        f = got["edge-honours-no-store"]
        assert f.verdict == RED
        assert "957" in f.evidence, "the observed age must reach the operator"
        assert "body differs from origin" in f.evidence, \
            "the byte delta is the operator's confirmation it is really stale"

    def test_cdn_cache_control_alone_still_counts(self):
        # The board set BOTH; CF ignored both. Either one must trigger.
        got = self._run(self._obs("", cf="HIT", age="600", cdn="no-store"))
        assert got["edge-honours-no-store"].verdict == RED

    def test_a_legitimately_cacheable_response_is_not_flagged(self):
        got = self._run(self._obs("public, max-age=120", cf="HIT", age="60"))
        assert got["edge-honours-no-store"].verdict == PASS, \
            "caching something cacheable is the CDN doing its job"

    def test_no_store_that_was_not_stored_is_fine(self):
        got = self._run(self._obs("no-store", cf="DYNAMIC", age="0"))
        assert got["edge-honours-no-store"].verdict == PASS

    def test_a_stored_but_zero_age_copy_is_not_yet_evidence(self):
        # age is what proves a stale copy was served; without it there is no
        # observed staleness to report.
        got = self._run(self._obs("no-store", cf="HIT", age="0"))
        assert got["edge-honours-no-store"].verdict == PASS

    def test_headers_are_read_case_insensitively(self):
        # ★ CF sends lowercase, Flask sends title-case. Reading only one casing
        # would make this check silently never fire — a guard that cannot
        # trigger, which is the exact failure it exists to catch.
        obs = [("/x", {"cache-control": "no-store", "CF-Cache-Status": "HIT",
                       "Age": "500"}, 10, 20)]
        out = []
        probe_web._check_stale_edge_cache(obs, out)
        assert any(f.verdict == RED for f in out)

    def test_divergence_is_a_gauge_and_never_votes(self):
        got = self._run(self._obs("public, max-age=120", cf="HIT", age="60",
                                  e_len=100, o_len=900))
        g = got["edge-origin-divergence"]
        assert g.verdict == GAUGE
        assert g.counts_as_failure is False, \
            "timestamps and counters move between two requests; calling that a " \
            "defect needs a threshold nobody has defined"

    def test_divergence_is_not_reported_for_uncached_paths(self):
        got = self._run(self._obs("no-store", cf="DYNAMIC", age="0",
                                  e_len=100, o_len=900))
        assert "edge-origin-divergence" not in got, \
            "two live responses differing is not evidence of anything"


class TestBoardIdentity:
    """The rolling board is identified by a marker, never by position.

    ★ THE BUG THIS PINS, observed live. `upsert_issue` selected the board with
    `gh issue list --label qa-superuser --json number --jq '.[0].number'`, which
    is correct while exactly one such issue exists. Then the dashboard's "Open
    an issue" button began minting per-finding issues with the SAME label — gh
    returns newest-first — so the next run rewrote the operator's newest issue
    with the entire board. #2205 "EDGE is caching 1 path(s) that declare
    no-store" became "3 red across the caller-facing surfaces", title and body
    destroyed.
    """

    def _gh_returning(self, monkeypatch, issues):
        monkeypatch.setattr(
            board, "_gh",
            lambda a, input_text=None: (0, json.dumps(issues))
            if a[:2] == ["issue", "list"] else (0, ""))

    def test_the_marker_wins_over_a_newer_labelled_issue(self, monkeypatch):
        self._gh_returning(monkeypatch, [
            {"number": 2205, "title": "[qa-superuser] EDGE is caching",
             "body": "a finding\nfinding key web::x#1"},
            {"number": 2186, "title": "[qa-superuser] 3 red across the "
                                      "caller-facing surfaces",
             "body": board.BOARD_MARKER + "\n## board"},
        ])
        assert board._find_board_issue() == "2186", \
            "newest-first ordering must not decide which issue is the board"

    def test_a_per_finding_issue_is_never_adopted_as_the_board(self, monkeypatch):
        # No marker anywhere (pre-marker state) and only per-finding issues
        # present: adopt nothing rather than hijack one.
        self._gh_returning(monkeypatch, [
            {"number": 2205, "title": "[qa-superuser] EDGE is caching",
             "body": "x\nfinding key web::edge#1"},
            {"number": 2203, "title": "[qa-superuser] get_news future-dated",
             "body": "x\nfinding key data::news#2"},
        ])
        assert board._find_board_issue() is None

    def test_a_pre_marker_board_is_adopted_not_duplicated(self, monkeypatch):
        # The board already running when this fix deploys has no marker yet.
        self._gh_returning(monkeypatch, [
            {"number": 2205, "title": "[qa-superuser] EDGE is caching",
             "body": "x\nfinding key web::edge#1"},
            {"number": 2186, "title": "[qa-superuser] 3 red across the "
                                      "caller-facing surfaces",
             "body": "## DC Hub QA super-user — outside-in board"},
        ])
        assert board._find_board_issue() == "2186"

    def test_the_oldest_wins_when_several_could_be_the_board(self, monkeypatch):
        self._gh_returning(monkeypatch, [
            {"number": 9999, "title": "[qa-superuser] 5 red across the "
                                      "caller-facing surfaces", "body": "b"},
            {"number": 2186, "title": "[qa-superuser] 3 red across the "
                                      "caller-facing surfaces", "body": "b"},
        ])
        assert board._find_board_issue() == "2186"

    def test_the_published_board_carries_the_marker(self, monkeypatch):
        sent = {}

        def fake_gh(args, input_text=None):
            if args[:2] == ["issue", "list"]:
                return 0, "[]"
            if args[:2] == ["issue", "create"]:
                sent["body"] = input_text
            return 0, ""

        monkeypatch.setattr(board, "_gh", fake_gh)
        run = {"generated_at": "x", "counts": {"red": 0}, "findings": []}
        board.upsert_issue("## board body", {}, run, {})
        assert board.BOARD_MARKER in sent["body"], \
            "without the marker the next run cannot find its own board"

    def test_an_unreadable_issue_list_adopts_nothing(self, monkeypatch):
        monkeypatch.setattr(board, "_gh", lambda a, input_text=None: (1, "boom"))
        assert board._find_board_issue() is None


class TestIssueDedup:
    """One finding, one issue. Clicking the button twice must not mint a second.

    ★ Observed live: #2203 and #2209 were opened for the SAME finding, and
    #2204 and #2210 for another. A duplicate-issue backlog is the same disease
    as an unclosed one — the board stops being a list of real work.
    """

    def _gh_returning(self, monkeypatch, issues):
        monkeypatch.setattr(
            board, "_gh",
            lambda a, input_text=None: (0, json.dumps(issues))
            if a[:2] == ["issue", "list"] else (0, ""))

    def test_an_existing_issue_is_found_by_its_key(self, monkeypatch):
        self._gh_returning(monkeypatch, [
            {"number": 2203, "body": f"x\n{board.ISSUE_KEY_MARKER}data::news#1"},
        ])
        assert board.open_issue_numbers() == {"data::news#1": 2203}

    def test_the_oldest_duplicate_wins(self, monkeypatch):
        # Point at the original — the one carrying the discussion — so closing
        # the copies does not move the link.
        self._gh_returning(monkeypatch, [
            {"number": 2209, "body": f"x\n{board.ISSUE_KEY_MARKER}data::news#1"},
            {"number": 2203, "body": f"x\n{board.ISSUE_KEY_MARKER}data::news#1"},
        ])
        assert board.open_issue_numbers()["data::news#1"] == 2203

    def test_the_board_itself_is_not_indexed(self, monkeypatch):
        self._gh_returning(monkeypatch, [
            {"number": 2186, "body": board.BOARD_MARKER + "\n## board"},
        ])
        assert board.open_issue_numbers() == {}

    def test_a_read_failure_offers_a_new_issue_rather_than_hiding_the_button(
            self, monkeypatch):
        # Failing open is the safe direction here: offering to create one that
        # already exists is a duplicate; suppressing the button would remove the
        # only way to file at all.
        monkeypatch.setattr(board, "_gh", lambda a, input_text=None: (1, "boom"))
        assert board.open_issue_numbers() == {}

    def test_the_beat_carries_the_issue_number(self, monkeypatch):
        import requests
        seen = {}
        monkeypatch.setattr(board.C, "ADMIN_KEY", "k")
        monkeypatch.setattr(
            board, "open_issue_numbers", lambda: {"K": 2203})

        class R:
            status_code = 200
            text = "{}"

        monkeypatch.setattr(requests, "post",
                            lambda url, **kw: (seen.update(kw=kw), R())[1])
        f = _f(key="K", verdict=RED, severity=MAJOR).to_dict()
        run = {"generated_at": "x", "canary_fired": True, "edge": "e",
               "counts": summarize([Finding.from_dict(f)]), "findings": [f]}
        board.beat_dashboard(run, {"findings": {}})
        sent = json.loads(seen["kw"]["data"])
        assert sent["findings"][0]["issue_number"] == 2203, \
            "the page cannot dedup what the beat does not tell it"

    def test_the_page_links_the_existing_issue_instead_of_creating(self,
                                                                   monkeypatch):
        import flask
        from routes import qa_superuser_dashboard as mod
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "secret")
        app = flask.Flask(__name__)
        app.register_blueprint(mod.qa_superuser_dashboard_bp)
        page = app.test_client().get(
            "/api/v1/qa-superuser/dashboard?admin_key=secret").data.decode()
        assert "f.issue_number" in page
        assert "Issue #${f.issue_number}" in page


class TestIssueClosure:
    """The board closes the issues it opened — but only on an OBSERVED pass.

    A board that opens issues nobody closes becomes the backlog nobody works,
    which is the failure this whole tool was built in response to. But closing
    on anything weaker than "I asked again and the answer was right" would be
    the muted-alarm failure in a new costume, so these tests pin exactly which
    delta classes may close an issue.
    """

    def _issue(self, key, number=2203):
        return {"number": number, "title": "t",
                "body": f"stuff\nBoard: ... {board.ISSUE_KEY_MARKER}{key}"}

    def _harness(self, monkeypatch, key, issues):
        calls = []

        def fake_gh(args, input_text=None):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(issues)
            return 0, ""

        monkeypatch.setattr(board, "_gh", fake_gh)
        monkeypatch.delenv("QA_SUPERUSER_NO_CLOSE", raising=False)
        f = _f(key=key, verdict=PASS).to_dict()
        run = {"generated_at": "2026-08-04T05:00:00+00:00", "canary_fired": True,
               "edge": "e", "counts": summarize([Finding.from_dict(f)]),
               "findings": [f]}
        return run, calls

    def test_a_recovered_finding_closes_its_issue(self, monkeypatch):
        run, calls = self._harness(monkeypatch, "K", [self._issue("K")])
        closed = board.close_resolved_issues(run, {"K": board.RECOVERED})
        assert closed and "#2203" in closed[0]
        assert any(a[:2] == ["issue", "close"] for a in calls)

    def test_the_closing_comment_carries_the_proof(self, monkeypatch):
        bodies = []

        def fake_gh(args, input_text=None):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps([self._issue("K")])
            if args[:2] == ["issue", "comment"]:
                bodies.append(input_text)
            return 0, ""

        monkeypatch.setattr(board, "_gh", fake_gh)
        monkeypatch.delenv("QA_SUPERUSER_NO_CLOSE", raising=False)
        f = _f(key="K", verdict=PASS, evidence="82 tools, all present").to_dict()
        run = {"generated_at": "2026-08-04T05:00:00+00:00", "canary_fired": True,
               "edge": "e", "counts": summarize([Finding.from_dict(f)]),
               "findings": [f]}
        board.close_resolved_issues(run, {"K": board.RECOVERED})
        assert bodies and "82 tools, all present" in bodies[0]
        assert "not an absence of evidence" in bodies[0]

    def _harness_verdict(self, monkeypatch, verdict, severity=INFO):
        """A run whose single finding K carries the given verdict."""
        calls = []

        def fake_gh(args, input_text=None):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps([self._issue("K")])
            return 0, ""

        monkeypatch.setattr(board, "_gh", fake_gh)
        monkeypatch.delenv("QA_SUPERUSER_NO_CLOSE", raising=False)
        if verdict == BLIND:
            f = blind(key="K", surface="mcp", seat=SEAT_ANON, title="t",
                      why="w", basis="b").to_dict()
        else:
            f = _f(key="K", verdict=verdict, severity=severity).to_dict()
        run = {"generated_at": "2026-08-04T05:00:00+00:00", "canary_fired": True,
               "edge": "e", "counts": summarize([Finding.from_dict(f)]),
               "findings": [f]}
        return run, calls

    # ★ THE SAFETY PROPERTY NOW LIVES ON THE VERDICT, NOT THE DELTA — which is
    # strictly stronger. Closing keys on an explicit `verdict == PASS`, so
    # "the probe stopped looking" still cannot close a defect, and the rule no
    # longer depends on catching a transition at the right moment.
    @pytest.mark.parametrize("verdict,severity", [
        (BLIND, INFO),      # could not look
        (GAUGE, INFO),      # makes no pass/fail claim
        (RED, MAJOR),       # observed failing
        (RED, MINOR),       # observed failing, cosmetic — still not a pass
    ])
    def test_only_an_observed_pass_can_close_an_issue(self, monkeypatch,
                                                      verdict, severity):
        run, calls = self._harness_verdict(monkeypatch, verdict, severity)
        assert board.close_resolved_issues(run, {}) == []
        assert not any(a[:2] == ["issue", "close"] for a in calls)

    @pytest.mark.parametrize("delta", ["WENT_BLIND", "DISAPPEARED", "STILL",
                                       "NEW", "REGRESSED", "FLAPPING",
                                       "UNCHANGED"])
    def test_a_passing_finding_closes_regardless_of_delta(self, monkeypatch, delta):
        # ★ THE ORPHAN FIX. Closing used to require the RECOVERED transition, so
        # an issue a human filed WHILE red — for a finding that had already
        # flipped green in an earlier run — never saw another RECOVERED event
        # and stayed open forever. Observed with #2228. Keying on current state
        # makes every run re-check every open issue, so filing time no longer
        # matters.
        run, calls = self._harness_verdict(monkeypatch, PASS)
        closed = board.close_resolved_issues(run, {"K": getattr(board, delta)})
        assert closed, f"a currently-passing finding must close its issue ({delta})"
        assert any(a[:2] == ["issue", "close"] for a in calls)

    def test_it_is_idempotent_across_runs(self, monkeypatch):
        # Run twice against the same open issue: the second run simply finds
        # nothing left to close, rather than erroring or double-commenting.
        run, calls = self._harness_verdict(monkeypatch, PASS)
        first = board.close_resolved_issues(run, {})
        assert first
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (0, "[]")
                            if a[:2] == ["issue", "list"] else (0, ""))
        assert board.close_resolved_issues(run, {}) == []

    def test_an_issue_without_the_marker_is_never_touched(self, monkeypatch):
        # The rolling board issue carries no finding key. Closing IT would take
        # the whole surface down, so the marker is required, not assumed.
        run, calls = self._harness(
            monkeypatch, "K",
            [{"number": 2186, "title": "board", "body": "no key here"}])
        assert board.close_resolved_issues(run, {"K": board.RECOVERED}) == []
        assert not any(a[:2] == ["issue", "close"] for a in calls)

    def test_a_different_findings_issue_is_not_closed(self, monkeypatch):
        run, calls = self._harness(monkeypatch, "K", [self._issue("OTHER")])
        assert board.close_resolved_issues(run, {"K": board.RECOVERED}) == []

    def test_the_kill_switch_stops_it(self, monkeypatch):
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (0, "[]"))
        monkeypatch.setenv("QA_SUPERUSER_NO_CLOSE", "1")
        assert board.close_resolved_issues(
            {"findings": [], "generated_at": "x"}, {"K": board.RECOVERED}) == []

    def test_an_unlistable_api_closes_nothing(self, monkeypatch):
        monkeypatch.setattr(board, "_gh",
                            lambda a, input_text=None: (1, "API down"))
        monkeypatch.delenv("QA_SUPERUSER_NO_CLOSE", raising=False)
        f = _f(key="K", verdict=PASS).to_dict()
        run = {"generated_at": "x", "canary_fired": True, "edge": "e",
               "counts": summarize([Finding.from_dict(f)]), "findings": [f]}
        assert board.close_resolved_issues(run, {"K": board.RECOVERED}) == []


class TestStableKeys:
    def test_same_inputs_give_the_same_key(self):
        assert stable_key("mcp", "anon", "x") == stable_key("mcp", "anon", "x")

    def test_seat_is_part_of_identity(self):
        # "gated for anon" and "gated for a paying key" are different facts.
        assert stable_key("mcp", "anon", "x") != stable_key("mcp", "paid", "x")

    def test_long_keys_do_not_collide_after_truncation(self):
        a = stable_key("mcp", "anon", "x" * 200 + "A")
        b = stable_key("mcp", "anon", "x" * 200 + "B")
        assert a != b


class TestAnonSeatBudgetIsMeasuredNotAssumed:
    """The anon budget is keyed on (ip, TOOL, day) — never on the session.

    This class exists because the probe assumed the opposite for four days
    (2026-08-04 → 08-07). It opened a fresh MCPSession before each
    budget-dependent check and asserted, in its own `basis` string, that this
    produced "a caller WITH trial budget". It does not. The consequences were
    exactly the two failure modes this harness was built to catch:

      * the quota-movement check filed a SPENT meter as a GAUGE — a reassuring
        number parked where a measurement should have been — and went
        unobserved for ~24 consecutive runs without ever saying so; and
      * the paid-vs-anon check compared a paying key against an EXHAUSTED
        anonymous one and passed. Measured live on 2026-08-07 its only
        paid-only field was `citation`, which a control with budget also
        receives — a green that could not have gone red.

    Ground truth, from mcp-server server.mjs:
        _trialDayCounts.get(`${ipKey}:${tool}:${day}`)
    """

    def _env(self, remaining, extra=None):
        sc = {"market": "ashburn", "stats": {"n": 1},
              "quota": {"tier": "free", "full_answers_remaining_today": remaining}}
        sc.update(extra or {})
        return {"structuredContent": sc}

    # ── the meter check ────────────────────────────────────────────────────
    def test_a_spent_meter_is_BLIND_not_a_gauge(self):
        """Nothing about MOVEMENT was observed, so nothing may be claimed.

        GAUGE means "observed, reported as a number, no pass/fail claim". A
        meter pinned at its floor was not observed at all — that is BLIND, and
        BLIND is what makes the board render `unobserved` instead of quietly
        carrying a number that reads like a finding.
        """
        from tools.qa_superuser import probe_mcp
        out = []
        sess = _StubSession(self._env(0))
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: sess})()
        try:
            probe_mcp._check_quota_moves(out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        meter = [f for f in out if "quota-meter" in f.key]
        assert len(meter) == 1
        assert meter[0].verdict == BLIND, (
            f"a meter already at zero proves nothing about movement; got "
            f"{meter[0].verdict} — {meter[0].title}")
        assert not meter[0].counts_as_failure

    def test_a_meter_with_room_that_does_not_move_is_still_RED(self):
        """The rotation must not smother the defect it exists to expose."""
        from tools.qa_superuser import probe_mcp
        out = []
        envs = [self._env(2), self._env(2)]

        class _S:
            def open(self):
                return self

            def call(self, _n, _a):
                return envs.pop(0)

        probe_mcp.MCPSession = lambda *a, **k: _S()
        try:
            probe_mcp._check_quota_moves(out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        meter = [f for f in out if "quota-meter" in f.key][0]
        assert meter.verdict == RED and meter.counts_as_failure

    # ── the rotation that restores observability ───────────────────────────
    def test_rotation_never_picks_the_tool_the_other_checks_exhaust(self):
        """get_market_intel is FLAGSHIP_TOOL and a TIER_PROBE_CALL.

        Both of its 2 daily calls are spent before the meter check runs, so
        picking it would guarantee the blindness the rotation exists to end.
        """
        from tools.qa_superuser import config as C
        assert C.FLAGSHIP_TOOL not in [t for t, _, _ in C.METERED_TOOLS]

    def test_each_run_of_a_day_gets_a_different_tool(self):
        """The cap is per-tool, so a rotating tool is a fresh budget."""
        import datetime as dt
        from tools.qa_superuser import config as C
        from tools.qa_superuser import probe_mcp
        picked = []
        for hour in range(0, 24, 4):
            slot = (dt.datetime(2026, 8, 7, hour, tzinfo=dt.timezone.utc)
                    .timetuple().tm_yday * 6 + hour // 4)
            picked.append(C.METERED_TOOLS[slot % len(C.METERED_TOOLS)][0])
        assert len(set(picked)) >= len(C.METERED_TOOLS), (
            f"a day's runs must cycle the whole pool; got {picked}")
        assert probe_mcp._metered_tool_for_run()[0] in [
            t for t, _, _ in C.METERED_TOOLS]

    def test_the_two_calls_use_different_arguments(self):
        """An unchanged meter must not be explicable as a cached response."""
        from tools.qa_superuser import config as C
        for tool, a, b in C.METERED_TOOLS:
            assert a != b, f"{tool} would compare a response against itself"

    # ── the paid-vs-anon comparison ────────────────────────────────────────
    def test_paid_beats_anon_is_BLIND_when_the_control_was_spent(self):
        """Paid vs a post-cap caller measures the cap, not the paywall."""
        from tools.qa_superuser import probe_mcp
        out = []
        paid_env = {"structuredContent": {"market": "ashburn", "stats": {},
                                          "citation": {}, "by_status": {}}}
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: _StubSession(self._env(0))})()
        try:
            probe_mcp._check_paid_beats_anon(paid_env, out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        assert len(out) == 1
        assert out[0].verdict == BLIND, (
            f"an exhausted control cannot support 'a paying key buys more "
            f"data'; got {out[0].verdict} — {out[0].title}")

    def test_paid_beats_anon_still_asserts_when_the_control_had_budget(self):
        """The BLIND guard must not disable the check on a healthy control."""
        from tools.qa_superuser import probe_mcp
        out = []
        paid_env = {"structuredContent": {"market": "a", "stats": {},
                                          "citation": {}, "by_status": {}}}
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: _StubSession(self._env(1))})()
        try:
            probe_mcp._check_paid_beats_anon(paid_env, out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        assert len(out) == 1
        assert out[0].verdict != BLIND
        assert "budget left" in out[0].evidence

    # ── the gauge that described nobody ────────────────────────────────────
    def test_envelope_ratio_labels_the_population_it_measured(self):
        """One label covering two populations is how '100% envelope' shipped.

        Exercised through the real helper rather than grepping the source —
        a comment satisfies grep (#37), only a call collects on the behaviour.
        """
        from tools.qa_superuser.probe_mcp import _budget_population
        assert _budget_population(self._env(0))[1] == "post-cap"
        assert _budget_population(self._env(2))[1] == "with-budget"
        assert _budget_population({"structuredContent": {}})[1] == "budget-unstated"

    def test_a_spent_caller_is_never_labelled_as_one_with_budget(self):
        """The two populations must not be collapsible by an falsy-zero bug."""
        from tools.qa_superuser.probe_mcp import _budget_population
        left, label = _budget_population(self._env(0))
        assert left == 0 and label != "with-budget"

    # ── the fourth population: budget held, answer withheld ────────────────
    def _mint(self, remaining=2):
        """A real first-touch mint envelope, keys as observed live 2026-08-07.

        The platform spends its first anonymous response introducing itself:
        trial key + upsell, zero data. Budget is untouched.
        """
        return {"structuredContent": {
            "auto_trial_key": "dch_trial_xxx", "first_call_nudge": {},
            "for_your_human": {}, "inline_full": {}, "persist_command": "x",
            "trial_preview": {}, "preview_is_partial": True, "upgrade": {},
            "starter_pack": {}, "platform": "x", "success": True,
            "quota": {"tier": "free", "full_answers_remaining_today": remaining},
        }}

    def test_a_first_touch_response_gets_its_own_label(self):
        """Budget and depth are nearly orthogonal — measured live.

            fresh CI IP, budget remaining -> 0 data fields (mint)
            IP with cap fully spent       -> 6 data fields (post-cap)

        #2343 inferred "has budget" => "was served data" and put
        "(with-budget): 100% of fields are envelope - 0 data field(s)" on the
        board. Self-contradicting, and the label was the wrong half.
        """
        from tools.qa_superuser.probe_mcp import _budget_population
        left, label = _budget_population(self._mint(remaining=2))
        assert label == "first-touch", f"got {label}"
        assert left == 2, "the meter reading is still reported, not discarded"

    def test_the_mint_marker_outranks_the_meter(self):
        """How the caller was SERVED describes it better than what it had."""
        from tools.qa_superuser.probe_mcp import _budget_population
        assert _budget_population(self._mint(remaining=0))[1] == "first-touch"

    def test_a_served_answer_is_never_called_a_mint(self):
        """The guard must not swallow the population it exists to separate."""
        from tools.qa_superuser.probe_mcp import _budget_population
        assert _budget_population(self._env(2))[1] == "with-budget"
        assert _budget_population(self._env(0))[1] == "post-cap"

    def test_paid_beats_anon_is_BLIND_against_an_EMPTY_control(self):
        """Zero data fields means "paid has more" is arithmetic on nothing.

        This is the case the board hit: a fresh CI runner IP whose first
        response carried 16 envelope fields and no data at all.
        """
        from tools.qa_superuser import probe_mcp
        out = []
        paid_env = {"structuredContent": {"market": "a", "stats": {},
                                          "citation": {}, "by_status": {}}}
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: _StubSession(self._mint(remaining=2))})()
        try:
            probe_mcp._check_paid_beats_anon(paid_env, out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        assert len(out) == 1 and out[0].verdict == BLIND, (
            f"an empty control cannot support 'a paying key buys more data'; "
            f"got {out[0].verdict}")
        assert "empty" in out[0].title

    def test_a_control_that_is_BOTH_spent_and_minting_is_still_rejected(self):
        """Guard on the value, not the label — caught by a live run, not a test.

        The mint markers outrank the meter in the LABEL, so a caller that was
        both spent and minting reads `first-touch`. A guard written against the
        label sailed straight past the post-cap case and used an invalid
        control. Observed live 2026-08-07: population='first-touch',
        remaining=0.
        """
        from tools.qa_superuser import probe_mcp
        out = []
        anon = self._mint(remaining=0)                      # spent AND minting
        anon["structuredContent"].update({"stats": {}, "market": "a"})
        paid_env = {"structuredContent": {"market": "a", "stats": {},
                                          "citation": {}, "by_status": {}}}
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: _StubSession(anon)})()
        try:
            probe_mcp._check_paid_beats_anon(paid_env, out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        assert probe_mcp._budget_population(anon)[1] == "first-touch"
        assert out[0].verdict == BLIND and "spent" in out[0].title, (
            f"a spent control must be rejected whatever its label says; "
            f"got {out[0].verdict} — {out[0].title}")

    def test_a_first_touch_control_WITH_data_is_still_a_valid_control(self):
        """Rejecting every mint would shrink coverage to fix nothing.

        Measured live 2026-08-07 on a spent IP: the first-touch response
        carried auto_trial_key AND 6 data fields. The mint block is not
        evidence that the answer was withheld.
        """
        from tools.qa_superuser import probe_mcp
        out = []
        anon = self._mint(remaining=2)
        anon["structuredContent"].update(
            {"stats": {}, "market": "a", "by_status": {}})   # served real data
        paid_env = {"structuredContent": {"market": "a", "stats": {},
                                          "citation": {}, "by_status": {}}}
        probe_mcp.MCPSession = lambda *a, **k: type(
            "S", (), {"open": lambda s: _StubSession(anon)})()
        try:
            probe_mcp._check_paid_beats_anon(paid_env, out)
        finally:
            from tools.qa_superuser.http import MCPSession as _real
            probe_mcp.MCPSession = _real
        assert out[0].verdict != BLIND, (
            "a first-touch control that WAS served data is usable")

    def test_envelope_ratio_key_does_not_carry_the_population(self):
        """Different keys must mean different FACTS, not different days.

        Keying on the budget state would make one finding vanish and another
        appear every run — the exact noise finding.py's stability rule forbids.
        """
        from tools.qa_superuser import config as C
        a = stable_key("mcp", "anon", "envelope-ratio", C.FLAGSHIP_TOOL)
        assert a == stable_key("mcp", "anon", "envelope-ratio", C.FLAGSHIP_TOOL)


class TestEveryRegisteredProbeIsCallableTheWayTheRunnerCallsIt:
    """The test that was missing on 2026-08-08, and cost two days of blindness.

    `probe_registries` shipped as `def probe()` while `run.collect()` calls
    `mod.probe(findings)` for every probe. Three separate safety nets each had
    a hole exactly the shape of this bug:

      * its OWN unit tests called `pr.probe()` — agreeing with the wrong
        signature, so they passed;
      * `collect()` catches every exception and files it as BLIND, which by
        rule 1 is never a failure, so the run stayed green and exit 0; and
      * the workflow's alarm is `if: failure()`, which a swallowed exception
        never triggers.

    So the surface was never measured on any run from the day it merged, and
    nothing anywhere said so. These tests assert the CONTRACT between the
    runner and the probes, which is the only place the mismatch was visible.
    """

    def test_every_probe_accepts_the_shared_findings_list(self):
        import inspect
        from tools.qa_superuser.run import PROBES
        for name, mod in PROBES:
            fn = getattr(mod, "probe", None)
            assert callable(fn), f"probe_{name} has no callable probe()"
            # bind() is the real question — "can the runner call this?" — not
            # a parameter-count heuristic that *args would defeat.
            inspect.signature(fn).bind([])

    def test_the_registry_is_not_empty_and_covers_every_probe_module(self):
        """A probe deleted from PROBES is a surface silently dropped."""
        import pkgutil
        import tools.qa_superuser as pkg
        from tools.qa_superuser.run import PROBES
        registered = {name for name, _ in PROBES}
        assert registered, "PROBES is empty — the harness measures nothing"
        on_disk = {m.name[len("probe_"):]
                   for m in pkgutil.iter_modules(pkg.__path__)
                   if m.name.startswith("probe_")}
        # `registries` vs module `probe_registries` — compare on the module
        # stem the runner names, so a new probe_*.py that nobody wired into
        # PROBES fails here rather than being quietly absent from every board.
        missing = on_disk - registered
        assert not missing, (
            f"probe module(s) {sorted(missing)} exist but are not in "
            "run.PROBES — they will never run, and no board will say so")


class TestASilentProbeCannotHide:
    """A probe that says NOTHING is the quietest way this harness goes blind.

    `registries` failed loudly and still went unnoticed for two days. A probe
    that returns cleanly having appended zero findings leaves no exception, no
    verdict and a board that is one surface narrower while still reading
    "0 red". So contributing nothing is itself a finding.
    """

    def _collect_with(self, monkeypatch, probe_fn):
        from tools.qa_superuser import run as R
        monkeypatch.setattr(R, "run_canary", lambda: (True, "stub"))
        mod = type("M", (), {"probe": staticmethod(probe_fn)})
        monkeypatch.setattr(R, "PROBES", (("stub", mod),))
        findings, fired = R.collect()
        assert fired
        return [f for f in findings if f.surface == "stub"]

    def test_a_probe_that_appends_nothing_is_an_instrument_fault(self, monkeypatch):
        out = self._collect_with(monkeypatch, lambda findings: None)
        assert len(out) == 1
        assert out[0].verdict == BLIND, "still no claim about the platform"
        assert out[0].instrument_fault is True, (
            "a surface that was not measured is OUR bug and must be routable")

    def test_a_crashing_probe_is_an_instrument_fault_not_a_platform_verdict(
            self, monkeypatch):
        def boom(findings):
            raise TypeError("probe() takes 0 positional arguments but 1 was given")
        out = self._collect_with(monkeypatch, boom)
        assert len(out) == 1
        assert out[0].verdict == BLIND
        assert out[0].instrument_fault is True
        assert "TypeError" in out[0].evidence

    def test_an_unreachable_surface_is_NOT_an_instrument_fault(self, monkeypatch):
        """Rule 1 stays intact: a third party being down is not our defect.

        This is the distinction the whole change turns on. If BLIND-because-
        -unreachable also became actionable, the board would route "Glama was
        down" to the brain as a bug to fix — inventing a defect, which is the
        error rule 1 exists to prevent.
        """
        from tools.qa_superuser.http import Unreachable

        def down(findings):
            raise Unreachable("connection reset")
        out = self._collect_with(monkeypatch, down)
        assert len(out) == 1
        assert out[0].verdict == BLIND
        assert out[0].instrument_fault is False

    def test_a_probe_that_speaks_produces_no_fault_finding(self, monkeypatch):
        """Kills the inverse bug: flagging healthy probes as faulty."""
        def ok(findings):
            findings.append(_f(key="real", surface="stub", verdict=PASS))
        out = self._collect_with(monkeypatch, ok)
        assert len(out) == 1
        assert out[0].key == "real"
        assert out[0].instrument_fault is False


class TestInstrumentFaultsNeverBecomeFailures:
    """Rule 1 is not weakened by making faults actionable.

    `instrument_fault` changes WHO a finding is addressed to. It must not
    change the arithmetic — the moment it counts as red, the harness starts
    reporting its own bugs as product defects, which is the mirror image of
    the mistake it was built to stop.
    """

    def test_a_fault_is_not_a_failure_and_not_red(self):
        f = blind(key="k", surface="registries", seat=SEAT_NONE, title="t",
                  why="TypeError", basis="b", instrument_fault=True)
        assert f.verdict == BLIND
        assert f.counts_as_failure is False
        c = summarize([f])
        assert c["red"] == 0 and c["failures"] == 0 and c["critical"] == 0
        assert c["blind"] == 1

    def test_blind_defaults_to_not_a_fault(self):
        """An unmarked BLIND must stay a platform observation, never ours."""
        assert blind(key="k", surface="s", seat=SEAT_NONE, title="t",
                     why="w", basis="b").instrument_fault is False

    def test_the_flag_survives_a_state_round_trip(self):
        """The board stores findings as JSON; a flag lost in the round trip
        would make the fault un-actionable again on the very next run."""
        f = blind(key="k", surface="s", seat=SEAT_NONE, title="t", why="w",
                  basis="b", instrument_fault=True)
        assert Finding.from_dict(json.loads(json.dumps(f.to_dict()))
                                 ).instrument_fault is True

    def test_a_prior_state_written_before_this_field_still_loads(self):
        """Old rows in the state branch have no `instrument_fault` key."""
        d = blind(key="k", surface="s", seat=SEAT_NONE, title="t", why="w",
                  basis="b").to_dict()
        d.pop("instrument_fault")
        assert Finding.from_dict(d).instrument_fault is False


class TestBoardSeparatesFaultsFromUnobserved:
    """Rendered in one list, "our probe crashed" reads as "a site was down"."""

    def _render(self, findings):
        run = {"generated_at": "2026-08-10T00:00:00+00:00",
               "edge": "https://dchub.cloud", "canary_fired": True,
               "counts": summarize(findings),
               "findings": [f.to_dict() for f in findings]}
        return board.render(run, {}, {})

    def test_a_fault_gets_its_own_section_and_the_headline_says_so(self):
        body = self._render([
            blind(key="a", surface="registries", seat=SEAT_NONE,
                  title="registries probe crashed", why="TypeError", basis="b",
                  instrument_fault=True),
            blind(key="b", surface="mcp", seat=SEAT_NONE,
                  title="paid comparison unobserved", why="spent", basis="b"),
        ])
        assert "Instrument faults" in body
        assert "not measured — instrument fault" in body
        # and the genuine unobserved is still filed as unobserved
        assert "### Unobserved (not failures)" in body

    def test_no_fault_section_when_there_is_no_fault(self):
        body = self._render([
            blind(key="b", surface="mcp", seat=SEAT_NONE, title="unobserved",
                  why="spent", basis="b")])
        assert "Instrument faults" not in body
        assert "instrument fault" not in body.lower().split("### unobserved")[0]


class TestAutoInvestigateSelectsOnlyWhatAHumanWouldClick:
    """The selection rule, pure — no DB, no brain, no HTTP.

    This lane exists because `investigate` and `propose-fix` were BUTTONS: a
    critical red waited for someone to open a page, and a crashed probe waited
    forever because its card had no buttons at all. Automating the READ is safe;
    what must not creep in is automating the DIFF.
    """

    def _mod(self, monkeypatch):
        from routes import qa_superuser_dashboard as mod
        return mod

    def _f(self, **kw):
        base = {"key": "k", "verdict": "RED", "severity": "critical",
                "evidence": "e"}
        base.update(kw)
        return base

    def test_an_observed_failure_is_a_candidate(self, monkeypatch):
        mod = self._mod(monkeypatch)
        todo, _ = mod.auto_investigate_candidates([
            self._f(key="a", severity="critical"),
            self._f(key="b", severity="major")])
        assert [f["key"] for f in todo] == ["a", "b"]

    def test_an_instrument_fault_is_a_candidate_though_it_is_not_red(self, monkeypatch):
        """The whole point of #2503, carried one layer further."""
        mod = self._mod(monkeypatch)
        todo, _ = mod.auto_investigate_candidates([
            self._f(key="crash", verdict="BLIND", severity="info",
                    instrument_fault=True)])
        assert [f["key"] for f in todo] == ["crash"]

    def test_gauges_passes_and_unobserved_platform_surfaces_are_never_sent(
            self, monkeypatch):
        """Handing the brain a non-defect asks it to explain something that has
        not been shown to exist — rule 1, one layer up."""
        mod = self._mod(monkeypatch)
        todo, skipped = mod.auto_investigate_candidates([
            self._f(key="g", verdict="GAUGE", severity="info"),
            self._f(key="p", verdict="PASS", severity="info"),
            self._f(key="b", verdict="BLIND", severity="info"),
            self._f(key="minor", verdict="RED", severity="minor"),
        ])
        assert todo == [] and skipped == [], \
            "a non-candidate is not a 'skip' — it was never eligible"

    def test_a_current_investigation_is_not_redone(self, monkeypatch):
        mod = self._mod(monkeypatch)
        todo, skipped = mod.auto_investigate_candidates([
            self._f(key="done", investigation={"state": "current"})])
        assert todo == []
        assert "already has a current investigation" in skipped[0]["why"]

    def test_a_flapper_whose_evidence_moves_every_run_is_not_re_analysed(
            self, monkeypatch):
        """★ The defect this cooldown exists for, found by reading what the
        NEXT run would actually do rather than by a test failing.

        The quota-meter check rotates the tool it spends (the anon cap is keyed
        on (ip, tool, day)), so its evidence names a different tool every run →
        new evidence_sha → investigation `stale` → eligible again. It sits
        FIRST in board order and had already crossed the pass/fail line 9x, so
        it would eat one of three slots every 4h forever while a genuinely new
        red waited for the next run.
        """
        mod = self._mod(monkeypatch)
        recent = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(hours=4)).isoformat()
        todo, skipped = mod.auto_investigate_candidates([
            self._f(key="flapper", investigation={"state": "stale",
                                                  "at": recent})])
        assert todo == []
        assert "cooldown" in skipped[0]["why"]

    def test_the_cooldown_expires(self, monkeypatch):
        mod = self._mod(monkeypatch)
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(hours=30)).isoformat()
        todo, _ = mod.auto_investigate_candidates([
            self._f(key="old", investigation={"state": "stale", "at": old})])
        assert [f["key"] for f in todo] == ["old"]

    def test_a_brand_new_finding_is_never_delayed_by_the_cooldown(self, monkeypatch):
        """No prior row means nothing to cool down from — a new critical must
        be analysed on the run it appears, not 12h later."""
        mod = self._mod(monkeypatch)
        todo, _ = mod.auto_investigate_candidates([self._f(key="fresh")])
        assert [f["key"] for f in todo] == ["fresh"]

    def test_a_STALE_investigation_IS_redone(self, monkeypatch):
        """Stale means it explains older evidence — that is a reason to look
        again, not a reason to stay quiet."""
        mod = self._mod(monkeypatch)
        todo, _ = mod.auto_investigate_candidates([
            self._f(key="moved", investigation={
                "state": "stale",
                "at": (datetime.datetime.now(datetime.timezone.utc)
                       - datetime.timedelta(hours=48)).isoformat()})])
        assert [f["key"] for f in todo] == ["moved"]

    def test_UNREADABLE_investigation_state_blocks_dispatch(self, monkeypatch):
        """★ The guard that was dead code when first written.

        `_attach_investigations` reports a failed read by setting
        `investigation_unreadable` on the finding and leaving `investigation`
        ABSENT — not by setting `state: 'unreadable'`. Checking the wrong key
        made every finding look never-investigated on any DB blip, which
        re-runs the ~48s chain and stacks a second wall of analysis on the
        issue. BLIND is not 'none', here as everywhere else.
        """
        mod = self._mod(monkeypatch)
        todo, skipped = mod.auto_investigate_candidates([
            self._f(key="unknown", investigation_unreadable="db unreachable")])
        assert todo == [], "we cannot tell if this was already analysed"
        assert "unreadable" in skipped[0]["why"]


class TestAutoInvestigateRefusesLoudly:
    """Every refusal names itself. A lane that declines silently looks healthy,
    which is this repo's most repeated failure."""

    def _client(self, monkeypatch, latest, err=None):
        import flask
        from routes import qa_superuser_dashboard as mod
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "secret")
        monkeypatch.delenv("QA_AUTO_INVESTIGATE", raising=False)
        monkeypatch.setattr(mod, "_load",
                            lambda limit=1: {"latest": latest, "error": err,
                                             "history": []})
        monkeypatch.setattr(mod, "_attach_investigations", lambda l: None)
        app = flask.Flask(__name__)
        app.register_blueprint(mod.qa_superuser_dashboard_bp)
        return app.test_client(), mod

    def _run(self, findings=(), canary=True, when=None):
        when = when or datetime.datetime.now(datetime.timezone.utc)
        return {"generated_at": when.isoformat(), "canary_fired": canary,
                "findings": list(findings), "counts": {}}

    def _post(self, client, **body):
        return client.post("/api/v1/admin/qa-superuser/auto-investigate",
                           headers={"X-Admin-Key": "secret"}, json=body)

    def test_unauthorized_without_the_admin_key(self, monkeypatch):
        client, _ = self._client(monkeypatch, self._run())
        r = client.post("/api/v1/admin/qa-superuser/auto-investigate", json={})
        assert r.status_code == 401

    def test_the_kill_switch_stops_it(self, monkeypatch):
        client, _ = self._client(monkeypatch, self._run())
        monkeypatch.setenv("QA_AUTO_INVESTIGATE", "0")
        body = self._post(client).get_json()
        assert body["ok"] is False and body["refused"] == "kill switch"

    def test_an_unreadable_board_refuses_rather_than_guesses(self, monkeypatch):
        client, _ = self._client(monkeypatch, None, err="database unreachable")
        r = self._post(client)
        assert r.status_code == 503
        assert r.get_json()["refused"] == "board unreadable"

    def test_a_stale_board_is_refused(self, monkeypatch):
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(hours=30))
        client, _ = self._client(monkeypatch, self._run(when=old))
        body = self._post(client).get_json()
        assert body["refused"] == "board is stale"
        assert body["stale_hours"] > 9

    def test_a_run_whose_canary_did_not_fire_is_refused(self, monkeypatch):
        client, _ = self._client(monkeypatch, self._run(canary=False))
        body = self._post(client).get_json()
        assert body["refused"] == "must-fail control did not fire"

    def test_dry_run_dispatches_nothing_and_says_what_it_would_do(self, monkeypatch):
        fired = []
        client, mod = self._client(monkeypatch, self._run([
            {"key": "a", "verdict": "RED", "severity": "critical", "evidence": "e"}]))
        monkeypatch.setattr(mod, "_run_investigation",
                            lambda f: fired.append(f) or (True, "stored"))
        body = self._post(client, dry_run=True).get_json()
        assert body["would_dispatch"] == ["a"]
        assert fired == [], "dry run must touch nothing"

    def test_the_cap_reports_what_it_deferred_rather_than_dropping_it(
            self, monkeypatch):
        """A cap that silently truncates reads as 'everything was handled'."""
        findings = [{"key": f"k{i}", "verdict": "RED", "severity": "critical",
                     "evidence": "e"} for i in range(5)]
        client, _ = self._client(monkeypatch, self._run(findings))
        body = self._post(client, dry_run=True, limit=2).get_json()
        assert body["would_dispatch"] == ["k0", "k1"]
        assert body["deferred_to_next_run"] == ["k2", "k3", "k4"]

    def test_the_limit_is_bounded_however_it_is_asked(self, monkeypatch):
        findings = [{"key": f"k{i}", "verdict": "RED", "severity": "critical",
                     "evidence": "e"} for i in range(40)]
        client, mod = self._client(monkeypatch, self._run(findings))
        body = self._post(client, dry_run=True, limit=999).get_json()
        assert len(body["would_dispatch"]) == mod.AUTO_INVESTIGATE_MAX_LIMIT

    def test_it_investigates_and_never_proposes(self, monkeypatch):
        """The line that has held since the autonomy core: analyse, never
        generate a diff unasked. propose.py's clinching case is that the
        edge-caching finding had TWO opposite valid remedies and an auto-fixer
        would have picked the one that re-creates the Neon stampede.

        ★ Asserts BEHAVIOUR, not source text. A grep for "propose" would be
          satisfied by this very docstring and defeated by any indirection —
          the same weakness that let a signature mismatch through
          test_probe_is_registered_in_the_runner for two days.
        """
        import threading
        investigated, proposed = [], []
        client, mod = self._client(monkeypatch, self._run([
            {"key": "a", "verdict": "RED", "severity": "critical",
             "evidence": "e"}]))

        # Run the dispatch thread inline so the assertion sees the real calls.
        class _Inline:
            def __init__(self, target=None, args=(), daemon=None):
                self._t, self._a = target, args

            def start(self):
                self._t(*self._a)

        monkeypatch.setattr(threading, "Thread", _Inline)
        monkeypatch.setattr(mod, "_run_investigation",
                            lambda f: (investigated.append(f["key"]),
                                       (True, "stored"))[1])
        monkeypatch.setattr(mod, "_run_proposal",
                            lambda meta: proposed.append(meta))

        body = self._post(client).get_json()
        assert body["dispatched"] == ["a"]
        assert investigated == ["a"], "the analysis must actually run"
        assert proposed == [], \
            "the automatic lane must never generate a diff — that stays a click"
