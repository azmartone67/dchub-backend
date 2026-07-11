"""Brazil ONS balanço parser — global grid expansion (2026-07-11).

Locks in the shape we extract from tr.ons.org.br GetBalancoEnergetico:
itaipu-as-hydro, night-time negative-solar clamping, the wind+solar+hydro
renewable definition, staleness + sanity refusal. Pure-function tests —
no network, no DB, and (per the green-main rule) never imports main.
"""
from datetime import datetime, timedelta, timezone

from routes.iso_br_ons import _parse_balanco, _GEN_SANE_MW

_BRT = timezone(timedelta(hours=-3))


def _fresh_stamp():
    return datetime.now(_BRT).replace(microsecond=0).isoformat()


def _payload(stamp=None):
    """Trimmed real payload shape (values from the 2026-07-11 live fetch)."""
    return {
        "Data": stamp or _fresh_stamp(),
        "sudesteECentroOeste": {
            "geracao": {"total": 31900.0, "hidraulica": 12826.4, "termica": 6658.4,
                        "eolica": 37.4, "nuclear": 2009.6, "solar": 6004.7,
                        "itaipu50HzBrasil": 0.0, "itaipu60Hz": 4363.6},
            "cargaVerificada": 37202.7, "importacao": 7741.4, "exportacao": 2438.7,
        },
        "sul": {
            "geracao": {"total": 10180.8, "hidraulica": 7652.9, "termica": 1523.0,
                        "eolica": 738.1, "nuclear": 0.0, "solar": 266.9},
            "cargaVerificada": 11631.4, "importacao": 2438.7, "exportacao": 988.2,
        },
        "nordeste": {
            "geracao": {"total": 22994.8, "hidraulica": 1959.6, "termica": 280.9,
                        "eolica": 12398.0, "nuclear": 0.0, "solar": 8356.4},
            "cargaVerificada": 12334.7, "importacao": 0.0, "exportacao": 10658.8,
        },
        "norte": {
            "geracao": {"total": 5007.2, "hidraulica": 2095.3, "termica": 1820.8,
                        "eolica": 363.6, "nuclear": 0.0, "solar": 727.6},
            "cargaVerificada": 7924.6, "importacao": 2917.4, "exportacao": 0.0,
        },
        "internacional": {"argentina": 988.5, "paraguai": 0.0, "uruguai": -0.36},
    }


def test_parses_all_four_subsystems():
    out = _parse_balanco(_payload())
    assert out and not out["stale"]
    assert sorted(out["subsystems"]) == ["BR_NORDESTE", "BR_NORTE", "BR_SECO", "BR_SUL"]


def test_itaipu_counts_as_hydro_in_seco():
    out = _parse_balanco(_payload())
    seco = out["subsystems"]["BR_SECO"]
    # hidraulica 12826.4 + itaipu50Hz 0 + itaipu60Hz 4363.6
    assert abs(seco["fuel_hydro_mw"] - 17190.0) < 0.5


def test_sin_aggregate_and_renewable_definition():
    out = _parse_balanco(_payload())
    sin = out["sin"]
    total = sin["generation_total_mw"]
    assert _GEN_SANE_MW[0] <= total <= _GEN_SANE_MW[1]
    renew = sin["fuel_hydro_mw"] + sin["fuel_wind_mw"] + sin["fuel_solar_mw"]
    assert abs(sin["renewable_pct"] - round(100.0 * renew / total, 1)) < 0.11
    # thermal bundle is carried but never renamed to gas — no fake split
    assert "fuel_thermal_mw" in sin
    assert "fuel_gas_mw" not in sin
    assert abs(sin["demand_mw"] - (37202.7 + 11631.4 + 12334.7 + 7924.6)) < 0.5


def test_negative_night_solar_is_clamped():
    p = _payload()
    p["sul"]["geracao"]["solar"] = -12.5  # real ONS behavior at night
    out = _parse_balanco(p)
    assert out["subsystems"]["BR_SUL"]["fuel_solar_mw"] == 0.0


def test_stale_stamp_is_flagged_not_parsed():
    out = _parse_balanco(_payload(stamp="2026-01-01T00:00:00-03:00"))
    assert out["stale"] is True
    assert "sin" not in out


def test_missing_subsystem_is_skipped_not_fabricated():
    p = _payload()
    del p["norte"]
    out = _parse_balanco(p)
    assert "BR_NORTE" not in out["subsystems"]
    assert len(out["subsystems"]) == 3


def test_implausible_totals_refused():
    p = _payload()
    for k in ("sudesteECentroOeste", "sul", "nordeste", "norte"):
        for fuel in p[k]["geracao"]:
            p[k]["geracao"][fuel] = 1.0  # ~KW-scale garbage, not a 70GW grid
    assert _parse_balanco(p) is None


def test_garbage_payloads_refused():
    assert _parse_balanco(None) is None
    assert _parse_balanco({}) is None
    assert _parse_balanco({"Data": "not-a-date"}) is None
    assert _parse_balanco([1, 2, 3]) is None
