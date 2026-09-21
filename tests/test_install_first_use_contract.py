"""GET /api/v1/ops/install-stats/first-use — contract, no database.

The measurement's claims rest on facts about OTHER modules: which X-API-Key the
REST tracker records, what the self-serve claim mints, which event_types each
mcp_call_log writer stamps. Each test below reads those facts from the producer
(its code or its behaviour), never from a copy kept here, so the endpoint
cannot keep publishing a coverage story the producers no longer tell.
The SQL itself is proven against a real Postgres in
tests/test_install_first_use_sql.py.
"""
import ast
import datetime
import os
import re
import secrets

import pytest

flask = pytest.importorskip("flask")

from routes import api_usage_tracker as tracker  # noqa: E402
from routes.install_stats import (  # noqa: E402
    _EXCLUDE_NOTHING,
    _FIRST_USE_SQL,
    _MCP_EVENT_TYPES,
    _NOT_A_PROBE,
    _NOT_USE_EVENT_TYPES,
    _PROBE_PREFIX,
    _REST_EVENT_PATTERN,
    _REST_EVENT_TYPES,
    _ROW_NOT_A_PROBE,
    _first_use_params,
    _instrument,
    _key_record,
    _window_summary,
    register_install_stats,
)
from mcp_calls_deloop import (  # noqa: E402
    external_session_predicate,
    internal_tag_regex_predicate,
    real_ua_predicate,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UTC = datetime.timezone.utc


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel):
    return ast.parse(_read(rel))


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


# ── the route ───────────────────────────────────────────────────────────────

def test_route_is_registered():
    app = flask.Flask(__name__)
    register_install_stats(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/ops/install-stats/first-use" in rules
    assert "/api/v1/ops/install-stats" in rules


def test_control_and_populations_go_through_the_one_helper():
    """Same discipline as the ledger: a control written as its own query would
    prove only itself. The route must execute nothing but _first_use()."""
    fn = _fn(_tree("routes/install_stats.py"), "install_first_use")
    executes = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"]
    assert executes == [], "install_first_use runs its own SQL beside _first_use()"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_first_use"]
    assert len(calls) == 2, "expected one _first_use for the populations, one for the control"
    control = [c for c in calls if len(c.args) == 3]
    assert control and [a.id for a in control[0].args[1:]] == ["_PROBE_PREFIX", "_EXCLUDE_NOTHING"]


# ── the SQL's shape ─────────────────────────────────────────────────────────

def test_placeholders_match_the_bound_parameters():
    """One %s too many or too few and psycopg2 raises; a stray % 500s the route."""
    params = _first_use_params("web-%", _PROBE_PREFIX)
    assert _FIRST_USE_SQL.count("%s") == len(params)
    assert re.findall(r"%(?!s)", _FIRST_USE_SQL) == []


def test_the_population_carries_the_shared_probe_clause():
    assert _NOT_A_PROBE in _FIRST_USE_SQL
    params = _first_use_params("install-%", _PROBE_PREFIX)
    assert params[2:4] == ("install-%", _PROBE_PREFIX)


def test_the_row_rules_are_the_shared_predicates_not_lookalikes():
    for rendered in (real_ua_predicate("l.user_agent"),
                     external_session_predicate("l.session_id"),
                     internal_tag_regex_predicate("l.platform")):
        assert rendered in _ROW_NOT_A_PROBE
    assert _ROW_NOT_A_PROBE in _FIRST_USE_SQL


def test_sentinels_and_patterns_carry_no_accidental_wildcard():
    assert not set("%_") & set(_EXCLUDE_NOTHING)
    # the only wildcard in the REST pattern is its trailing %
    assert "_" not in _REST_EVENT_PATTERN and _REST_EVENT_PATTERN.count("%") == 1
    assert _REST_EVENT_PATTERN.endswith("%")


def test_the_published_rows_never_carry_the_key():
    """The per-key rows feed a PUBLIC response. The final SELECT must not
    project api_key; the joins may use it, the output may not."""
    flat = " ".join(_FIRST_USE_SQL.split())
    final = flat[flat.rindex("SELECT pop.client"):flat.rindex(" FROM pop ")]
    assert "api_key" not in final


# ── the facts the coverage story rests on, read from the producers ──────────

def _tracked_prefix(headers, query_string=None):
    """Drive the REAL before/after_request hooks and return what they buffered."""
    app = flask.Flask(__name__)

    @app.route("/api/v1/tracker-shape-probe")
    def _f():
        return "ok"

    saved = (tracker._ensure_schema, tracker._ensure_flusher_running)
    tracker._ensure_schema = lambda: None
    tracker._ensure_flusher_running = lambda: None
    try:
        tracker.install_tracker(app)
        with tracker._BUFFER_LOCK:
            tracker._BUFFER.clear()
        app.test_client().get("/api/v1/tracker-shape-probe", headers=headers,
                              query_string=query_string)
        with tracker._BUFFER_LOCK:
            got = [e["key_prefix"] for e in tracker._BUFFER]
            tracker._BUFFER.clear()
    finally:
        tracker._ensure_schema, tracker._ensure_flusher_running = saved
    return got


def _claim_key_literal_prefix():
    fn = _fn(_tree("flask_mcp_endpoints.py"), "claim_key")
    for n in ast.walk(fn):
        if (isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "api_key" for t in n.targets)
                and isinstance(n.value, ast.BinOp)
                and isinstance(n.value.left, ast.Constant)):
            return n.value.left.value
    return None


def test_the_tracker_records_exactly_the_shapes_the_endpoint_says_it_does():
    """basis.rest_coverage is built from TRACKED_KEY_PREFIXES; every shape named
    there must really be recorded, from the header the endpoint says."""
    assert set(tracker.TRACKED_KEY_PREFIXES) == {
        tracker.ACCOUNT_KEY_PREFIX, tracker.SELF_SERVE_KEY_PREFIX}
    n = tracker.STORED_PREFIX_LEN
    for shape in tracker.TRACKED_KEY_PREFIXES:
        key = shape + "a" * 30
        assert _tracked_prefix({"X-API-Key": key}) == [key[:n]], shape
        assert _tracked_prefix({"X-API-Key": key[:n - 1]}) == [], shape
    account = tracker.ACCOUNT_KEY_PREFIX + "a" * 30
    # A dchub_ key is recorded exactly as before 2026-09-21: X-API-Key only,
    # and even beside a server credential — partner-usage reads these rows.
    assert _tracked_prefix({"Authorization": "Bearer " + account}) == []
    assert _tracked_prefix({"X-API-Key": account, "X-Internal-Key": "k"}) == [account[:n]]
    # Server credentials alone are a class, never a key (unchanged).
    assert _tracked_prefix({"X-Internal-Key": "k"}) == ["internal"]
    assert _tracked_prefix({}) == []


def test_a_self_serve_claimed_key_is_recorded_on_rest_and_never_on_mcp_fan_out():
    """/api/v1/keys/claim mints the key shape the whole first-use measurement
    is about. Built from the claim handler's own literal, then pushed through
    the tracker's own hooks. Before 2026-09-21 every line below returned []."""
    prefix = _claim_key_literal_prefix()
    assert prefix, "claim_key no longer builds api_key as '<literal>' + ..."
    claimed = prefix + secrets.token_hex(16)
    stored = [claimed[:tracker.STORED_PREFIX_LEN]]
    # web-map: js/map.js sends X-API-Key, map.html sends Bearer
    assert _tracked_prefix({"X-API-Key": claimed}) == stored
    assert _tracked_prefix({"Authorization": "Bearer " + claimed}) == stored
    assert _tracked_prefix({"X-API-Key": " " + claimed + " "}) == stored
    # X-API-Key is the credential acted on; a Bearer beside it is not the caller
    assert _tracked_prefix({"X-API-Key": "junk",
                            "Authorization": "Bearer " + claimed}) == []
    # The MCP server's callAPI() fan-out: X-Internal-Key WITH the caller's key.
    # That is MCP use, so it must never land on the REST arm under the key.
    fan_out = {"X-API-Key": claimed, "X-Internal-Key": "k",
               "X-MCP-Platform": "claude", "X-MCP-Session": "s"}
    assert _tracked_prefix(fan_out) == ["internal"]
    # ... even when the server's credential is empty (DCHUB_INTERNAL_KEY unset)
    assert _tracked_prefix({"X-API-Key": claimed, "X-Internal-Key": ""}) == []
    assert _tracked_prefix({"Authorization": "Bearer " + claimed,
                            "X-DC-Internal-Token": ""}) == []
    assert _tracked_prefix({"X-API-Key": claimed}, "admin_key=") == []
    assert _tracked_prefix({"X-API-Key": claimed, "X-Admin-Key": "k"}) == ["admin"]
    assert _tracked_prefix({"X-API-Key": claimed, "X-Internal-Cron": "1"}) == ["cron"]
    # never a JWT or a lowercase scheme the backend does not read as a key
    assert _tracked_prefix({"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9." + "x" * 40}) == []
    assert _tracked_prefix({"Authorization": "bearer " + claimed}) == []


def test_mcp_event_types_are_the_track_writers_own_map():
    tree = _tree("flask_mcp_endpoints.py")
    maps = [n for n in ast.walk(tree) if isinstance(n, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "blocked_paid_only" for k in n.keys)]
    assert len(maps) == 1, "the status->event_type map in the track writer moved"
    written = {v.value for v in maps[0].values if isinstance(v, ast.Constant)}
    assert written == set(_MCP_EVENT_TYPES)


def _insert_sites():
    """Every string literal that inserts into mcp_call_log, per file."""
    sites = {}
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        rel_dir = os.path.relpath(dirpath, _ROOT)
        if rel_dir.split(os.sep)[0] in ("tests", "dchub-mcp-v2.1", ".git",
                                         "node_modules", ".venv", "venv"):
            dirnames[:] = []
            continue
        for f in filenames:
            if not f.endswith(".py"):
                continue
            rel = os.path.normpath(os.path.join(rel_dir, f))
            try:
                src = _read(rel)
                if "mcp_call_log" not in src:
                    continue
                tree = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                        and "INSERT INTO mcp_call_log" in n.value:
                    sites.setdefault(rel, []).append(n.value)
    return sites


def test_every_mcp_call_log_writer_is_classified():
    """A new writer would land on a channel by accident. Name every one."""
    sites = _insert_sites()
    assert sum(len(v) for v in sites.values()) >= 4, "scan found almost nothing"
    assert set(sites) == {"flask_mcp_endpoints.py", "routes/market_brief.py",
                          "routes/onboarding_page.py"}, (
        "mcp_call_log gained or lost a writer: classify its event_type in "
        "routes/install_stats.py, then update this set")
    onboarding = " ".join(sites["routes/onboarding_page.py"])
    for et in ("key_issued", "key_first_use"):
        assert f"'{et}'" in onboarding
    classified = set(_MCP_EVENT_TYPES) | set(_REST_EVENT_TYPES) | set(_NOT_USE_EVENT_TYPES)
    assert {"key_issued", "key_first_use"} <= classified
    assert 'f"bulk:{tier}"' in _read("routes/market_brief.py")


# ── the Python that turns rows into figures ─────────────────────────────────

def _row(minted, mcp=None, rest=None, log=None, meter=None, *, tracker_ok=False,
         mcp_any=None, rest_any=None, settled=None, not_use=0, unclassified=0,
         client="web-map"):
    return (client, minted, tracker_ok, mcp, rest, log, meter,
            mcp_any if mcp_any is not None else mcp,
            rest_any if rest_any is not None else rest,
            settled, not_use, unclassified)


T0 = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=_UTC)
H = datetime.timedelta(hours=1)


def test_key_record_grain_and_timing():
    r = _key_record(_row(T0, mcp=T0 + 2 * H))
    assert (r["grain"], r["hours"], r["days"], r["mcp"], r["rest"]) == ("call", 2.0, 0, True, False)
    # a meter day EARLIER than any timestamped use makes the first use day-grain
    r = _key_record(_row(T0, mcp=T0 + 72 * H, meter=datetime.date(2026, 9, 2)))
    assert (r["grain"], r["hours"], r["days"], r["rest"]) == ("day", None, 1, True)
    # the same UTC day as the timestamped use keeps the finer grain
    r = _key_record(_row(T0, rest=T0 + 3 * H, meter=datetime.date(2026, 9, 1)))
    assert (r["grain"], r["hours"]) == ("call", 3.0)
    # api_endpoint_log is REST at call grain
    r = _key_record(_row(T0, log=T0 + 5 * H, tracker_ok=True))
    assert (r["rest"], r["in_endpoint_log"], r["hours"]) == (True, True, 5.0)
    r = _key_record(_row(T0))
    assert (r["mcp"], r["rest"], r["grain"]) == (False, False, None)


def test_naive_timestamps_are_read_as_utc_not_raised_on():
    naive = T0.replace(tzinfo=None)
    r = _key_record(_row(naive, mcp=naive + 4 * H))
    assert r["hours"] == 4.0
    r = _key_record(_row(T0, mcp=naive + 4 * H))
    assert r["hours"] == 4.0


def test_days_are_utc_days_whatever_zone_the_driver_returns():
    """A session in another zone hands back aware datetimes in THAT zone. Days
    must be UTC days: 23:00 at UTC-5 is 04:00 UTC the next day."""
    est = datetime.timezone(datetime.timedelta(hours=-5))
    minted = datetime.datetime(2026, 9, 1, 23, 0, tzinfo=est)   # 09-02 04:00Z
    r = _key_record(_row(minted, mcp=minted + 2 * H))            # 09-02 06:00Z
    assert (r["hours"], r["days"]) == (2.0, 0)
    r = _key_record(_row(minted, meter=datetime.date(2026, 9, 2)))
    assert (r["grain"], r["days"]) == ("day", 0)


def test_probe_only_rows_are_not_use():
    r = _key_record(_row(T0, mcp_any=T0 + H, rest_any=T0 + H))
    assert (r["mcp"], r["rest"], r["mcp_any_row"], r["rest_any_row"]) == (False, False, True, True)


def test_window_summary_splits_channels_exactly():
    recs = [_key_record(x) for x in (
        _row(T0, mcp=T0 + H),                              # mcp only
        _row(T0, rest=T0 + 30 * H),                        # rest only, day 1
        _row(T0, mcp=T0 + 2 * H, rest=T0 + 3 * H),         # both
        _row(T0, meter=datetime.date(2026, 9, 9)),         # rest only, day grain, 8d
        _row(T0),                                          # never
    )]
    s = _window_summary(recs)
    assert (s["minted"], s["first_use_any"], s["mcp_only"], s["rest_only"],
            s["both"], s["never_used"]) == (5, 4, 1, 2, 1, 1)
    t = s["time_to_first_use"]
    assert t["keys"] == 4 and t["day_grain_only_keys"] == 1
    assert t["hours_call_grain"] == {"keys": 3, "median": 2.0, "max": 30.0}
    assert (t["days"]["same_day"], t["days"]["1_to_6"], t["days"]["7_to_29"],
            t["days"]["30_plus"]) == (2, 1, 1, 0)


def test_instrument_verdicts_are_derived():
    dch = dict(tracker_ok=False)
    known_rest = _key_record(_row(T0, rest=T0 + H, settled=T0 + H, **dch))
    fresh_rest = _key_record(_row(T0, rest=T0 + H, settled=None, **dch))
    seen = _key_record(_row(T0, log=T0 + H, tracker_ok=True))
    mcp = _key_record(_row(T0, mcp=T0 + H))
    # a REST request the tracker should hold and does not -> blind
    inst = _instrument([known_rest, mcp], [])
    assert inst["api_endpoint_log"]["verdict"] == "blind"
    assert inst["api_endpoint_log"]["keys_known_to_have_made_a_rest_request"] == 1
    assert inst["mcp_call_log.mcp"]["verdict"] == "live"
    # an unsettled row proves nothing yet
    assert _instrument([fresh_rest], [])["api_endpoint_log"]["verdict"] == "unproven"
    # rows for a DIFFERENT key never rescue a failed cross-check
    inst = _instrument([known_rest, seen], [])["api_endpoint_log"]
    assert (inst["verdict"], inst["keys_with_rows_here"]) == ("blind", 1)
    # the known key itself held -> live; one of two held -> partial
    held = _key_record(_row(T0, rest=T0 + H, settled=T0 + H, log=T0 + H))
    assert _instrument([held], [])["api_endpoint_log"]["verdict"] == "live"
    assert _instrument([held, known_rest], [])["api_endpoint_log"]["verdict"] == "partial"
    # nothing known: rows for some key make it a count, none make it unproven
    assert _instrument([seen], [])["api_endpoint_log"]["verdict"] == "live"
    assert _instrument([mcp], [])["api_endpoint_log"]["verdict"] == "unproven"
    assert _instrument([], [])["mcp_call_log.mcp"]["verdict"] == "unproven"
    # first_row_at: the EARLIEST row held for any checked key, None when none
    later = _key_record(_row(T0, log=T0 + 9 * H, tracker_ok=True))
    assert _instrument([later, held, mcp], [])["api_endpoint_log"]["first_row_at"] == (
        (T0 + H).isoformat())
    assert _instrument([mcp], [])["api_endpoint_log"]["first_row_at"] is None
    metered = [_key_record(_row(T0, meter=datetime.date(2026, 9, d))) for d in (5, 3)]
    assert _instrument(metered, [])["api_usage_meter"]["first_row_at"] == "2026-09-03"
