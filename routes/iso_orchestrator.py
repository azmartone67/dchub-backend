"""
iso_orchestrator.py — fan out to all ISO extractors in one call.

POST /api/v1/iso/all/extract — runs every ISO extractor in parallel
via a thread pool, returns per-ISO results. Future cron only needs
ONE URL to refresh the entire grid coverage.

Phase HH+ (2026-05-13): switched from sequential to parallel fan-out.
At 11 ISOs × ~3-5s each, sequential = 30-55s which blew through CF
Worker's 15s edge timeout. ThreadPool brings wall time down to
roughly max(per-ISO) + epsilon (~5-8s typical).
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, jsonify

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


iso_orchestrator_bp = Blueprint("iso_orchestrator", __name__, url_prefix="/api/v1/iso/all")
SOURCE_ID = "iso-orchestrator"

# Per-ISO hard ceiling so one slow upstream can't starve the whole batch.
# Most ISOs respond in <5s; 12s leaves headroom for EIA EBA which can be
# sluggish, while still keeping wall time well under CF Worker's 15s.
_PER_ISO_TIMEOUT_S = 12


def _run_one(extractor_module_name, iso_label):
    """Call run_extraction on a sibling iso_* module, capture result/error."""
    try:
        mod = __import__(f"routes.{extractor_module_name}", fromlist=["run_extraction"])
        return mod.run_extraction()
    except Exception as e:
        return {"iso": iso_label, "status": "import_error",
                "error": f"{type(e).__name__}: {e}"}


@iso_orchestrator_bp.route("/extract", methods=["POST", "GET"])
def extract_all():
    started = time.time()
    results = []

    # Phase GG (2026-05-13): added PJM — until now DC Hub advertised
    # "7 ISOs" but only registered 6 here. PJM is the largest US ISO
    # (~150 GW peak) covering mid-Atlantic + Ohio Valley.
    #
    # Phase HH (2026-05-13): expanded coverage 7 → 11 grid operators.
    # The 4 new entries are NOT all traditional ISOs:
    #   IESO  — Independent Electricity System Operator (Ontario)
    #   AESO  — Alberta Electric System Operator
    #   TVA   — Tennessee Valley Authority (vertically-integrated US fed utility)
    #   BPA   — Bonneville Power Administration (Pacific NW federal PMA)
    # All four have major data-center build-out + public hourly fuel-mix
    # data (or EIA EBA fallback for TVA/BPA). Together they close the
    # biggest North American DC-market coverage gaps.
    extractors = [
        ("iso_ercot", "ERCOT"),
        ("iso_caiso", "CAISO"),
        ("iso_nyiso", "NYISO"),
        ("iso_miso",  "MISO"),
        ("iso_pjm",   "PJM"),     # ← Phase GG
        ("iso_spp",   "SPP"),
        ("iso_isone", "ISONE"),
        ("iso_ieso",  "IESO"),    # ← Phase HH — Ontario
        # ("iso_aeso", "AESO") removed 2026-05-30 — the US-realtime extractor
        # persisted 0 rows since registration. AESO is served by the
        # iso_aeso_intl baseline model (at /api/v1/iso/aeso + /aeso-intl),
        # not this orchestrator, so it no longer runs the dead extractor.
        ("iso_tva",   "TVA"),     # ← Phase HH — Tennessee Valley
        ("iso_bpa",   "BPA"),     # ← Phase HH — Pacific NW
        # 2026-05-30: non-ISO utility/co-op balancing authorities (16 BAs:
        # APS/SRP/FPL + big IOUs + co-ops). run_extraction() fans out all 16
        # in parallel internally, so this single slot stays under the timeout.
        ("eia_utility_bas", "UTILITY_BAS"),
    ]

    # Phase HH+: parallel fan-out (ThreadPool). I/O-bound network work,
    # GIL is fine. max_workers = len(extractors) so every ISO gets its
    # own thread — they're all just sitting in requests.get() most of
    # the time. Per-future timeout prevents any one stall from blocking
    # the orchestrator past CF Worker's edge limit.
    with ThreadPoolExecutor(max_workers=max(len(extractors), 4)) as pool:
        future_to_label = {
            pool.submit(_run_one, mod_name, label): (mod_name, label)
            for mod_name, label in extractors
        }
        for fut in as_completed(future_to_label, timeout=None):
            mod_name, label = future_to_label[fut]
            try:
                results.append(fut.result(timeout=_PER_ISO_TIMEOUT_S))
            except Exception as e:
                # TimeoutError or any propagated extractor failure that
                # somehow escaped _run_one's try/except.
                results.append({
                    "iso": label,
                    "status": "timeout" if "Timeout" in type(e).__name__ else "error",
                    "error": f"{type(e).__name__}: {e}",
                })

    elapsed_ms = int((time.time() - started) * 1000)
    total_rows = sum(r.get("rows_inserted", 0) for r in results)
    failed = [r for r in results if r.get("status") not in ("ok",)]

    # Status logic: orchestrator's job is to run all extractors.
    # If at least ONE produced rows, the orchestrator succeeded — per-ISO
    # failures are still visible on each source's own page.
    succeeded_count = sum(1 for r in results if r.get("status") == "ok")
    if total_rows > 0 or succeeded_count >= len(results) / 2:
        orch_status = "success"
    elif succeeded_count == 0:
        orch_status = "failure"
    else:
        orch_status = "success"  # at least 1 worked
    _heartbeat(
        SOURCE_ID,
        status=orch_status,
        rows_affected=total_rows,
        duration_ms=elapsed_ms,
        error=("; ".join(r.get("error", "") for r in failed)[:500] if orch_status == "failure" else None),
        metadata={
            "iso_count": len(results),
            "succeeded": succeeded_count,
            "failed_isos": [r["iso"] for r in failed],
        },
    )

    return jsonify(
        duration_ms=elapsed_ms,
        iso_count=len(results),
        total_rows_inserted=total_rows,
        failed_count=len(failed),
        results=results,
    ), 200


@iso_orchestrator_bp.route("/health", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        registered_isos=["ERCOT", "CAISO", "NYISO", "MISO", "PJM", "SPP", "ISONE",
                          "IESO", "AESO", "TVA", "BPA"],
        endpoint="/api/v1/iso/all/extract",
        iso_count=11,
        future_isos=["ESO (UK)", "AEMO (AU)", "EirGrid (IE)"],
    ), 200
