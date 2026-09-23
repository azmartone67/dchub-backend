"""tests/test_infra_projects_read.py — the PUBLIC read of the gas pipeline and
transmission PROJECT tables, GET /api/v1/infra-projects (2026-09-23), without
a database. The SQL itself runs against a real Postgres in
tests/test_infra_projects_upsert_sql.py::test_public_read_filters_summary_and_new_since.

What must hold, and why each can fail:

  * The path stays keyless. free_tier_gate.GATED_PREFIXES holds
    '/api/v1/transmission' as a PREFIX match, so naming this route
    '/api/v1/transmission-projects' would have put public-domain / ERCOT-§5
    facts behind the paid wall — and the MCP tool (a free citation hook, like
    get_power_pipeline over /api/v1/planned-generators) would 402 for everyone.
  * A type-specific filter narrows type=all to its own type, and against the
    other explicit type it is REPORTED in `ignored`, never silently dropped.
  * No caller text reaches the SQL string: every value is a bound parameter.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routes import infra_projects_ingest as ipi  # noqa: E402

PATH = "/api/v1/infra-projects"


def _opts(**q):
    opts, err = ipi.parse_read_args(q)
    assert err is None, err
    return opts


def test_the_route_is_served_on_get():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ipi.infra_projects_ingest_bp)
    rules = [r for r in app.url_map.iter_rules() if r.rule == PATH]
    assert len(rules) == 1 and "GET" in rules[0].methods


def test_the_path_is_not_behind_the_free_tier_gate():
    import free_tier_gate as ftg
    assert not ftg.is_gated(PATH)
    assert not ftg.is_gated(PATH + "?type=transmission")
    # the trap this path avoids — keep it true, or the pin above proves nothing
    assert ftg.is_gated("/api/v1/transmission-projects")


def test_type_defaults_to_both_and_accepts_aliases():
    assert _opts()["types"] == ["gas_pipeline", "transmission"]
    assert _opts(type="gas")["types"] == ["gas_pipeline"]
    assert _opts(type="TX")["types"] == ["transmission"]
    assert ipi.parse_read_args({"type": "oil"})[1]


def test_a_type_specific_filter_narrows_all_and_is_reported_elsewhere():
    assert _opts(min_capacity="500")["types"] == ["gas_pipeline"]
    assert _opts(min_kv="345")["types"] == ["transmission"]
    both = _opts(min_capacity="500", min_kv="345")
    assert both["types"] == ["gas_pipeline", "transmission"]
    o = _opts(type="transmission", min_capacity="500")
    assert o["types"] == ["transmission"] and "gas pipeline" in o["ignored"][0]
    o = _opts(type="gas_pipeline", min_kv="345")
    assert o["types"] == ["gas_pipeline"] and "transmission" in o["ignored"][0]
    assert _opts(type="gas_pipeline", min_capacity="1")["ignored"] == []


def test_bad_input_is_refused_not_guessed():
    for q in ({"state": "Texas"}, {"min_kv": "high"}, {"limit": "lots"},
              {"new_since": "last week"}, {"in_service_after": "2027-13-40"}):
        opts, err = ipi.parse_read_args(q)
        assert opts is None and err, q


def test_limit_is_clamped_and_delisted_is_opt_in():
    assert _opts()["limit"] == ipi._READ_LIMIT_DEFAULT
    assert _opts(limit="999999")["limit"] == ipi._READ_LIMIT_MAX
    assert _opts(limit="0")["limit"] == 1
    assert _opts()["include_delisted"] is False
    assert _opts(include_delisted="1")["include_delisted"] is True
    where, _ = ipi.build_where("gas_pipeline", _opts())
    assert "in_latest_release" in where
    where, _ = ipi.build_where("gas_pipeline", _opts(include_delisted="true"))
    assert "in_latest_release" not in where


def test_new_since_excludes_the_initial_load():
    where, params = ipi.build_where("transmission", _opts(new_since="2026-09-01"))
    assert "NOT in_initial_load" in where and "first_seen_at >= %s" in where
    assert str(params[-1]) == "2026-09-01"


def test_caller_text_never_reaches_the_sql_string():
    evil = "x'); DROP TABLE gas_pipeline_projects; --"
    o = _opts(status=evil, type="gas_pipeline")
    where, params = ipi.build_where("gas_pipeline", o)
    assert "DROP" not in where.upper()
    assert [evil.lower()] in params
