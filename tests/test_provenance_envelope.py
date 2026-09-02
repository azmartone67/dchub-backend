"""provenance-v1 (2026-07-11) — envelope helper + wired-endpoint shape tests.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO
DB / JWT_SECRET and must NEVER import main. Everything here runs against
routes/provenance.py (pure) and routes/interconnection_queues.py mounted on
a bare Flask app with NEON_URL forced to None (no network, no DB).

Run:  python3 -m pytest tests/test_provenance_envelope.py -v
"""
from __future__ import annotations

import datetime

import pytest

from routes.provenance import (
    ATTRIBUTION_COMPOSITE,
    CITE_AS,
    DCPI_CITE_TEMPLATE,
    DEFAULT_FALLBACK_URL,
    FACILITIES_FALLBACK_URL,
    FACILITY_CITE_TEMPLATE,
    LICENSE,
    LICENSE_COMPOSITE,
    MARKET_CITE_TEMPLATE,
    MARKETS_FALLBACK_URL,
    PROVENANCE_VERSION,
    attach_provenance,
    facility_verification_counts,
    provenance_block,
    queue_flag,
    verified_flag,
)


# ─── provenance_block ────────────────────────────────────────────────────

def test_block_full_shape():
    blk = provenance_block(
        source="DC Hub facilities registry (discovered_facilities)",
        method="multi-source discovery + dedup verification",
        as_of=datetime.datetime(2026, 7, 11, 12, 0, 0),
        counts={"verified": 4903, "tracked": 21938},
        cite_template=FACILITY_CITE_TEMPLATE,
        default_v="tracked",
    )
    assert blk["source"].startswith("DC Hub facilities registry")
    # ★2026-08-10 — this assertion used to read `== LICENSE == "CC-BY-4.0"`
    # and it ENCODED THE BUG. The facilities collection is a composite that
    # includes OpenStreetMap (ODbL 1.0, share-alike); ODbL does not permit
    # re-licensing derived data as CC-BY-4.0, so claiming CC-BY here asserted
    # a grant DC Hub does not hold. The test passed because it asserted what
    # the code did, not whether the code was allowed to do it.
    assert blk["license"] == LICENSE_COMPOSITE
    assert blk["license"] != LICENSE          # must NOT be the CC-BY grant
    assert blk["cite_as"] == CITE_AS == "DC Hub, dchub.cloud"
    assert blk["as_of"] == "2026-07-11T12:00:00"
    assert blk["verification_counts"] == {"verified": 4903, "tracked": 21938}
    assert blk["cite_url_template"] == "https://dchub.cloud/facilities/{slug}"
    # v1 lock — the three contract-hardening keys are always present.
    assert blk["provenance_version"] == PROVENANCE_VERSION == 1
    assert blk["default_v"] == "tracked"
    assert blk["fallback_url"] == "https://dchub.cloud/facilities/directory"


# ─── licence coherence (★2026-08-10) ─────────────────────────────────────
# We grant CC-BY-4.0 over what DC Hub COMPUTED (DCPI scores, methodology,
# our grid analysis) and NOT over the composite facility corpus, which
# carries OpenStreetMap's ODbL share-alike and PeeringDB's attribution
# requirement. These guards exist because the previous test asserted the
# wrong value for a year and nothing noticed.

def test_facilities_layer_is_not_relicensed_as_ccby():
    """ODbL 1.0 is share-alike: derived data may NOT be re-licensed CC-BY.
    Any collection citing /facilities/ must therefore not claim CC-BY."""
    blk = provenance_block("facilities", "m",
                           cite_template=FACILITY_CITE_TEMPLATE,
                           default_v="tracked")
    assert "CC-BY" not in blk["license"], (
        "facility corpus contains ODbL data — CC-BY is not ours to grant")
    assert blk["license"] == LICENSE_COMPOSITE


def test_facilities_layer_emits_the_attribution_odbl_requires():
    """ODbL §4.3 and PeeringDB both require attribution. An API consumer
    who never loads a web page must still receive it."""
    blk = provenance_block("facilities", "m",
                           cite_template=FACILITY_CITE_TEMPLATE,
                           default_v="tracked")
    attr = blk.get("attribution", "")
    assert attr == ATTRIBUTION_COMPOSITE
    # Naming the licence is the part that discharges the obligation —
    # "contains third-party data" would not.
    assert "OpenStreetMap" in attr
    assert "ODbL" in attr
    assert "PeeringDB" in attr


@pytest.mark.parametrize("tmpl", [DCPI_CITE_TEMPLATE, MARKET_CITE_TEMPLATE, None])
def test_our_own_computed_work_keeps_the_ccby_grant(tmpl):
    """DCPI scores and market analysis are DC Hub's derived work, reproducible
    from public inputs. They stay CC-BY-4.0 — that grant is the whole point of
    publishing an index, and narrowing it would be a regression, not caution."""
    blk = provenance_block("DCPI", "m", cite_template=tmpl, default_v="inferred")
    assert blk["license"] == LICENSE == "CC-BY-4.0"
    # No attribution field: there is no upstream obligation on our own scores.
    assert "attribution" not in blk


def test_licence_selection_is_driven_by_the_template_not_the_source_string():
    """Guard against a tempting-but-wrong implementation: keying off the
    human-readable `source` text would silently mislabel any route that
    words its source differently."""
    # Says "facilities" in the source, but is NOT the facilities collection.
    blk = provenance_block("count of facilities per market", "m",
                           cite_template=MARKET_CITE_TEMPLATE,
                           default_v="inferred")
    assert blk["license"] == LICENSE == "CC-BY-4.0"
    # Says nothing about facilities, but IS the facilities collection.
    blk2 = provenance_block("registry", "m",
                            cite_template=FACILITY_CITE_TEMPLATE,
                            default_v="tracked")
    assert blk2["license"] == LICENSE_COMPOSITE


def test_block_minimal_omits_optional_keys():
    """Payload discipline: optional keys must be ABSENT (not null) when unset.
    v1 lock: provenance_version, default_v and fallback_url are NOT optional —
    they are always present."""
    blk = provenance_block("src", "how", default_v="tracked")
    assert "as_of" not in blk
    assert "verification_counts" not in blk
    assert "cite_url_template" not in blk
    assert set(blk) == {"provenance_version", "source", "method", "default_v",
                        "fallback_url", "license", "cite_as"}


def test_block_accepts_string_as_of_and_coerces_count_values():
    blk = provenance_block("s", "m", as_of="2026-07-10",
                           counts={"verified": "4903", "tracked": 21938.0},
                           default_v="tracked")
    assert blk["as_of"] == "2026-07-10"
    assert blk["verification_counts"] == {"verified": 4903, "tracked": 21938}


def test_block_never_raises_on_garbage():
    """Fail-soft contract: helper errors must never break a response."""
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")

    blk = provenance_block(Boom(), Boom(), as_of=Boom(), counts=Boom(),
                           cite_template=None, default_v="tracked")
    assert isinstance(blk, dict)
    assert blk["license"] == LICENSE
    assert blk["cite_as"] == CITE_AS
    # v1 lock survives even the degraded minimal block.
    assert blk["provenance_version"] == 1
    assert blk["fallback_url"] == DEFAULT_FALLBACK_URL
    assert blk["default_v"] == "tracked"


def test_block_drops_unusable_counts_not_the_block():
    blk = provenance_block("s", "m", counts={"verified": None},
                           default_v="tracked")
    assert "verification_counts" not in blk
    assert blk["source"] == "s"


# ─── v1 lock: provenance_version / fallback_url / default_v ──────────────

def test_v1_version_always_present_and_is_1():
    assert provenance_block("s", "m", default_v="inferred")[
        "provenance_version"] == 1


def test_v1_default_v_is_required_keyword():
    """Every wiring site MUST declare default_v explicitly — omitting it is
    a TypeError at the call site (caught by each site's fail-soft wrapper,
    but red in tests/review so future wirings can't forget it)."""
    with pytest.raises(TypeError):
        provenance_block("s", "m")          # no default_v
    with pytest.raises(TypeError):
        attach_provenance({}, "s", "m")     # no default_v
    with pytest.raises(TypeError):
        # positional smuggling must not work either (keyword-only)
        provenance_block("s", "m", None, None, None, None, "tracked")


def test_v1_fallback_url_always_present_and_surface_specific():
    # facilities template → facilities directory
    blk = provenance_block("s", "m", cite_template=FACILITY_CITE_TEMPLATE,
                           default_v="tracked")
    assert blk["fallback_url"] == FACILITIES_FALLBACK_URL \
        == "https://dchub.cloud/facilities/directory"
    # markets + DCPI templates → markets directory
    for tpl in (MARKET_CITE_TEMPLATE, DCPI_CITE_TEMPLATE):
        blk = provenance_block("s", "m", cite_template=tpl,
                               default_v="inferred")
        assert blk["fallback_url"] == MARKETS_FALLBACK_URL \
            == "https://dchub.cloud/markets/directory"
    # no template → site root (still ALWAYS present)
    blk = provenance_block("s", "m", default_v="published")
    assert "cite_url_template" not in blk
    assert blk["fallback_url"] == DEFAULT_FALLBACK_URL == "https://dchub.cloud"


def test_v1_fallback_url_explicit_override_wins():
    blk = provenance_block("s", "m", cite_template=DCPI_CITE_TEMPLATE,
                           fallback_url=FACILITIES_FALLBACK_URL,
                           default_v="inferred")
    assert blk["fallback_url"] == FACILITIES_FALLBACK_URL


def test_v1_default_v_carried_verbatim():
    for tier in ("verified", "tracked", "published", "inferred"):
        assert provenance_block("s", "m", default_v=tier)["default_v"] == tier


# ─── verified_flag (facilities: canonical fleet filter) ──────────────────

def test_verified_flag_fleet_filter_semantics():
    # COALESCE(is_duplicate,0)=0 → verified (0, None, False all pass)
    assert verified_flag({"is_duplicate": 0}) == "verified"
    assert verified_flag({"is_duplicate": None}) == "verified"
    assert verified_flag({"is_duplicate": False}) == "verified"
    # flagged duplicate → tracked
    assert verified_flag({"is_duplicate": 1}) == "tracked"
    assert verified_flag({"is_duplicate": True}) == "tracked"


def test_verified_flag_conservative_default_when_column_absent():
    """Legacy `facilities` rows carry no is_duplicate — floors never
    over-claim, so the default is 'tracked'."""
    assert verified_flag({"name": "Ashburn DC-1"}) == "tracked"
    assert verified_flag(None) == "tracked"
    assert verified_flag("not-a-dict") == "tracked"
    assert verified_flag({}, default="verified") == "verified"


# ─── queue_flag (published vs inferred) ──────────────────────────────────

def test_queue_flag():
    assert queue_flag(True) == "published"
    assert queue_flag(False) == "inferred"


# ─── attach_provenance ───────────────────────────────────────────────────

def test_attach_stamps_once_and_never_overwrites():
    payload = {"ok": True}
    attach_provenance(payload, "s1", "m1", default_v="tracked")
    assert payload["provenance"]["source"] == "s1"
    attach_provenance(payload, "s2", "m2", default_v="inferred")  # must NOT overwrite
    assert payload["provenance"]["source"] == "s1"
    assert payload["provenance"]["default_v"] == "tracked"


def test_attach_is_safe_on_non_dict():
    assert attach_provenance(None, "s", "m", default_v="tracked") is None
    assert attach_provenance([1, 2], "s", "m", default_v="tracked") == [1, 2]


def test_attach_passes_v1_keys_through():
    payload = {}
    attach_provenance(payload, "s", "m", cite_template=FACILITY_CITE_TEMPLATE,
                      default_v="tracked")
    prov = payload["provenance"]
    assert prov["provenance_version"] == 1
    assert prov["default_v"] == "tracked"
    assert prov["fallback_url"] == FACILITIES_FALLBACK_URL


# ─── facility_verification_counts (no DB → canonical floors) ─────────────

def test_facility_verification_counts_omits_without_a_measurement():
    """★2026-09-01: this used to read "without a DB this comes from
    canonical_stats fallback floors" and guard the shape only `if counts is
    not None`. Once the helper stopped publishing those floors as counts, that
    branch became unreachable in CI (no DB => always None) — a test that could
    no longer fail, still reporting green. Assert the contract that actually
    holds here instead: no measurement, no field.

    The measured shape (verified <= tracked, both ints) is exercised against
    driven module state in tests/test_provenance_counts_are_measured.py, which
    is the only place that CAN reach it without a database."""
    assert facility_verification_counts() is None


# ─── wired endpoint shape: interconnection-queue snapshot (no DB) ────────

@pytest.fixture()
def queue_client(monkeypatch):
    import routes.interconnection_queues as iq
    monkeypatch.setattr(iq, "NEON_URL", None)   # force the no-DB path
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(iq.interconnection_queues_bp)
    with app.test_client() as client:
        yield client


def test_queue_snapshot_carries_collection_provenance(queue_client):
    r = queue_client.get("/api/v1/interconnection-queue/snapshot")
    assert r.status_code == 200
    body = r.get_json()
    prov = body.get("provenance")
    assert isinstance(prov, dict)
    assert prov["license"] == "CC-BY-4.0"
    assert prov["cite_as"] == "DC Hub, dchub.cloud"
    assert "published" in prov["method"]          # names the v convention
    assert "interconnection queues" in prov["source"]
    # v1 lock: version + fallback_url + per-surface default_v. Snapshot rows
    # are pure ISO published disclosures → default_v = published; no
    # cite_url_template on this surface → fallback is the site root.
    assert prov["provenance_version"] == 1
    assert prov["default_v"] == "published"
    assert prov["fallback_url"] == "https://dchub.cloud"
    # Additive-only: the legacy citation fields must survive.
    assert body.get("source") == "https://dchub.cloud/interconnection-queues"
    assert "methodology" in body


def test_queue_snapshot_provenance_default_v_required_and_per_surface():
    """The shared queue block builder inherits the v1 lock: default_v is a
    required keyword (snapshot/by-iso pass 'published'; refined passes
    'inferred' because its rows carry DC Hub enrichments)."""
    from routes.interconnection_queues import _snapshot_provenance
    with pytest.raises(TypeError):
        _snapshot_provenance()                       # no default_v
    assert _snapshot_provenance(default_v="published")["default_v"] == "published"
    assert _snapshot_provenance(default_v="inferred")["default_v"] == "inferred"
    assert _snapshot_provenance(default_v="published")["provenance_version"] == 1


def test_queue_serialize_rows_flag_published():
    """Per-record flag: iso_queue_snapshots rows are the ISO's own published
    disclosures → v='published' (ONE compact field, no per-row URLs)."""
    from routes.interconnection_queues import _serialize
    row = {
        "iso": "ERCOT",
        "as_of": datetime.date(2026, 7, 1),
        "queued_load_total_gw": 150.0,
        "queued_load_data_center_gw": None,
        "queued_load_dc_share_pct": None,
        "new_applications_q_gw": None,
        "new_applications_period": None,
        "historical_completion_pct": None,
        "top_subregions": [],
        "source_url": "https://www.ercot.com/gis",
        "source_name": "ERCOT GIS Report",
    }
    out = _serialize(row)
    assert out["v"] == "published"
    assert out["source_name"] == "ERCOT GIS Report"   # additive, nothing lost
    # No per-row cite URLs / license (collection-level only — bytes).
    assert "cite_url" not in out and "license" not in out


def test_refined_queue_no_db_returns_clean_error(queue_client):
    """Fail-soft: the no-DB path stays the pre-envelope 503 contract."""
    r = queue_client.get("/api/v1/interconnection-queue/refined")
    assert r.status_code == 503
    assert r.get_json()["error"] == "no_db"


# ─── wired endpoint shape: DCPI cite template constant ───────────────────

def test_dcpi_cite_template_is_collection_level_constant():
    assert DCPI_CITE_TEMPLATE == "https://dchub.cloud/dcpi/{market_slug}"
