"""
Rendered-body tests for the /vs comparison pages (routes/competitive_seo.py).

Why these exist (2026-08-02): the /vs pages carried a hardcoded
"mcp_tools": 40 while canon said 82, and rendered the raw-row facility
floor — for weeks, because the module was outside AGENT_CODE_SURFACES and
its counts are interpolated (invisible to the fence's literal scan). The
fence now scans the source (tests/test_canonical_counts_drift.py); these
tests cover the OUTPUT side: the rendered body must carry the PINNED canon
values and none of the retired ones, and the "<brand> alternative" title
treatment must survive.

House rules: pytest functions only — no module-scope work beyond imports;
never import main.py. The blueprint mounts on a bare Flask app with the
radar probe cache forced cold (no network) and canonical_stats forced to
its fallback path (no DB).
"""
import pytest


@pytest.fixture()
def vs_client(monkeypatch):
    import canonical_stats
    import routes.competitive_seo as cs

    # No DB in CI: force the canonical_stats fallback path so PINNED wins.
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda force=False: {})
    monkeypatch.setattr(canonical_stats, "facilities_verified_phrase",
                        lambda: "")

    # No network: radar probe cache cold, background warm disabled.
    monkeypatch.setattr(cs, "_ci_read_cached", None)
    monkeypatch.setattr(cs, "_ci_kick_refresh", None)

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(cs.competitive_seo_bp)
    with app.test_client() as client:
        yield client


def _body(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.get_data(as_text=True)


def _title(body):
    assert "<title>" in body and "</title>" in body
    return body.split("<title>", 1)[1].split("</title>", 1)[0]


def test_datacenterhawk_title_and_h1_target_alternative(vs_client):
    body = _body(vs_client, "/vs/datacenterhawk")
    title = _title(body)
    # The measured SERP query is "datacenterhawk alternative" — both tokens
    # must be in the <title>, and "alternative" must be in the H1.
    assert "datacenterHawk" in title
    assert "Alternative" in title
    assert '<span class="vs">alternative</span>' in body
    assert "The datacenterHawk" in body


def test_dcbyte_and_dcd_get_the_same_title_treatment(vs_client):
    for slug, brand in (("dc-byte", "DC Byte"),
                        ("datacenterdynamics", "DatacenterDynamics")):
        title = _title(_body(vs_client, f"/vs/{slug}"))
        assert "Alternative" in title, f"/vs/{slug} title lost 'Alternative'"
        assert brand in title, f"/vs/{slug} title lost the brand token"


def test_counts_are_canon_bound_not_hardcoded(vs_client):
    from ai_surface_canon import PINNED
    body = _body(vs_client, "/vs/datacenterhawk")
    assert f"{PINNED['tools_advertised']}+" in body
    assert PINNED["public"]["facilities"] in body
    # The exact drifted values this change retires. "40+ tools" shipped for
    # weeks while canon said 82; the 2x,000+ floors are the raw-row
    # over-claim path (on the canon stale_markers denylist).
    assert "40+ tools" not in body
    for stale in ("21,000+", "22,000+", "23,000+", "12,650+"):
        assert stale not in body, f"retired floor {stale} re-appeared"


def test_sp_acquisition_section_only_on_datacenterhawk(vs_client):
    hawk = _body(vs_client, "/vs/datacenterhawk")
    assert 'id="sp-global-acquisition"' in hawk
    assert "S&amp;P Global" in hawk
    # Neutral-tone guard: the section states facts, never disparagement.
    for banned in ("uncertainty", "risky", "abandon", "worse"):
        assert banned not in hawk.lower().split("sp-global-acquisition")[1] \
            .split("<div class=\"cta\">")[0], \
            f"S&P section drifted into non-neutral language: {banned!r}"
    baxtel = _body(vs_client, "/vs/baxtel")
    assert "sp-global-acquisition" not in baxtel


def test_recon_matrix_renders_measured_axes(vs_client):
    body = _body(vs_client, "/vs/datacenterhawk")
    # The three axes the comparison rests on, straight from
    # routes/competitor_recon.AXES — if the soft import breaks, the table
    # silently vanishes and this catches it.
    for label in ("AI-agent access (MCP / robots / llms.txt)",
                  "Live grid telemetry",
                  "Public pricing"):
        assert label in body, f"recon matrix axis missing: {label!r}"
    # Self-declared gaps must render too (honesty guard: the matrix is only
    # defensible while it shows DC Hub's own weak axes).
    assert "self-declared" in body


def test_alias_301_consolidates_link_equity(vs_client):
    r = vs_client.get("/vs/dchawk")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/vs/datacenterhawk")


def test_jsonld_points_at_the_real_domain(vs_client):
    body = _body(vs_client, "/vs/datacenterhawk")
    assert "datacenterhawk.com" in body
    # Any remaining standalone dchawk.com is the dead domain the JSON-LD
    # about.url used to point agents at.
    assert "dchawk.com" not in body.replace("datacenterhawk.com", "")
