"""D1 (2026-09-02): an EU feed failure cannot hide inside a green data-pulse.

WHAT WAS MEASURED (2026-09-02 00:29-04:28Z)
  - /api/v1/heal/findings: 34 x iso_metric_count_zero_24h (every EU_* zone +
    ENTSOE). /api/v1/iso/eu/health -> HTTP 200 {"live_feed_ok": false};
    /api/v1/iso/eu/snapshot -> 503 no_zone_answered; /api/v1/iso/eu/debug ->
    upstream HTTP 503 with an HTML "Transparency Platform" body. web-api.tp.
    entsoe.eu answered the SAME 503 HTML tokenless from an unrelated IP with
    both a python-requests and a browser User-Agent at 04:28Z — a platform
    outage, not a token or UA block.
  - data-pulse run 33572651578 (23:48Z): {"failed_count": 9, "iso_count": 21}
    -> `::notice rows=586 failed=9`, job GREEN. It failed only when EVERY
    extractor failed, and the notice never named anyone; the response was
    printed to 1000 bytes and ENTSOE sat past the cut.
  - Root of the 9: `failed = status not in ("ok",)` counted 8 LIVE-only
    modules that never set `status` (their failure channel is errors[]) plus
    AESO's honest no_new_data. So 9/21 read "failed" on EVERY tick and the
    one real failure was indistinguishable.
  - No deadman feed existed for ENTSO-E (iso-data-pull covers the 7 US ISOs).

THE CONTRACT
  1. classify_result: a status-less summary is judged on errors[]; an earned
     zero (no_new_data) is not a failure.
  2. summarize_families: one DERIVED verdict per producer family, with the
     failing members NAMED; rows summed; max_content_date only from a real
     upstream stamp.
  3. data-pulse.yml: names the failing extractors in the ::notice, emits
     ::error and fails the run when a MUST_HAVE family is not ok, and beats
     the per-family ledger rows by COPYING the orchestrator's verdict — no
     literal status in the beat step. Behavioural: the embedded python is
     extracted from the YAML and executed against fixtures.
  4. iso_eu_entsoe sets `status`, carries data_period_end_newest, and
     /api/v1/iso/eu/health answers 503 when live_feed_ok is false.
  5. freshness_public / dcpi_freshness_watchdog / L23 audit dim: the EU zone
     streams roll up to one named family line; a dead family is a freshness
     failure even while every market's computed_at is fresh.

NO NETWORK, NO DB. The workflow scripts run in a subprocess/in-process with
urllib patched; module imports are the light ISO modules only (no main.py).
"""
import ast
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request
from contextlib import redirect_stdout

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "data-pulse.yml"


# ── 1. classify_result ──────────────────────────────────────────────────
@pytest.mark.parametrize("summary, want", [
    ({"iso": "NGESO", "errors": [], "rows_inserted": 3}, "ok"),           # status-less, healthy
    ({"iso": "ENTSOE", "errors": ["entsoe_live_fetch_failed"]}, "failed"),  # status-less, errors[]
    ({"iso": "AESO", "status": "no_new_data", "note": "x"}, "no_new_data"),
    ({"iso": "X", "status": "import_error", "error": "boom"}, "failed"),
    ({"iso": "X", "status": "timeout", "error": "t"}, "failed"),
    ({"iso": "X", "status": "ok", "errors": ["late warning"]}, "ok"),      # status wins
    ("garbage", "failed"),
])
def test_classify_result(summary, want):
    from routes.iso_orchestrator import classify_result
    assert classify_result(summary)[0] == want


def test_classify_result_names_the_reason():
    from routes.iso_orchestrator import classify_result
    assert classify_result({"iso": "ENTSOE", "errors": ["e1", "e2"]}) == ("failed", "e1")
    assert classify_result({"iso": "X", "status": "import_error", "error": "boom"})[1] == "boom"


# ── 2. summarize_families ───────────────────────────────────────────────
def _by_iso(results):
    from routes.iso_orchestrator import classify_result
    out = {}
    for r in results:
        v, reason = classify_result(r)
        out[r["iso"]] = {"verdict": v, "rows_inserted": int(r.get("rows_inserted") or 0),
                         "reason": reason}
    return out


_INTL = ["NGESO", "AEMO", "TAIPOWER", "OCCTO", "EMA", "ONS", "KEPCO-KR",
         "IESO", "AESO", "HYDROQUEBEC"]


def _results(entsoe=None, intl_failed=(), intl_rows=2):
    rs = [{"iso": "PJM", "status": "ok", "rows_inserted": 5}]
    if entsoe is not None:
        rs.append(entsoe)
    for m in _INTL:
        if m in intl_failed:
            rs.append({"iso": m, "errors": [f"{m.lower()}_fetch_failed"]})
        else:
            rs.append({"iso": m, "errors": [], "rows_inserted": intl_rows})
    return rs


def test_entsoe_failure_is_a_failed_family_with_the_reason_named():
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe={"iso": "ENTSOE", "status": "error",
                          "errors": ["entsoe_live_fetch_failed — all zones unreachable"]})
    fam = summarize_families(_by_iso(rs), rs)["iso-eu-entsoe"]
    assert fam["status"] == "failed"
    assert fam["rows_inserted"] == 0
    assert fam["max_content_date"] is None, "no upstream stamp -> None, never the clock"
    assert "ENTSOE" in fam["note"] and "unreachable" in fam["note"]


def test_healthy_entsoe_is_success_with_the_newest_period_as_content_date():
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe={"iso": "ENTSOE", "status": "ok", "rows_inserted": 40,
                          "data_period_end_newest": "2026-09-02T03:00:00+00:00"})
    fam = summarize_families(_by_iso(rs), rs)["iso-eu-entsoe"]
    assert fam == {**fam, "status": "success", "rows_inserted": 40,
                   "max_content_date": "2026-09-02T03:00:00+00:00"}


def test_zero_new_rows_from_a_healthy_family_is_an_earned_no_new_data():
    """After D4 a repeated reading dedups to 0 rows; that must reset the
    board's zero-row counter (no_new_data), not climb it."""
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe={"iso": "ENTSOE", "status": "ok", "rows_inserted": 0})
    assert summarize_families(_by_iso(rs), rs)["iso-eu-entsoe"]["status"] == "no_new_data"


def test_a_missing_member_is_failed_not_absent():
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe=None)
    fam = summarize_families(_by_iso(rs), rs)["iso-eu-entsoe"]
    assert fam["status"] == "failed" and "no member" in fam["note"]


def test_intl_family_is_degraded_when_some_members_fail_and_names_them():
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe={"iso": "ENTSOE", "status": "ok", "rows_inserted": 1},
                  intl_failed=("KEPCO-KR", "EMA"))
    fam = summarize_families(_by_iso(rs), rs)["iso-intl"]
    assert fam["status"] == "degraded"
    assert "KEPCO-KR" in fam["note"] and "EMA" in fam["note"]
    assert fam["rows_inserted"] == 2 * 8
    assert fam["members"]["KEPCO-KR"] == "failed" and fam["members"]["NGESO"] == "ok"


def test_intl_family_is_failed_only_when_every_member_fails():
    from routes.iso_orchestrator import summarize_families
    rs = _results(entsoe={"iso": "ENTSOE", "status": "ok", "rows_inserted": 1},
                  intl_failed=tuple(_INTL))
    assert summarize_families(_by_iso(rs), rs)["iso-intl"]["status"] == "failed"


def test_us_isos_are_not_in_any_family():
    """iso-data-pull already owns the US ISOs' ledger row — a second writer
    for the same fact is the one-direction-masking trap."""
    from routes.iso_orchestrator import _FAMILIES
    members = {m for _, ms in _FAMILIES for m in ms}
    for us in ("PJM", "ERCOT", "CAISO", "NYISO", "MISO", "SPP", "ISONE", "UTILITY_BAS"):
        assert us not in members
    assert {f for f, _ in _FAMILIES} == {"iso-eu-entsoe", "iso-intl"}


def test_extract_all_publishes_families_and_named_failures():
    src = (ROOT / "routes" / "iso_orchestrator.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "extract_all")
    called = {getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "summarize_families" in called and "classify_result" in called
    jsonify_kw = {k.arg for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "jsonify" for k in n.keywords}
    for key in ("families", "failed_isos", "no_new_data_isos", "by_iso", "failed_count"):
        assert key in jsonify_kw, f"extract_all response lacks {key}"


# ── 3. data-pulse.yml ───────────────────────────────────────────────────
def _steps():
    d = yaml.safe_load(WF.read_text())
    return d, d["jobs"]["pulse"]["steps"]


def _step(name_sub):
    _, steps = _steps()
    hits = [s for s in steps if name_sub.lower() in (s.get("name") or "").lower()]
    assert len(hits) == 1, f"expected one step matching {name_sub!r}, got {[s.get('name') for s in hits]}"
    return hits[0]


def _heredoc(step):
    run = step["run"]
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", run, re.S)
    assert m, "step has no python heredoc"
    return m.group(1)


def _uncommented(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _fixture(entsoe_status, entsoe_note="entsoe_live_fetch_failed — all zones unreachable"):
    return {
        "total_rows_inserted": 586, "failed_count": 1 if entsoe_status == "failed" else 0,
        "failed_isos": ([{"iso": "ENTSOE", "reason": entsoe_note}]
                        if entsoe_status == "failed" else []),
        "no_new_data_isos": ["AESO"],
        "families": {
            "iso-eu-entsoe": {"status": entsoe_status, "rows_inserted": 0 if entsoe_status != "success" else 40,
                              "max_content_date": "2026-09-02T03:00:00+00:00", "note": entsoe_note},
            "iso-intl": {"status": "degraded", "rows_inserted": 16,
                         "max_content_date": None, "note": "failed: KEPCO-KR (timeout)"},
        },
    }


def _run_iso_script(tmp_path, fixture, must="iso-eu-entsoe"):
    (tmp_path / "iso_response.json").write_text(json.dumps(fixture))
    out = tmp_path / "gh_output"
    out.write_text("")
    env = {**os.environ, "MUST_HAVE_FAMILIES": must, "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run([sys.executable, "-"], input=_heredoc(_step("Trigger ISO orchestrator")),
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    outputs = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
    return proc.stdout, outputs


def test_must_have_family_env_names_entsoe():
    d, _ = _steps()
    assert "iso-eu-entsoe" in d["env"]["MUST_HAVE_FAMILIES"].split(",")


def test_iso_step_names_the_failing_extractors_and_errors_on_a_must_have_failure(tmp_path):
    stdout, outputs = _run_iso_script(tmp_path, _fixture("failed"))
    notice = next(l for l in stdout.splitlines() if l.startswith("::notice"))
    assert "failed_isos=ENTSOE(" in notice and "no_new_data=AESO" in notice
    assert any(l.startswith("::error") and "iso-eu-entsoe" in l for l in stdout.splitlines())
    assert outputs["must_have_failed"] == "1"
    assert "iso-eu-entsoe=failed" in outputs["must_have_error"]
    assert outputs["rows"] == "586" and outputs["failed"] == "1"


@pytest.mark.parametrize("status", ["success", "no_new_data"])
def test_iso_step_passes_when_the_must_have_family_is_ok(tmp_path, status):
    stdout, outputs = _run_iso_script(tmp_path, _fixture(status, "ok"))
    assert outputs["must_have_failed"] == "0"
    assert "::error" not in stdout


def test_iso_step_treats_a_missing_family_block_as_a_failure(tmp_path):
    """An older backend (no `families`) must not read as green."""
    fx = _fixture("success", "ok")
    del fx["families"]
    stdout, outputs = _run_iso_script(tmp_path, fx)
    assert outputs["must_have_failed"] == "1" and "missing" in outputs["must_have_error"]


def test_totals_and_the_final_step_turn_a_must_have_failure_into_a_red_run():
    totals = _step("Compute totals")["run"]
    assert "steps.iso.outputs.must_have_failed" in totals
    assert re.search(r'must_have_failed \}\}" = "1" \]; then\s*\n\s*echo "status=failure"', totals)
    _, steps = _steps()
    last = steps[-1]
    assert "must-have" in (last.get("name") or "").lower()
    assert last.get("if") and "must_have_failed" in last["if"]
    assert re.search(r"^\s*exit 1\s*$", last["run"], re.M)
    # After the notify / heartbeat / beat steps, so the failure is RECORDED first.
    names = [s.get("name") or "" for s in steps]
    assert names.index(last["name"]) > names.index(_step("Beat the per-family")["name"])
    assert names.index(_step("Beat the per-family")["name"]) > names.index(_step("Notify the autonomous")["name"])


def test_beat_step_copies_the_orchestrators_verdict_and_never_asserts_one(tmp_path, monkeypatch):
    step = _step("Beat the per-family")
    script = _heredoc(step)
    assert '"success"' not in _uncommented(script) and "'success'" not in _uncommented(script), (
        "the beat step must not contain a literal status")
    assert re.search(r"cadence_hours\"?\s*:\s*\d+", script), "the producer declares its cadence"
    assert "X-Admin-Key" in script and "/api/v1/admin/ingest-runs/beat" in script

    fx = _fixture("failed")
    (tmp_path / "iso_response.json").write_text(json.dumps(fx))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_KEY", "test-admin-key")
    sent = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        sent.append((req.full_url, dict(req.header_items()), json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<beat-step>", "exec"), {"__name__": "__beat__"})
    assert len(sent) == 2, buf.getvalue()
    by_feed = {b["feed"]: (u, h, b) for u, h, b in sent}
    assert set(by_feed) == {"iso-eu-entsoe", "iso-intl"}
    url, headers, body = by_feed["iso-eu-entsoe"]
    assert url.endswith("/api/v1/admin/ingest-runs/beat")
    assert headers.get("X-admin-key") == "test-admin-key"
    assert body["status"] == "failed", "status must be the orchestrator's verdict, verbatim"
    assert body["rows_inserted"] == 0
    assert body["max_content_date"] == "2026-09-02T03:00:00+00:00"
    assert isinstance(body["cadence_hours"], int) and body["cadence_hours"] >= 1
    assert "unreachable" in body["note"]
    assert "max_content_date" not in by_feed["iso-intl"][2], "None must not be sent as a date"
    assert by_feed["iso-intl"][2]["status"] == "degraded"


def test_beat_step_warns_and_beats_nothing_without_a_families_block(tmp_path, monkeypatch):
    (tmp_path / "iso_response.json").write_text(json.dumps({"total_rows_inserted": 1}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not beat with no verdict"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(_heredoc(_step("Beat the per-family")), "<beat-step>", "exec"), {})
    assert "::warning" in buf.getvalue()


# ── 4. iso_eu_entsoe status + health code ───────────────────────────────
def test_entsoe_run_extraction_sets_status_on_every_path(monkeypatch):
    from routes import iso_eu_entsoe as eu
    monkeypatch.setattr(eu, "_token", lambda: "")
    assert eu.run_extraction()["status"] == "error"
    monkeypatch.setattr(eu, "_token", lambda: "tok")
    monkeypatch.setattr(eu, "_live_snapshot", lambda: None)
    s = eu.run_extraction()
    assert s["status"] == "error" and s["rows_inserted"] == 0
    snap = {"metrics": {"generation_total_mw": {"value": 1.0, "unit": "MW"}},
            "zones": {"DE_LU": {"data_period_end": "2026-09-02T03:00:00+00:00",
                                "generation_total_mw": 1.0, "renewable_pct": 1.0, "gas_pct": 1.0},
                      "FR": {"data_period_end": "2026-09-02T02:00:00+00:00",
                             "generation_total_mw": 1.0, "renewable_pct": 1.0, "gas_pct": 1.0}}}
    monkeypatch.setattr(eu, "_live_snapshot", lambda: snap)
    monkeypatch.setattr(eu, "_persist_metrics", lambda s: 7)
    s = eu.run_extraction()
    assert s["status"] == "ok" and s["rows_inserted"] == 7
    assert s["data_period_end_newest"] == "2026-09-02T03:00:00+00:00"


def test_entsoe_health_carries_the_verdict_in_the_status_code(monkeypatch):
    from flask import Flask
    from routes import iso_eu_entsoe as eu
    app = Flask(__name__)
    app.register_blueprint(eu.iso_eu_entsoe_bp)
    monkeypatch.setattr(eu, "_token", lambda: "tok")
    monkeypatch.setattr(eu, "_zone_snapshot", lambda code, max_age=None: None)
    r = app.test_client().get("/api/v1/iso/eu/health")
    assert r.status_code == 503 and r.get_json()["live_feed_ok"] is False
    monkeypatch.setattr(eu, "_zone_snapshot",
                        lambda code, max_age=None: {"observed_age_s": 0, "data_age_s": 60,
                                                    "data_period_end": "x"})
    r = app.test_client().get("/api/v1/iso/eu/health")
    assert r.status_code == 200 and r.get_json()["live_feed_ok"] is True


# ── 5. family roll-up on the freshness surfaces ─────────────────────────
def test_feed_family_rollup_judges_on_the_anchor_and_names_stale_members():
    from routes.freshness_public import summarize_feed_families as f
    per = [{"stream": "ENTSOE", "age_hours": 50.0}, {"stream": "EU_DE_LU", "age_hours": 50.0},
           {"stream": "EU_FR", "age_hours": 1.0}, {"stream": "PJM", "age_hours": 0.2}]
    out = f(per, 4.0)
    assert set(out) == {"entsoe"}
    fam = out["entsoe"]
    assert fam["live_feed_ok"] is False
    assert fam["stale_streams"] == ["ENTSOE", "EU_DE_LU"] and fam["streams_total"] == 3
    assert fam["deadman_feed"] == "iso-eu-entsoe" and fam["health"] == "/api/v1/iso/eu/health"
    ok = f([{"stream": "ENTSOE", "age_hours": 1.0}, {"stream": "EU_BG", "age_hours": 90.0}], 4.0)
    assert ok["entsoe"]["live_feed_ok"] is True, "one intermittent zone must not pin the family dead"
    assert ok["entsoe"]["stale_streams"] == ["EU_BG"]


def test_feed_family_rollup_without_an_anchor_judges_on_members_and_empty_is_absent():
    from routes.freshness_public import summarize_feed_families as f
    out = f([{"stream": "EU_FR", "age_hours": 9.0}], 4.0)
    assert out["entsoe"]["live_feed_ok"] is False and "no anchor" in out["entsoe"]["live_feed_ok_basis"]
    assert f([], 4.0) == {} and f([{"stream": "PJM", "age_hours": 1.0}], 4.0) == {}
    assert f([{"stream": "ENTSOE", "age_hours": None}], 4.0) == {}, "unmeasured is absent, never ok"


def test_freshness_public_sla_breakdown_carries_the_family_rollup():
    src = (ROOT / "routes" / "freshness_public.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_sla_breakdown")
    called = {getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "summarize_feed_families" in called
    assert "feed_families" in ast.get_source_segment(src, fn)


def test_dcpi_freshness_watchdog_names_a_dead_live_feed():
    from routes.dcpi_freshness_watchdog import live_feeds_from_ages as f
    dead = f([("ENTSOE", 50.0), ("EU_DE_LU", 50.0), ("EU_FR", 49.0)])
    assert dead["stale"] == ["entsoe"] and dead["measured"] is True
    alive = f([("ENTSOE", 0.5), ("EU_DE_LU", 0.5)])
    assert alive["stale"] == [] and alive["families"]["entsoe"]["live_feed_ok"] is True
    none = f([])
    assert none["stale"] == [] and none["measured"] is False, "no rows is unmeasured, not fresh"
    src = (ROOT / "routes" / "dcpi_freshness_watchdog.py").read_text()
    route = next(n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef) and n.name == "dcpi_freshness")
    body = ast.get_source_segment(src, route)
    assert "live_feeds_from_ages(" in body and "stale_live_feeds" in body


def test_layer23_dcpi_freshness_dim_fails_on_a_dead_live_feed():
    """Exec the audit dim against a stub — no import of the lifecycle module."""
    src = (ROOT / "routes" / "brain_layer23_lifecycle.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_audit_dcpi_freshness")

    class _AuditUnavailable:  # never returned by the stub
        pass

    fresh = {"stats": {"total": 300, "fresh_24h": 300, "stale_1_3d": 0, "stale_3_7d": 0, "stale_7d": 0},
             "oldest_15": [{"market_name": "x"}]}
    for payload, want_ok in ((fresh, True),
                             ({**fresh, "stale_live_feeds": ["entsoe"]}, False),
                             ({**fresh, "stale_live_feeds": []}, True)):
        ns = {"_AuditUnavailable": _AuditUnavailable, "_call_internal": lambda _p, _r=payload: _r}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<l23>", "exec"), ns)
        out = ns["_audit_dcpi_freshness"]()
        assert out["ok"] is want_ok, (payload.get("stale_live_feeds"), out)
        if not want_ok:
            assert out["verdict"].startswith("live-feed-dead:") and "entsoe" in out["verdict"]
            assert out["stale_live_feeds"] == ["entsoe"]
