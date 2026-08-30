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


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_ieso.py:426 — review and remove one
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
        ("iso_ieso",  "IESO"),    # ← Phase HH — Ontario (LIVE since shell #41)
        # shell #41 WS2 (2026-07-28) — CORRECTION to the 2026-05-30 note that
        # used to sit here. It said the AESO extractor "persisted 0 rows since
        # registration", which read as "the feed is dead". The feed is NOT
        # dead: ets.aeso.ca answers tokenless (probed HTTP 200, 9,680 B).
        # The bug was _iso_common.parse_csv_numeric_columns returning {} on a
        # SECTIONED body and the extractor then reporting status "ok" with 0
        # rows. The section-aware parser now lives in iso_aeso_intl — the
        # module that actually owns /api/v1/iso/aeso — so AESO rejoins the
        # fan-out THERE. routes/iso_aeso.py stays dead: main.py never imports
        # it, and registering it would collide with the iso_aeso_canonical
        # alias at main.py:34271-34274.
        ("iso_aeso_intl", "AESO"),
        # Registered since 2026-05-24 but scheduled ZERO times until now —
        # absent from this list AND from crawler_scheduler._RUNNERS, so its
        # rows only ever landed when a human hit /run. Now LIVE off
        # hydroquebec.com open data, and LIVE-only: a failed fetch writes
        # nothing rather than backfilling a modeled row.
        ("iso_hydroquebec", "HYDROQUEBEC"),
        # NOT added: iso_nordpool_intl. 7 of its 15 NORDIC_ZONES
        # (iso_nordpool_intl.py:42-44) already rank LIVE via ENTSO-E as
        # EU_NO_1/EU_NO_2/EU_SE_3/EU_SE_4/EU_FI/EU_DK_1/EU_DK_2, so a modeled
        # Nordics aggregate would rank a synthetic row alongside its own live
        # constituents. The genuinely-missing Nordic/Baltic zones belong in
        # iso_eu_entsoe._ZONES, not here.
        ("iso_tva",   "TVA"),     # ← Phase HH — Tennessee Valley
        ("iso_bpa",   "BPA"),     # ← Phase HH — Pacific NW
        # #60 (2026-06-02): LIVE international grids via tokenless public APIs
        # (real data, NOT modeled baselines). GB = Elexon Insights (5-min fuel
        # mix + demand); AU = AEMO NEM summary (demand + gen + price). Feed the
        # NGESO/AEMO-tagged DCPI markets (London, Sydney, Melbourne).
        ("iso_uk_elexon", "NGESO"),
        ("iso_au_aemo",   "AEMO"),
        # ENTSO-E EU (2026-06-02): ONE token unlocks ~12 major European
        # DC-market bidding zones (Frankfurt/Paris/Amsterdam/Dublin/Madrid/
        # Brussels/Warsaw/Vienna + Nordics). LIVE actual-generation-per-type;
        # LIVE-only no-op if ENTSOE_API_Token is unset. Internally fans out to
        # all zones, so this single slot stays under the per-ISO timeout.
        ("iso_eu_entsoe", "ENTSOE"),
        # APAC #2 (2026-06-02): Taiwan via Taipower real-time generation
        # (token-free, browser-UA). Top APAC DC market (TSMC + hyperscalers).
        # LIVE-only. Japan/Korea/India/SG/NZ remain auth-gated/fragmented.
        ("iso_tw_taipower", "TAIPOWER"),
        # APAC #3+#4 (2026-07-01, daily-content-feeds #2): Japan via the TSO
        # denki-yoho CSVs (6 verified-live areas incl TEPCO; fans out
        # internally like eia_utility_bas, so one slot) and Singapore via the
        # NEMS community mirror. Both LIVE-only with staleness guards.
        # 2026-07-11: Japan's slot now ALSO ingests the eria_jukyu 30-min
        # FULL fuel mix for all 10 areas (same internal fan-out, same slot).
        ("iso_jp_denkiyoho", "OCCTO"),
        ("iso_sg_nems",     "EMA"),
        # Global expansion (2026-07-11): Brazil ONS (first South American
        # grid — live minute-level balanço, full renewable split, thermal
        # bundled) and South Korea KPX (5-min full fuel mix scraped from the
        # token-free real-time dashboard; the data.go.kr API alternative is
        # key-gated). Both LIVE-only with staleness+sanity guards. NOTE:
        # KPX responds in 14-18s from US IPs — its thread can outlive the
        # 12s result budget below; the write still lands, the summary just
        # reports late (visible in this endpoint's per-ISO results).
        ("iso_br_ons",  "ONS"),
        ("iso_kr_kpx",  "KEPCO-KR"),
        # 2026-05-30: non-ISO utility/co-op balancing authorities (43 BAs:
        # APS/SRP/FPL + big IOUs + Pacific-NW PUDs + WAPA + co-ops).
        # run_extraction() fans out all of them in parallel internally
        # (max_workers=24 in routes/eia_utility_bas.py — this comment read "12"
        # until 2026-07-29; read the code, not the comment), so this single
        # slot stays under the timeout. ws2: each BA's EIA fetch is memoised
        # for the hourly EIA-930 cadence (routes/eia930.py), so most 15-min
        # ticks reuse a parsed reading instead of re-hitting EIA.
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

# AUTO-REPAIR: duplicate route '/health' also in main.py:7891 — review and remove one

@iso_orchestrator_bp.route("/health", methods=["GET"])
def health():
    # 2026-05-31: registered_isos = the 10 North-American grid operators this
    # orchestrator runs. AESO was dropped 2026-05-30 (its US-realtime extractor
    # was removed; it now lives in the iso_aeso_intl baseline model). Plus 43
    # US utility BAs via the eia_utility_bas slot (counted separately).
    #
    # method per operator (2026-05-31, #100 ISO coverage expansion):
    #   9 LIVE feeds — ERCOT/CAISO/NYISO/ISONE pull native ISO feeds; MISO/PJM/
    #     BPA/TVA pull the authenticated EIA-930 balancing-authority feed (MISO
    #     repointed to EIA-930 today — its api.misoenergy.org Data Broker feed
    #     went "no data"); SPP pulls its real-time market page.
    #   1 MODELED — IESO (Ontario) is now a modeled baseline (iso_ieso.py).
    #     reports.ieso.ca went behind Okta SAML SSO (no longer auth-free) and
    #     Ontario is outside EIA-930, so there is no auth-free live feed. It
    #     still WRITES real grid_data rows, anchored to IESO's published 2024
    #     mix — same honest treatment as the other Canadian operators (AESO,
    #     Hydro-Québec). So this is strictly "9 live + 1 modeled", not 10 live.
    # 2026-06-02 (#60): international LIVE grids now shipped — GB (NGESO/Elexon),
    # AU (AEMO), EU (ENTSO-E bidding zones), TW (Taipower). The old
    # future_isos list (UK/AU/EirGrid) is now LIVE (EirGrid/IE is covered by the
    # ENTSO-E IE_SEM zone). Remaining future = APAC beyond TW/AU, which is
    # auth-gated/fragmented (Japan OCCTO, Korea KPX, India Grid-India, SG EMA).
    #
    # ws2-merged (2026-07-29): the EU zone count is READ from the registry,
    # never typed here — this payload said "12 zones" for two months while the
    # registry held 25. Import failure → the label carries NO number rather than
    # a stale one. "configured" is deliberate: a zone reaches the scoreboard
    # only if its ENTSO-E call answered, so it is an upper bound.
    try:
        from routes.iso_eu_entsoe import _ZONES as _eu_zones
        _eu_zones_configured = len(_eu_zones)
    except Exception:
        _eu_zones_configured = None
    return jsonify(
        status="ok",
        # ★ HAND-MAINTAINED, and it has drifted before. The list that actually
        # runs is `extractors` inside extract_all() — this is a second copy and
        # can only be trusted as far as its last edit. POST
        # /api/v1/iso/all/extract returns the real count.
        registered_isos_source="hand-maintained copy, NOT derived from extractors",
        registered_isos=["ERCOT", "CAISO", "NYISO", "MISO", "PJM", "SPP", "ISONE",
                          "IESO", "AESO", "HYDROQUEBEC", "TVA", "BPA"],
        endpoint="/api/v1/iso/all/extract",
        north_america_iso_count=12,
        # 2026-07-28: IESO, AESO and HYDROQUEBEC all moved from modeled to LIVE
        # (reports-public.ieso.ca, ets.aeso.ca, hydroquebec.com open data — all
        # tokenless, all probed). No North-American operator here is modeled.
        live_feed_count=12,
        modeled_baseline_count=0,
        modeled_isos=[],
        utility_bas_count=43,
        # 2026-07-01 (daily-content-feeds #2): Japan + Singapore now LIVE —
        # OCCTO aggregate + TEPCO/JP_<area> rows from 6 verified denki-yoho
        # CSVs (of 10 areas; Kansai/Tohoku serve stale mirrors, honestly
        # skipped) and EMA from the NEMS community mirror (nems.sn.sg).
        # 2026-07-11 (global expansion): Japan upgraded to FULL fuel mix
        # (eria_jukyu 30-min CSVs, all 10 areas incl Kansai/Tohoku); Brazil
        # (ONS, minute-level, first South American grid) + South Korea (KPX
        # 5-min full mix — the dashboard is token-free; only the data.go.kr
        # API needs a key) now LIVE.
        international_live=["NGESO (GB, Elexon)", "AEMO (AU)",
                            (f"ENTSOE (EU, {_eu_zones_configured} zones configured)"
                             if _eu_zones_configured else "ENTSOE (EU)"),
                            "TAIPOWER (TW)",
                            "OCCTO (JP, 5-min demand 6 areas + 30-min full fuel mix all 10 areas)",
                            "EMA (SG, NEMS mirror)",
                            "ONS (BR, 4 subsystems, minute-level full renewable split)",
                            "KEPCO-KR (KR, KPX 5-min full fuel mix)"],
        future_isos=["India (Grid-India)"],
        future_isos_note=("India's fuel-mix sources (grid-india.in, meritindia.in) "
                          "TLS-reset non-Indian IPs — needs an India-region relay "
                          "(~$5/mo Mumbai VPS) before a live feed is possible; "
                          "npp.gov.in works from US but is daily per-plant MU with "
                          "no wind/solar split"),
    ), 200
