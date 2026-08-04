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
from tools.qa_superuser.probe_mcp import _is_envelope
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
class TestEnvelopeClassification:
    @pytest.mark.parametrize("key", [
        "upgrade", "starter_pack", "for_your_human", "quota",
        "_entity", "_gated", "_recent_facilities_total_in_pro",
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
