"""tests/test_infra_projects_ingest.py — the gas-pipeline and transmission
PROJECT lanes (2026-09-22), without a database or the network.

What must hold, and why each can fail:

  * Keys are stable and the payload is de-duplicated. ERCOT's TPIT repeats five
    project numbers verbatim (measured 2026-09-22); two rows with one key in a
    single INSERT … ON CONFLICT DO UPDATE is a Postgres error, so the whole
    weekly run would 500.
  * A cost RANGE ('13000-17000') is never collapsed to one end — the numeric
    column stays null and the raw text is kept.
  * TPIT's sheet outranks its status cell (a Completed-sheet row whose cell
    still says 'Planned' is Completed).
  * The upsert never rewrites first_seen_at or in_initial_load on conflict —
    that is the whole basis of the honest "+N new" on /whats-new.
  * The parsers match headers by NAME, find the release date where EIA puts it
    (column B), keep the project hyperlink, and pick the exact TPIT link, not
    the "Archived …" zip that shares its words.
  * The endpoint refuses without the admin header and refuses a payload below
    the floor without touching the database.

The DB half (first_seen_at surviving a second release, delisting, status
history, the board's count) is tests/test_infra_projects_upsert_sql.py.
"""
import ast
import datetime as dt
import gzip
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

STRONG = "9f3c2a7e5b1d4c8a0e6f2b9d7c3a1e5f8b4d0c6a2e9f7b3d1c5a8e0f4b2d6c9a"


@pytest.fixture
def ipi():
    from routes import infra_projects_ingest as m
    return m


@pytest.fixture
def fetch():
    import infra_fetch as f
    return f


# ── keys + normalisation ───────────────────────────────────────────────────
def test_gas_key_is_the_normalised_name_and_duplicates_are_dropped(ipi):
    rows, dups = ipi.normalize_gas_rows([
        {"project_name": "Algonquin Project Beacon", "status": "Announced"},
        {"project_name": "algonquin  project-beacon", "status": "Applied"},
        {"project_name": "   "},
        {"project_name": "Transco SESE", "status": "Construction"},
    ])
    assert [r["project_key"] for r in rows] == ["algonquin-project-beacon", "transco-sese"]
    assert dups == 1
    assert rows[0]["status"] == "Announced", "first occurrence must win"
    assert rows[0]["source_url"] == ipi.GAS_SOURCE_URL
    assert rows[0]["license"] == ipi.GAS_LICENSE


def test_a_cost_range_is_kept_as_text_and_never_collapsed_to_a_number(ipi):
    (r,), _ = ipi.normalize_gas_rows([{"project_name": "Alaska LNG",
                                       "cost_musd": "13000-17000", "miles": "807",
                                       "in_service_year": "2029",
                                       "source_row_updated": "2026-07-05T00:00:00",
                                       "project_url": "javascript:alert(1)"}])
    assert r["cost_musd"] is None and r["cost_musd_text"] == "13000-17000"
    assert r["miles"] == 807.0 and r["in_service_year"] == 2029
    assert r["source_row_updated"] == dt.date(2026, 7, 5)
    assert r["project_url"] is None, "only http(s) links may be stored"


def test_tpit_duplicate_numbers_are_dropped_and_excel_floats_are_keyed_as_ints(ipi):
    rows, dups = ipi.normalize_tx_rows([
        {"project_number": 110733.0, "title": "A", "source_list": "future",
         "source_status": "Planned"},
        {"project_number": "110733", "title": "A again", "source_list": "future"},
        {"project_number": None, "title": "no key"},
    ])
    assert [r["project_key"] for r in rows] == ["110733"]
    assert dups == 1


@pytest.mark.parametrize("lst,cell,want", [
    ("completed", "Planned", "Completed"),
    ("cancelled", "Conceptual", "Cancelled"),
    ("future", "Under Construction", "Under Construction"),
    ("planned", "None", None),
])
def test_tpit_sheet_outranks_the_status_cell(ipi, lst, cell, want):
    (r,), _ = ipi.normalize_tx_rows([{"project_number": 1, "source_list": lst,
                                      "source_status": cell}])
    assert r["status"] == want
    assert r["source_list"] == lst


def test_every_declared_field_is_produced_by_its_normaliser(ipi):
    """A field in _*_FIELDS that the normaliser never sets would KeyError on
    the first real run — after deploy, on the runner, weekly."""
    g, _ = ipi.normalize_gas_rows([{"project_name": "x"}])
    t, _ = ipi.normalize_tx_rows([{"project_number": 1}])
    assert set(ipi._GAS_FIELDS) == set(g[0])
    assert set(ipi._TX_FIELDS) == set(t[0])


# ── the upsert statement ───────────────────────────────────────────────────
@pytest.mark.parametrize("table,fields", [("gas_pipeline_projects", "_GAS_FIELDS"),
                                          ("transmission_projects", "_TX_FIELDS")])
def test_upsert_never_rewrites_identity_on_conflict(ipi, table, fields):
    sql = ipi.upsert_sql(table, getattr(ipi, fields))
    head, _, on_conflict = sql.partition("ON CONFLICT")
    assert "first_seen_at" in head and "in_initial_load" in head, (
        "INSERT must write the identity columns")
    for col in ("first_seen_at", "in_initial_load", "project_key"):
        assert f"{col} =" not in on_conflict, (
            f"ON CONFLICT rewrites {col} — every release would re-date old "
            f"projects as new, or re-key them")
    assert "(source, project_key)" in on_conflict
    assert "RETURNING (xmax = 0)" in sql


# ── parsers (duck-typed workbooks: CI's unit job has no openpyxl) ──────────
class _Link:
    def __init__(self, target):
        self.target = target


class _Cell:
    def __init__(self, value, link=None):
        self.value = value
        self.hyperlink = _Link(link) if link else None


class _Sheet:
    def __init__(self, title, rows):
        self.title, self._rows = title, rows

    def iter_rows(self, values_only=False):
        for r in self._rows:
            yield tuple(c.value for c in r) if values_only else tuple(r)


class _Book:
    def __init__(self, sheets):
        self.worksheets = sheets
        self.sheetnames = [s.title for s in sheets]

    def __getitem__(self, k):
        return next(s for s in self.worksheets if s.title == k)


def _cells(*vals):
    return [v if isinstance(v, _Cell) else _Cell(v) for v in vals]


def test_gas_parser_reads_by_header_name_release_from_column_b_and_the_link(fetch):
    contents = _Sheet("Contents", [_cells(None, "Workbook Contents", None),
                                   _cells(None, "Release Date:", dt.datetime(2026, 8, 4))])
    # Columns deliberately NOT in EIA's order: a positional parser would
    # read the operator as the name.
    active = _Sheet(fetch.GAS_PROJECTS_SHEET, [
        _cells("Natural Gas Pipeline Projects"),
        _cells("Pipeline Operator Name", "Project Name", "Status",
               "Additional Capacity (MMcf/d)", "Last Updated Date", "Website"),
        _cells("Algonquin Gas Transmission LLC", "Project Beacon", "Announced", 300,
               dt.datetime(2026, 7, 6), _Cell("Project website", "https://example.com/b")),
        _cells(None, None, None, None, None, None),
    ])
    rows = fetch.parse_gas_projects_workbook(_Book([contents, active]))
    assert len(rows) == 1
    r = rows[0]
    assert r["project_name"] == "Project Beacon"
    assert r["operator"] == "Algonquin Gas Transmission LLC"
    assert r["capacity_mmcfd"] == 300
    assert r["source_row_updated"] == "2026-07-06T00:00:00"
    assert r["project_url"] == "https://example.com/b"
    assert r["source_release"] == "2026-08-04"
    assert r["miles"] is None, "a missing header must read null, not a neighbour"
    json.dumps(rows)


def test_tpit_parser_reads_list_sheets_only_and_the_as_of_date(fetch):
    hdr = _cells("ERCOT Project Number", "Project Title (text, please start with…)",
                 "TSP/Company Contact", "Transmission Status \"under construction\"",
                 "Transmission Owner (text)", "Transmission Owner Project Number (Optional)",
                 "Projected In-Service Date (Month/Yr)", "Service Level kV")
    banner = _cells("JULY BASE … (TPIT) FUTURE PROJECTS AS OF 7/13/2026")
    row = _cells(81354, "GSEC_Snodgrass", "Jane Doe jane@example.com", "Planned", "GSEC",
                 "X-9", dt.datetime(2027, 1, 1), 138)
    book = _Book([
        _Sheet("ImprovementCostSummary07172026", [_cells("x")]),
        _Sheet("FutureTPIT071326NoCost", [banner, hdr, row]),
        _Sheet("CompletedTPIT071326NoCost", [banner, hdr, _cells(99746, "Done", None,
                                                                   "Planned", "TNMP")]),
        _Sheet("RTPProjects", [_cells("RTP")]),
    ])
    rows = fetch.parse_tpit_workbook(book, "https://www.ercot.com/files/x.xlsx")
    assert [(r["project_number"], r["source_list"]) for r in rows] == [
        (81354, "future"), (99746, "completed")]
    r = rows[0]
    assert r["owner"] == "GSEC", "must not read 'Transmission Owner Project Number'"
    assert r["kv"] == 138 and r["projected_isd"] == "2027-01-01T00:00:00"
    assert r["source_release"] == "2026-07-13"
    assert not any("jane" in str(v).lower() for v in r.values()), (
        "the TSP contact column (names, e-mails) must never be read")


def test_tpit_link_is_the_exact_title_not_the_archive(fetch):
    html = ('<a href="https://www.ercot.com/files/docs/2021/10/22/Archived-Transmission-'
            'Project-and-Information-Tracking.zip" title="Archived Transmission Project and '
            'Information Tracking">A</a>'
            '<a href="/files/docs/2022/03/02/ERCOT-July-TPIT-No-Cost-071326.xlsx" '
            'class="d-block" title="Transmission Project and Information Tracking">T</a>')
    assert fetch.find_tpit_url(html) == (
        "https://www.ercot.com/files/docs/2022/03/02/ERCOT-July-TPIT-No-Cost-071326.xlsx")
    assert fetch.find_tpit_url("<a href='x.xlsx'>nothing</a>") is None


def test_project_layers_are_registered_and_quiet_weeks_beat_no_new_data(fetch):
    for layer, feed in (("gas-pipeline-projects", "gas-pipeline-projects-ingest"),
                        ("transmission-projects", "transmission-projects-ingest")):
        assert fetch.LAYERS[layer]["zero_is_quiet"] is True
        assert fetch._BOARD_FEED[layer] == feed
        assert os.path.exists(os.path.join(ROOT, ".github", "workflows", feed + ".yml"))
    src = open(os.path.join(ROOT, "tools", "infra_fetch.py"), encoding="utf-8").read()
    assert 'status="no_new_data" if quiet else "success"' in src


# ── the HTTP gate ──────────────────────────────────────────────────────────
@pytest.fixture
def client(ipi, monkeypatch):
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    calls = []

    def _no_db(*a, **k):
        calls.append(a)
        return 200, {"ok": True, "inserted": 0}
    monkeypatch.setattr(ipi, "_write", _no_db)
    app = Flask(__name__)
    app.register_blueprint(ipi.infra_projects_ingest_bp)
    c = app.test_client()
    c.calls = calls
    return c


def _post(client, path, rows, key=STRONG, qs=""):
    body = gzip.compress(json.dumps({"rows": rows}).encode())
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
    if key:
        headers["X-Admin-Key"] = key
    return client.post(path + qs, data=body, headers=headers)


GAS = "/api/v1/admin/ingest/gas-pipeline-projects"
TX = "/api/v1/admin/ingest/transmission-projects"


@pytest.mark.parametrize("key", [None, "wrong-" + STRONG[6:]])
def test_ingest_refuses_without_the_admin_header(client, key):
    r = _post(client, GAS, [{"project_name": f"p{i}"} for i in range(50)], key=key)
    assert r.status_code == 401 and client.calls == []


def test_query_string_key_is_not_accepted(client):
    r = _post(client, GAS, [{"project_name": "p"}], key=None, qs=f"?admin_key={STRONG}")
    assert r.status_code == 401


def test_a_payload_below_the_floor_never_reaches_the_database(client, ipi):
    r = _post(client, TX, [{"project_number": i} for i in range(ipi._MIN_ROWS["ercot_tpit"] - 1)])
    assert r.status_code == 400 and client.calls == []
    assert "floor" in r.get_json()["error"]


def test_a_payload_at_the_floor_is_written(client, ipi):
    n = ipi._MIN_ROWS["eia_ng_pipeline_projects"]
    r = _post(client, GAS, [{"project_name": f"p{i}"} for i in range(n)])
    assert r.status_code == 200, r.get_json()
    assert len(client.calls) == 1
    assert len(client.calls[0][5]) == n


def test_dry_run_writes_nothing(client):
    r = _post(client, GAS, [{"project_name": "p"}], qs="?dry_run=1")
    assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert client.calls == []


# ── the board ──────────────────────────────────────────────────────────────
def _growth_src():
    return open(os.path.join(ROOT, "routes", "infra_growth.py"), encoding="utf-8").read()


def _dict_literal(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def test_project_layers_are_on_the_board_with_first_seen_counting():
    tree = ast.parse(_growth_src())
    layers = {row[0]: row for row in _dict_literal(tree, "_LAYERS")}
    fresh = _dict_literal(tree, "_FRESH_COL")
    first = _dict_literal(tree, "_FIRST_SEEN")
    for label in ("gas_pipeline_projects", "transmission_projects"):
        assert layers[label][1] == label
        assert fresh[label] == "last_seen_at", (
            "freshness must be the per-run stamp; first_seen_at would read a live "
            "weekly loader as dead after its first quiet quarter")
        assert first[label] == ("first_seen_at", "in_initial_load")
        for d in ("_EXPECTED_CADENCE", "_FRIENDLY", "_PROVENANCE"):
            assert label in _dict_literal(tree, d), f"{label} missing from {d}"


def _status_fn():
    tree = ast.parse(_growth_src())
    ns = {}
    for node in tree.body:
        if ((isinstance(node, ast.FunctionDef) and node.name == "_layer_status")
                or (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "_RELOAD_FRESH_DAYS"
                            for t in node.targets))):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<s>", "exec"), ns, ns)
    return ns["_layer_status"]


def test_first_seen_wording_never_claims_the_count_did_not_move():
    f = _status_fn()
    st, why = f(0, 7, 0, 21, "quarterly", first_seen=True)
    assert st == "refreshed"
    assert "initial load is never counted" in why and "count did not move" not in why
    st, why = f(3, 7, 0, 21, "quarterly", first_seen=True)
    assert st == "growing" and why.startswith("+3 first seen")
    st, why = f(None, None, 0, 21, "quarterly", first_seen=True)
    assert st == "measuring" and "never read as zero" in why
    # The default path is unchanged for every other layer.
    assert f(0, 7, 0, 21, "quarterly")[1].startswith("table written 0d ago")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
