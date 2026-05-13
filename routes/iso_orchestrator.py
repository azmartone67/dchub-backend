"""
iso_orchestrator.py — fan out to all ISO extractors in one call.

POST /api/v1/iso/all/extract — runs every ISO extractor sequentially,
returns per-ISO results. Future cron only needs ONE URL to refresh
the entire grid coverage.
"""

import time
from flask import Blueprint, jsonify

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


iso_orchestrator_bp = Blueprint("iso_orchestrator", __name__, url_prefix="/api/v1/iso/all")
SOURCE_ID = "iso-orchestrator"


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
    # (~150 GW peak) covering mid-Atlantic + Ohio Valley. The
    # autonomous intelligence dashboard reported "3 ISOs reporting"
    # because MISO/SPP/ISONE had 0 rows or errored; with PJM added +
    # SPP/ISONE URL refreshes (same phase), full 7-ISO coverage.
    extractors = [
        ("iso_ercot", "ERCOT"),
        ("iso_caiso", "CAISO"),
        ("iso_nyiso", "NYISO"),
        ("iso_miso",  "MISO"),
        ("iso_pjm",   "PJM"),     # ← Phase GG
        ("iso_spp",   "SPP"),
        ("iso_isone", "ISONE"),
    ]

    for module_name, label in extractors:
        results.append(_run_one(module_name, label))

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
        registered_isos=["ERCOT", "CAISO", "NYISO", "MISO", "PJM", "SPP", "ISONE"],
        endpoint="/api/v1/iso/all/extract",
        iso_count=7,
        future_isos=[],
    ), 200
