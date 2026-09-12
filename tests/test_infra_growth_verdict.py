"""The growth board's verdict must not be narrower than its own detail lines.

★★★ THE DEFECT (measured 2026-09-12 05:40Z, infra-growth-tracker on main)
The run printed, per layer:

    power_plants_discovered: no staleness threshold is declared for this layer,
        so it is never flagged as overdue; last ingest 194d ago
    metro_fiber_routes: +1,137 new rows in the last 7d [⚠ freshness is NOT
        coming from the main source: 'carrier_kmz:zayo_2016wayback' … 81d behind]
    substations: +23 new rows in the last 7d [⚠ … 'HIFLD' … 28d behind]

and then, as its last line:

    ✅ no flatlines — all layers within expected cadence

That summary was computed from `flatlines` alone. A layer with no declared
threshold never enters the flatline test, and the mask marker is appended to
status_reason without touching `status` or `flatline` — so neither could reach
the green line. Two predicates, one output, and the narrower one printed last.

WHAT THESE TESTS HOLD THE FIX TO
  1. ONE predicate, routes/infra_growth._verdict, and it is an ALLOWLIST: a
     status word added later is held, never green.
  2. Every signal a detail line prints reaches it — status, flatline, the
     dominant-source mask (through the SAME _is_masked the marker calls),
     known_issue — plus a declared layer with no record at all, the one case
     _summary's `if not rows: continue` hides from everything else.
  3. The snapshot and growth responses publish it.
  4. The workflow only RENDERS it. The heredoc is executed here against
     fixtures — one of them built by the real _verdict over the live shapes
     above — so a second predicate cannot creep back into the YAML unseen.

NO NETWORK, NO DB. _verdict is extracted with ast and executed (the idiom in
tests/test_whats_new_layer_status.py); only the endpoint test needs flask, and it
imports it inside the test.
"""
import ast
import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

GROWTH = os.path.join(ROOT, "routes", "infra_growth.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "infra-growth-tracker.yml")
GREEN_LINE = "all layers within expected cadence"
_WANTED = ("_LAYERS", "_GREEN_STATUSES", "_is_masked", "_verdict")
_LIVE_HELD = {"power_plants_discovered", "substations", "metro_fiber_routes"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _verdict_ns():
    """Extract and EXECUTE the shipped _verdict and what it needs.

    Executed, not grepped: a presence check goes green on a re-broken body."""
    from util.dominant_source import MASK_LAG_DAYS
    tree = ast.parse(_read(GROWTH))
    ns, found = {"MASK_LAG_DAYS": MASK_LAG_DAYS}, set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign):
            name = next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
        else:
            continue
        if name in _WANTED:
            exec(compile(ast.Module(body=[node], type_ignores=[]), GROWTH, "exec"), ns, ns)
            found.add(name)
    missing = sorted(set(_WANTED) - found)
    assert not missing, f"routes/infra_growth.py no longer defines {missing}"
    assert len(ns["_LAYERS"]) >= 10, "_LAYERS collapsed — this suite would judge a toy board"
    return ns


def _rec(layer, status="on_cadence", **kw):
    rec = {"layer": layer, "status": status, "flatline": False, "dominant_source": None,
           "dominant_source_lag_days": 0, "known_issue": None}
    rec.update(kw)
    return rec


def _board(ns, **overrides):
    """One green record per declared layer; an override replaces one, None drops it."""
    out = []
    for label, _tbl, _cat, _stale in ns["_LAYERS"]:
        if label in overrides:
            if overrides[label] is not None:
                out.append(overrides[label])
            continue
        out.append(_rec(label))
    return out


def _live(ns):
    """The three layers infra-growth-tracker printed green over on 2026-09-12."""
    labels = {row[0] for row in ns["_LAYERS"]}
    assert _LIVE_HELD <= labels, (
        "precondition: the live layers must still be declared, or these tests "
        "judge a board that no longer exists")
    return _board(
        ns,
        power_plants_discovered=_rec("power_plants_discovered", "unjudged",
                                     dominant_source_lag_days=None),
        substations=_rec("substations", "growing", dominant_source="HIFLD",
                         dominant_source_lag_days=28),
        metro_fiber_routes=_rec("metro_fiber_routes", "growing",
                                dominant_source="carrier_kmz:zayo_2016wayback",
                                dominant_source_lag_days=81),
    )


# ── 1. the predicate ────────────────────────────────────────────────────────
def test_a_board_where_every_declared_layer_is_judged_green_is_green():
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns))
    assert v["all_green"] is True, v
    assert v["held"] == [] and v["not_reported"] == []
    assert v["declared"] == v["reported"] == len(ns["_LAYERS"])


def test_the_board_of_2026_09_12_is_not_green_and_names_all_three_layers():
    """The exact run that printed the green line."""
    ns = _verdict_ns()
    v = ns["_verdict"](_live(ns))
    assert v["all_green"] is False
    held = {h["layer"]: h for h in v["held"]}
    assert set(held) == _LIVE_HELD, held
    assert held["power_plants_discovered"]["why"] == ["status=unjudged"]
    assert any("masked" in w and "HIFLD" in w for w in held["substations"]["why"])
    assert held["substations"]["status"] == "growing", (
        "a masked layer is held WITHOUT its status being rewritten — the status "
        "is still true as far as it goes")


@pytest.mark.parametrize("status", ["unjudged", "measuring", "unmeasurable", "overdue",
                                    None, "", "a_status_word_added_next_month"])
def test_every_status_outside_the_allowlist_is_held(status):
    """Allowlist, not denylist: a word nobody has classified is never green."""
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns, data_centers=_rec("data_centers", status)))
    assert v["all_green"] is False
    assert [h["layer"] for h in v["held"]] == ["data_centers"]
    assert v["held"][0]["why"] == [f"status={status}"]


@pytest.mark.parametrize("status", ["growing", "refreshed", "on_cadence"])
def test_the_three_affirmative_statuses_are_green(status):
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns, data_centers=_rec("data_centers", status)))
    assert v["all_green"] is True, v["held"]


def test_a_flatlined_layer_is_held():
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns, gas_pipelines=_rec("gas_pipelines", flatline=True)))
    assert v["all_green"] is False
    assert v["held"] == [{"layer": "gas_pipelines", "status": "on_cadence", "why": ["flatline"]}]


def test_an_open_known_issue_is_held():
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns, fcc_fiber_hexes=_rec(
        "fcc_fiber_hexes", known_issue={"ref": "SH52-999", "note": "structurally stuck"})))
    assert v["all_green"] is False
    assert v["held"][0]["why"] == ["known issue SH52-999"]


def test_a_declared_layer_with_no_snapshot_record_is_not_green():
    """_summary skips a layer with no history (`if not rows: continue`), so it is
    absent from every record — the one case nothing else on the board can see."""
    ns = _verdict_ns()
    v = ns["_verdict"](_board(ns, subsea_cables=None))
    assert v["all_green"] is False
    assert v["not_reported"] == ["subsea_cables"] and v["held"] == []
    assert v["reported"] == v["declared"] - 1


def test_an_empty_board_is_not_green():
    ns = _verdict_ns()
    v = ns["_verdict"]([])
    assert v["all_green"] is False and len(v["not_reported"]) == len(ns["_LAYERS"])


# ── 2. one mask predicate for the marker and the verdict ────────────────────
def test_the_mask_boundary():
    ns = _verdict_ns()
    masked, lag = ns["_is_masked"], ns["MASK_LAG_DAYS"]
    assert masked(lag) is True and masked(lag + 60) is True
    assert masked(lag - 1) is False and masked(0) is False and masked(None) is False


def test_the_marker_and_the_verdict_call_the_same_mask_predicate():
    """If the marker kept an inline comparison, the line that warns and the
    verdict that sums the warnings could disagree about which layers are masked."""
    tree = ast.parse(_read(GROWTH))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for caller in ("_summary", "_verdict"):
        calls = {getattr(c.func, "id", None) for c in ast.walk(fns[caller])
                 if isinstance(c, ast.Call)}
        assert "_is_masked" in calls, f"{caller} no longer calls _is_masked"
    for name, fn in fns.items():
        if name == "_is_masked":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                assert not any(isinstance(o, ast.Name) and o.id == "MASK_LAG_DAYS"
                               for o in operands), (
                    f"{name} compares against MASK_LAG_DAYS inline — a second mask predicate")


# ── 3. the responses publish it ─────────────────────────────────────────────
def _growth_client(monkeypatch, layers):
    flask = pytest.importorskip("flask")
    from routes import infra_growth as G

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://growth.example/db")
    monkeypatch.setattr(G, "_admin_ok", lambda: True)
    monkeypatch.setattr(G.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(G, "_ensure", lambda cur: None)
    monkeypatch.setattr(G, "_count", lambda cur, tbl, label: 1)
    monkeypatch.setattr(G, "_summary", lambda cur: (layers, []))
    app = flask.Flask(__name__)
    app.register_blueprint(G.infra_growth_bp)
    return app.test_client()


@pytest.mark.parametrize("method,path", [("post", "/api/v1/admin/infra-growth/snapshot"),
                                         ("get", "/api/v1/admin/infra-growth")])
def test_the_snapshot_and_growth_responses_publish_the_verdict(monkeypatch, method, path):
    ns = _verdict_ns()
    body = getattr(_growth_client(monkeypatch, _live(ns)), method)(path).get_json()
    assert body["ok"] is True, body
    v = body.get("verdict")
    assert isinstance(v, dict), f"{path} does not publish a verdict: {sorted(body)}"
    assert v["all_green"] is False
    assert {h["layer"] for h in v["held"]} == _LIVE_HELD


# ── 4. the workflow renders, it does not re-derive ──────────────────────────
def _renderer_block():
    lines = _read(WORKFLOW).splitlines()
    marks = [i for i, line in enumerate(lines) if "growth-verdict-renderer" in line]
    assert len(marks) == 1, f"expected exactly one renderer block, found {len(marks)}"
    start = max(i for i in range(marks[0]) if lines[i].rstrip().endswith("<<'PY'"))
    end = next(i for i in range(marks[0], len(lines)) if lines[i].strip() == "PY")
    return start, end, lines


def _render(tmp_path, payload):
    start, end, lines = _renderer_block()
    script = tmp_path / "render.py"
    script.write_text(textwrap.dedent("\n".join(lines[start + 1:end])) + "\n", encoding="utf-8")
    data = tmp_path / "g.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "GROWTH_JSON": str(data)})
    assert r.returncode == 0, (
        f"the renderer is warning-level and must exit 0; got {r.returncode}\n{r.stderr}")
    return r.stdout


def test_the_workflow_never_prints_green_over_the_board_of_2026_09_12(tmp_path):
    """End to end: the REAL _verdict over the live shapes, rendered by the REAL
    heredoc. This is the run that printed the green line."""
    ns = _verdict_ns()
    out = _render(tmp_path, {"flatlines": [], "verdict": ns["_verdict"](_live(ns))})
    assert GREEN_LINE not in out, out
    assert "::warning::growth verdict NOT green" in out
    for layer in sorted(_LIVE_HELD):
        assert layer in out, f"{layer} held but not named:\n{out}"


def test_a_green_verdict_still_prints_the_green_line(tmp_path):
    ns = _verdict_ns()
    out = _render(tmp_path, {"flatlines": [], "verdict": ns["_verdict"](_board(ns))})
    assert GREEN_LINE in out and "NOT green" not in out, out


def test_a_response_without_a_verdict_is_unmeasured_not_green(tmp_path):
    """An older deploy answering the snapshot without the new key."""
    out = _render(tmp_path, {"flatlines": [], "layers": []})
    assert GREEN_LINE not in out and "UNMEASURED" in out, out


def test_only_the_boolean_true_is_green(tmp_path):
    out = _render(tmp_path, {"flatlines": [],
                             "verdict": {"all_green": "true", "held": [], "not_reported": []}})
    assert GREEN_LINE not in out, out


def test_flatlines_still_warn_beside_the_verdict(tmp_path):
    ns = _verdict_ns()
    board = _board(ns, gas_pipelines=_rec("gas_pipelines", flatline=True))
    out = _render(tmp_path, {
        "flatlines": ["gas_pipelines (no change in 140d, expected <130d, last ingest x)"],
        "verdict": ns["_verdict"](board)})
    assert "::warning::infra layers flatlined" in out and GREEN_LINE not in out, out


def test_no_other_line_of_the_workflow_can_print_the_green_claim():
    """The old summary was a bash `echo` outside any heredoc. Only the renderer
    may carry the claim; a comment quoting it is not code."""
    start, end, lines = _renderer_block()
    for i, line in enumerate(lines):
        code = line.strip()
        if GREEN_LINE in code and not code.startswith("#"):
            assert start < i < end, (
                f"line {i + 1} prints the green claim outside the renderer: {code}")
