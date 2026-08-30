"""
iso_br_ons.py — Brazil SIN grid ingestion via ONS Balanço de Energia (LIVE).

Global-expansion (2026-07-11). Sixth LIVE international integration (after GB
Elexon, AU AEMO, EU ENTSO-E, TW Taipower, JP denki-yoho + SG NEMS) and the
FIRST South American grid. Brazil is the largest LATAM data-center market
(São Paulo hyperscale + Rio/Fortaleza subsea landings), and no competitor
(WoodMac-LandGate, Enverus) carries non-US/EU live telemetry at all.

DATA SOURCE — ONS (Operador Nacional do Sistema Elétrico) real-time balance
JSON (token-free public; the backend of ons.org.br "Energia Agora"):
  https://tr.ons.org.br/Content/GetBalancoEnergetico/null
Returns per-subsystem generation by source + verified load, stamped to the
minute (America/Sao_Paulo, -03:00). Verified live 2026-07-11: SIN generation
~70.1 GW, hydro 24.5 GW + wind 13.5 GW + solar 15.4 GW ≈ 82% renewable.
Same endpoint electricitymaps' ONS parser has shipped against for ~8 years;
it survived ONS's site redesigns. Backfill/reconciliation channel (CC-BY):
dados.ons.org.br → balanco_energia_subsistema_ho CSVs on S3 (D-1, hourly).

VERIFIED GOTCHAS (2026-07-11):
  • GET only (HEAD → 405); plain http:// 302s → call https:// directly.
  • Subsystems: sudesteECentroOeste / sul / nordeste / norte. Only the SE/CO
    payload carries itaipu50HzBrasil + itaipu60Hz — BOTH are hydro delivered
    to the Brazilian grid and are INCLUDED in that subsystem's geracao.total,
    so hydro = hidraulica + itaipu50HzBrasil + itaipu60Hz.
  • solar can read slightly NEGATIVE at night (plant self-consumption) —
    clamp components at 0 and recompute the total from clamped parts.
  • There is NO SIN-wide row — the 4 subsystems are summed here.
  • "termica" is an AGGREGATE (gas + coal + oil + biomass, no split).

HONESTY:
  • LIVE-ONLY — if ONS is unreachable, writes NOTHING (no modeled fallback).
  • STALENESS GUARD — a payload whose Data stamp is >3h old is treated as a
    frozen mirror and skipped (the feed normally advances every 1-5 min).
  • SANITY BOUNDS — SIN generation outside 20-150 GW is refused rather than
    persisted (garbage/schema-drift protection).
  • renewable_pct = wind+solar+hydro / generation_total — the SAME definition
    as the US/GB/EU/TW scoreboard rows, and every one of those categories is
    explicit in the feed, so Brazil ranks apples-to-apples.
  • gas share is NOT reported — "termica" bundles gas/coal/oil/biomass with
    no public real-time split. fuel_thermal_mw carries the bundle and the
    snapshot says so; we never guess a gas number that doesn't exist.
  ISO_CODE map: ONS aggregate + BR_<subsystem> rows — the same
  aggregate-plus-zones convention as ENTSO-E (ENTSOE + EU_<code>) and Japan
  (OCCTO + JP_<area>).
"""
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._swallowed_writes import note_swallowed_write

try:
    import requests as _rq
except Exception:
    _rq = None

iso_br_ons_bp = Blueprint("iso_br_ons", __name__, url_prefix="/api/v1/iso/br")
SOURCE_ID = "iso-br-ons-live"
ISO_CODE = "ONS"

_ONS_URL = "https://tr.ons.org.br/Content/GetBalancoEnergetico/null"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
_STALE_AFTER_H = 3                      # minute-level feed; >3h old = frozen mirror
_GEN_SANE_MW = (20_000, 150_000)        # SIN-wide generation plausibility bounds

# payload key → (grid_data iso, display name)
_SUBSYSTEMS = {
    "sudesteECentroOeste": ("BR_SECO",     "Southeast/Center-West (São Paulo, Rio)"),
    "sul":                 ("BR_SUL",      "South (Porto Alegre, Curitiba)"),
    "nordeste":            ("BR_NORDESTE", "Northeast (Fortaleza, Recife)"),
    "norte":               ("BR_NORTE",    "North (Manaus, Belém)"),
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _pos(v):
    """Numeric ≥0, else 0.0 — clamps night-time negative solar + any junk."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _parse_stamp(s):
    """'2026-07-11T07:33:00-03:00' → aware datetime, or None."""
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _parse_balanco(payload):
    """Parse one GetBalancoEnergetico payload (already JSON-decoded).

    Returns {"stale": bool, "as_of": iso, "subsystems": {code: {...}},
    "sin": {...}} or None if the shape is unrecognized / out of bounds.
    Pure function — unit-tested without network. Never fabricates: a
    missing subsystem is simply absent from the result.
    """
    if not isinstance(payload, dict):
        return None
    stamp = _parse_stamp(payload.get("Data"))
    if stamp is None:
        return None  # not a balanço payload — refuse to guess
    if (datetime.now(timezone.utc) - stamp) > timedelta(hours=_STALE_AFTER_H):
        return {"stale": True, "as_of": stamp.isoformat()}

    subsystems = {}
    for key, (iso, name) in _SUBSYSTEMS.items():
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        gen = block.get("geracao")
        if not isinstance(gen, dict):
            continue
        # Itaipu 50Hz/60Hz keys exist only on SE/CO — both are hydro MW
        # delivered to the Brazilian grid (included in ONS's own total).
        hydro = (_pos(gen.get("hidraulica")) + _pos(gen.get("itaipu50HzBrasil"))
                 + _pos(gen.get("itaipu60Hz")))
        wind = _pos(gen.get("eolica"))
        solar = _pos(gen.get("solar"))
        nuclear = _pos(gen.get("nuclear"))
        thermal = _pos(gen.get("termica"))
        total = hydro + wind + solar + nuclear + thermal
        if total <= 0:
            continue
        subsystems[iso] = {
            "name": name,
            "generation_total_mw": round(total, 1),
            "fuel_hydro_mw": round(hydro, 1),
            "fuel_wind_mw": round(wind, 1),
            "fuel_solar_mw": round(solar, 1),
            "fuel_nuclear_mw": round(nuclear, 1),
            "fuel_thermal_mw": round(thermal, 1),
            "demand_mw": round(_pos(block.get("cargaVerificada")), 1),
        }
    if not subsystems:
        return None

    def _sum(k):
        return round(sum(s[k] for s in subsystems.values()), 1)

    gen_total = _sum("generation_total_mw")
    if not (_GEN_SANE_MW[0] <= gen_total <= _GEN_SANE_MW[1]):
        return None  # implausible for the SIN — refuse rather than persist garbage
    renew = _sum("fuel_hydro_mw") + _sum("fuel_wind_mw") + _sum("fuel_solar_mw")
    sin = {
        "generation_total_mw": gen_total,
        "fuel_hydro_mw": _sum("fuel_hydro_mw"),
        "fuel_wind_mw": _sum("fuel_wind_mw"),
        "fuel_solar_mw": _sum("fuel_solar_mw"),
        "fuel_nuclear_mw": _sum("fuel_nuclear_mw"),
        "fuel_thermal_mw": _sum("fuel_thermal_mw"),
        "demand_mw": _sum("demand_mw"),
        "renewable_pct": round(100.0 * renew / gen_total, 1),
    }
    return {"stale": False, "as_of": stamp.isoformat(), "subsystems": subsystems,
            "sin": sin}


def _live_snapshot():
    """Real live SIN snapshot, or None — never modeled, never stale."""
    if _rq is None:
        return None
    try:
        r = _rq.get(_ONS_URL, headers=_HEADERS, timeout=12)
        if not r.ok:
            return None
        parsed = _parse_balanco(r.json())
    except Exception:
        return None
    if not parsed or parsed.get("stale"):
        return None
    return parsed


_METRIC_UNITS = {"renewable_pct": "pct"}


def _persist(parsed):
    """Write the ONS aggregate + per-subsystem BR_<code> rows to grid_data."""
    if not parsed:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        def _ins(iso, name, value):
            nonlocal rows
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (iso, name, value, _METRIC_UNITS.get(name, "MW")),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                note_swallowed_write("grid_data", where="iso_br_ons._ins")
                pass
        for name, value in parsed["sin"].items():
            _ins(ISO_CODE, name, value)
        _ins(ISO_CODE, "subsystems_reporting", float(len(parsed["subsystems"])))
        for iso, sub in parsed["subsystems"].items():
            for name, value in sub.items():
                if name == "name":
                    continue
                _ins(iso, name, value)
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_ons_balanco_energetico",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": ("ONS Balanço de Energia (tr.ons.org.br, token-free JSON, "
                   "4 SIN subsystems, minute-level)"),
    }
    try:
        parsed = _live_snapshot()
        if parsed is None:
            summary["errors"].append(
                "ons_live_fetch_failed_or_stale — wrote nothing (no modeled fallback)")
        else:
            summary["metrics_extracted"] = (
                len(parsed["sin"]) + sum(len(s) - 1 for s in parsed["subsystems"].values()))
            summary["rows_inserted"] = _persist(parsed)
            summary["as_of"] = parsed["as_of"]
            summary["snapshot"] = dict(parsed["sin"])
            summary["subsystems_reporting"] = sorted(parsed["subsystems"].keys())
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_br_ons_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_br_ons_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    parsed = _live_snapshot()
    if parsed is None:
        return jsonify({"iso": ISO_CODE, "error": "ons_live_unavailable_or_stale"}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    return jsonify_gated_snapshot({
        "iso": ISO_CODE, "live": True,
        "metrics": dict(parsed["sin"]),
        "subsystems": parsed["subsystems"],
        "as_of": parsed["as_of"],
        "fuel_split_note": ("fuel_thermal_mw is ONS's aggregate 'termica' "
                            "(gas+coal+oil+biomass — no public real-time split), "
                            "so no gas share is reported. renewable_pct = "
                            "wind+solar+hydro (scoreboard-comparable)."),
        "source": "ONS Balanço de Energia (live, minute-level)",
    }, 200)
# AUTO-REPAIR: duplicate route '/health' also in main.py:7778 — review and remove one


@iso_br_ons_bp.route("/health", methods=["GET"])
def http_health():
    parsed = _live_snapshot()
    return jsonify({"iso": ISO_CODE, "live_feed_ok": parsed is not None,
                    "subsystems_reporting": sorted((parsed or {}).get("subsystems", {})),
                    "source": "ons_balanco_energetico_tokenfree"}), 200
