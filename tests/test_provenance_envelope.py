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
    CITE_AS,
    DCPI_CITE_TEMPLATE,
    FACILITY_CITE_TEMPLATE,
    LICENSE,
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
    )
    assert blk["source"].startswith("DC Hub facilities registry")
    assert blk["license"] == LICENSE == "CC-BY-4.0"
    assert blk["cite_as"] == CITE_AS == "DC Hub, dchub.cloud"
    assert blk["as_of"] == "2026-07-11T12:00:00"
    assert blk["verification_counts"] == {"verified": 4903, "tracked": 21938}
    assert blk["cite_url_template"] == "https://dchub.cloud/facilities/{slug}"


def test_block_minimal_omits_optional_keys():
    """Payload discipline: optional keys must be ABSENT (not null) when unset."""
    blk = provenance_block("src", "how")
    assert "as_of" not in blk
    assert "verification_counts" not in blk
    assert "cite_url_template" not in blk
    assert set(blk) == {"source", "method", "license", "cite_as"}


def test_block_accepts_string_as_of_and_coerces_count_values():
    blk = provenance_block("s", "m", as_of="2026-07-10",
                           counts={"verified": "4903", "tracked": 21938.0})
    assert blk["as_of"] == "2026-07-10"
    assert blk["verification_counts"] == {"verified": 4903, "tracked": 21938}


def test_block_never_raises_on_garbage():
    """Fail-soft contract: helper errors must never break a response."""
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")

    blk = provenance_block(Boom(), Boom(), as_of=Boom(), counts=Boom(),
                           cite_template=None)
    assert isinstance(blk, dict)
    assert blk["license"] == LICENSE
    assert blk["cite_as"] == CITE_AS


def test_block_drops_unusable_counts_not_the_block():
    blk = provenance_block("s", "m", counts={"verified": None})
    assert "verification_counts" not in blk
    assert blk["source"] == "s"


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
    attach_provenance(payload, "s1", "m1")
    assert payload["provenance"]["source"] == "s1"
    attach_provenance(payload, "s2", "m2")   # must NOT overwrite
    assert payload["provenance"]["source"] == "s1"


def test_attach_is_safe_on_non_dict():
    assert attach_provenance(None, "s", "m") is None
    assert attach_provenance([1, 2], "s", "m") == [1, 2]


# ─── facility_verification_counts (no DB → canonical floors) ─────────────

def test_facility_verification_counts_shape():
    counts = facility_verification_counts()
    # Without a DB this comes from canonical_stats fallback floors; either
    # way the contract is None OR {'verified': int, 'tracked': int} with
    # verified <= tracked (the fleet is a subset of the discovery pile).
    if counts is not None:
        assert set(counts) == {"verified", "tracked"}
        assert isinstance(counts["verified"], int)
        assert isinstance(counts["tracked"], int)
        assert 0 < counts["verified"] <= counts["tracked"]


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
    # Additive-only: the legacy citation fields must survive.
    assert body.get("source") == "https://dchub.cloud/interconnection-queues"
    assert "methodology" in body


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
