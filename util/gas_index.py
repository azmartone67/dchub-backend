"""gas_index.py — the DCGI / gas-to-grid kill switch. 2026-08-08.

Single source of truth for "may this process publish a DCGI composite or a
gas-fired $/MWh figure?". The answer is currently NO, and this module is why.

WHY OFF RATHER THAN CAVEATED
----------------------------
The 2026-08-08 adversarial audit (62 agents, 5 lenses) found four defects in
the DCGI composite and its gas-to-grid sibling. Each was observed live and
survived refutation. A disclosure footnote does not fix any of them, because
each one makes the number itself wrong — not imprecise:

  1. THE 20-POINT INTERSTATE TERM IS DEAD FOR ALL 53 PUBLISHED STATES.
     routes/gas_pipeline_ingest.py writes EIA's raw `TYPEPIPE` attribute
     verbatim, which is `Interstate` / `Intrastate` (capitalised).
     routes/dcgi.py compared `ptype == "interstate"` (lowercase), so it
     matched only the ~1% legacy `source='eia'` rows. Measured live
     2026-08-08 via /api/v1/gas-pipelines spatial sampling:

         lat=39.0,lng=-84.5  ->  Interstate 48, interstate 2
         lat=40.4,lng=-79.9  ->  Interstate 36, interstate 2
         lat=32.7,-96.8      ->  Intrastate 37, Interstate 3, intrastate 1

     Texas published `interstate: 7` against `pipelines: 6731`. One fifth of
     the access score was a constant zero wearing a variable's name.

  2. 40% OF THE COMPOSITE WAS A NON-DETERMINISTIC SQL TIE-BREAK.
     The cost term selected a state's price with
     `DISTINCT ON (UPPER(state)) ... ORDER BY UPPER(state), period DESC`
     across BOTH the EIA PIN (industrial) and PEU (electric-power) series.
     The two are not comparable — PIN carries the LDC distribution margin,
     PEU does not — and with no tiebreaker after `period`, Postgres is free
     to return either row for the same state on the same day. Same market,
     different DCGI per query.

  3. NINE STATES INCLUDING TEXAS RAN ON A HARDCODED CONSTANT.
     `cost = 50.0  # neutral when no price coverage` fired wherever
     eia_gas_prices held no row. Texas published DCGI 68.0 / rank #3 /
     "GAS-ADVANTAGED" off that constant; Montana ranked #4, ahead of
     Virginia, Ohio, Georgia and Illinois. Verified live 2026-08-08:

         GET /api/v1/dcgi/scores/TX
         -> {"dcgi":68.0,"gas_access_score":80.0,"gas_cost_score":50.0,
             "interstate":7,"pipelines":6731,"verdict":"GAS-ADVANTAGED"}

     Anonymous. No key. Both defects visible in one 200 response.

  4. THE $/MWh SURFACES DISAGREED WITH EACH OTHER BY UP TO 5.5x.
     Five surfaces published a gas-fired $/MWh for the same market on the
     same day, each picking the burner-tip price by a different rule, with
     no sanity gate anywhere. /api/v1/markets/phoenix/gas-to-grid served a
     physically impossible $6.73/MWh stamped `data_basis: "live"`.

"IT'S PRO-GATED" WAS NOT A DEFENCE
-----------------------------------
/api/v1/dcgi/scores/<STATE> and the /dcgi noscript table both served the
full numeric composite to anonymous callers. The soft-paywall covered
/api/v1/dcgi/scores (the list) and nothing else.

CURRENT STATE — 2026-08-30
--------------------------
    DCHUB_GAS_INDEX_ENABLED=1     ON.  Defects 1, 2 and 3 are repaired and the
                                  cost scale is recalibrated to the series it
                                  now reads. See /api/v1/dcgi/methodology ->
                                  corrections for the published record.
    DCHUB_GAS_TO_GRID_ENABLED     OFF. Defect 4 of the same audit — five
                                  surfaces publishing a gas-fired $/MWh for
                                  one market on one day, disagreeing by up to
                                  5.5x with no sanity gate — is NOT fixed.
                                  The DCGI repair does not touch it. Do not
                                  flip this one because the other one moved.

Two separate switches precisely so the cheaper fix could ship first, and so
that restoring DCGI could not silently restore the $/MWh figures with it.
Read per-call, not at import, so ops can flip either without a redeploy.

THE FENCES
----------
tests/test_gas_index_disabled_while_defective.py was the WITHDRAWAL fence: it
failed the build if a composite was served while a term was still broken. It
carried a dead-man's switch that failed once every fence in it skipped, with
instructions to re-enable, verify, and delete it in the same PR rather than
leave it green and checking nothing. That is what happened; it is gone.

Its forward replacement is tests/test_dcgi_terms_stay_fixed.py, which asserts
the terms are still RIGHT rather than that the index is still DOWN:
case-insensitive interstate matching at both sites, a total ordering on the
price pick, no numeric constant ever assigned to `cost`, and — functionally,
through the real scoring path — that an unpriced state comes back UNSCORED
with its reason rather than neutral-scored. The cost calibration is fenced
separately in tests/test_dcgi_cost_scale.py.
"""
import os

__all__ = [
    "gas_index_enabled",
    "GAS_STATE_TOKEN",
    "gas_index_copy",
    "resolve_gas_copy",
    "gas_to_grid_enabled",
    "gas_index_unavailable",
    "gas_to_grid_unavailable",
    "strip_index_fields",
    "GAS_INDEX_FIELDS",
    "GAS_TO_GRID_FIELDS",
    "AUDIT_REF",
    "WITHDRAWAL_HTTP_STATUS",
]

AUDIT_REF = "DCHUB_ANALYST_GRADE_AUDIT_2026-08-08"

# ── ONE AUTHORITY FOR WHAT WE SAY ABOUT THE INDEX'S STATE ──────────────────
# 2026-08-30. The kill switch reached three modules. FIVE others asserted the
# withdrawal in hardcoded prose — the agent cookbook, AGENTS.md, the
# competitor positioning copy, the AI-surface audit and the MCP server — so
# flipping DCHUB_GAS_INDEX_ENABLED would have served scores from
# /api/v1/dcgi/scores while `get_gas_index`'s own routing note still told the
# agent it had been withdrawn on 2026-08-08. The API and the agent channel
# would have contradicted each other, which is the failure the withdrawal
# existed to prevent, pointed the other way.
#
# Copy now carries a TOKEN and the sentence is resolved at serve time, so a
# consumer cannot hold a stale opinion about the switch. Enforced by
# tests/test_gas_index_copy_has_one_authority.py: no user-visible module may
# hardcode the withdrawal date, and no token may survive rendering in EITHER
# switch position.
# ★ Brace-FREE on purpose. The first version used {{GAS_INDEX_STATE}} and
#   AGENTS.md renders from an f-string, where Python collapses {{ }} to
#   { } at runtime — so the token mangled itself into {GAS_INDEX_STATE},
#   never matched the resolver, and shipped raw into the agent-facing
#   contract. Caught by the both-switch-positions fence, not by review.
GAS_STATE_TOKEN = "@@GAS_INDEX_STATE@@"


def gas_index_copy():
    """The one sentence any surface may say about the DCGI's current state."""
    if gas_index_enabled():
        return (
            "The DCGI score was withdrawn 2026-08-08 and restored 2026-08-30 "
            "after all three defective terms were repaired; scores published "
            "before 2026-08-08 are NOT comparable to current ones (see "
            "/api/v1/dcgi/methodology -> corrections). The gas-to-grid $/MWh "
            "remains withdrawn — a separate, unfixed defect."
        )
    return (
        "The DCGI score and every gas-to-grid $/MWh were withdrawn "
        "2026-08-08 — get_gas_index returns an unavailable_reason, never a "
        "score; do not quote a cached DCGI figure."
    )


def resolve_gas_copy(obj):
    """Substitute GAS_STATE_TOKEN through a str / dict / list, in place-ish.

    Applied at each surface's single rendering choke point rather than at each
    string, so adding a new recipe cannot forget it.
    """
    if isinstance(obj, str):
        return obj.replace(GAS_STATE_TOKEN, gas_index_copy()) if GAS_STATE_TOKEN in obj else obj
    if isinstance(obj, dict):
        return {k: resolve_gas_copy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(resolve_gas_copy(v) for v in obj)
    return obj

# ★★★ THE WITHDRAWAL RESPONSE MUST NOT BE A 5xx. 200, deliberately.
#
# It shipped as 503 for about twenty minutes on 2026-08-08 and that was a
# site-wide availability bug, not a cosmetic one.
#
# _worker.js runs a REACTIVE circuit breaker on the primary origin:
# 2 x 5xx from Railway within 10 seconds trips it for 30 seconds, and all
# traffic falls through to the Render mirror — which auto-deploys on every
# commit to main, burns its monthly pipeline minutes on ~120 crons + brain
# PRs, hits "Build blocked", and goes silently stale. So a 5xx here does not
# degrade the gas endpoints; it takes the ENTIRE SITE down to whatever code
# Render last managed to build. Two crawler hits on /api/v1/dcgi/scores
# within ten seconds were enough to hold it there.
#
# The status code is also just wrong on its own terms. 5xx means "the server
# failed". Nothing failed. The index is withdrawn — a deliberate, stable,
# indefinite state that every caller should be able to cache and move on
# from. A 503 tells a client to retry, tells our own error-rate monitoring
# the origin is unhealthy, and tells the circuit breaker to evacuate.
#
# Rejected alternatives:
#   410 Gone      — semantically closest, and a 4xx would not trip the
#                   breaker, but 410 means PERMANENT. Search engines and
#                   agent caches treat it as "delete this". We intend to
#                   re-enable once the terms are fixed.
#   404 Not Found — false. The route exists and has something true to say.
#   503           — the bug above.
#
# 200 with `ok: false`, `available: false`, `status: "disabled"` and an
# `unavailable_reason` is unambiguous to anything that reads the body, which
# is what every real consumer of this API does. tests/ fences that no
# withdrawal path can return a 5xx again.
WITHDRAWAL_HTTP_STATUS = 200

# Every field name that carries a DCGI composite or one of its two component
# scores. Anything listed here is withheld while the index is disabled.
GAS_INDEX_FIELDS = (
    "dcgi",
    "dcgi_score",
    "gas_cost_score",
    "gas_access_score",
    "verdict",
    "dcgi_verdict",
    "gas_price",
    "gas_price_sector",
    "gas_price_period",
    # `interstate` is withheld as a FIGURE, not only as a score input: the
    # same case mismatch that killed the 20-point term also makes the
    # published count wrong on its own (TX: 7 against 6,731 segments,
    # ~145x low). It is not a "component of a disabled score" — it is a
    # broken number that happened to also feed one.
    "interstate",
    "pipelines_interstate",
)

# Every field name that carries a gas-fired $/MWh figure.
GAS_TO_GRID_FIELDS = (
    "usd_per_mwh_cc_new",
    "usd_per_mwh_cc_avg",
    "usd_per_mwh_cc_old",
    "usd_per_mwh_peaker",
    "usd_per_mwh_peaker_old",
    "scenarios_usd_per_mwh",
    "gas_to_grid_usd_per_mwh",
    "custom_scenario",
    "headline_behind_meter_vs_grid_delta_usd_mwh",
)


def _on(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def gas_index_enabled() -> bool:
    """True only if an operator has explicitly re-enabled the DCGI composite."""
    return _on("DCHUB_GAS_INDEX_ENABLED")


def gas_to_grid_enabled() -> bool:
    """True only if an operator has explicitly re-enabled gas-fired $/MWh."""
    return _on("DCHUB_GAS_TO_GRID_ENABLED")


def gas_index_unavailable(surface: str = "") -> dict:
    """The honest body served in place of a DCGI composite.

    Names the defects rather than hiding behind "temporarily unavailable" —
    an analyst who already pulled the number deserves to know which term was
    wrong, and a returning agent needs a reason it can quote.
    """
    out = {
        "ok": False,
        "available": False,
        "status": "disabled",
        "unavailable_reason": (
            "The Data Center Gas Index (DCGI) is withdrawn pending correction. "
            "It is not degraded or delayed — two of its three terms were "
            "measurably wrong, so no subset of it is publishable. (1) The "
            "20-point interstate-share term matched a lowercase literal "
            "against capitalised EIA TYPEPIPE values and scored ~0 for all 53 "
            "published states. (2) The 40%-weighted cost term fell back to a "
            "hardcoded 50.0 for nine states including Texas, which was "
            "published as DCGI 68.0 / rank #3 / GAS-ADVANTAGED on that "
            "constant; where a price did exist it was chosen by a "
            "non-deterministic tie-break between the EIA industrial (PIN) and "
            "electric-power (PEU) series, which are not comparable."
        ),
        "withdrawn_on": "2026-08-08",
        "audit_ref": AUDIT_REF,
        "what_is_still_published": [
            "/api/v1/dcgi/operators — parent-midstream registry with live "
            "natural-gas segment counts. No composite, no price term.",
            "/api/v1/gas-pipelines — per-segment EIA geodata.",
            "/api/v1/dcgi/methodology — the scoring definition, so the "
            "withdrawn figure stays auditable.",
        ],
        "reenable": "set DCHUB_GAS_INDEX_ENABLED=1 once the terms are fixed",
    }
    if surface:
        out["surface"] = surface
    return out


def gas_to_grid_unavailable(surface: str = "") -> dict:
    """The honest body served in place of a gas-fired $/MWh figure."""
    out = {
        "ok": False,
        "available": False,
        "status": "disabled",
        "unavailable_reason": (
            "Gas-to-grid $/MWh is withdrawn pending correction. Five surfaces "
            "published a gas-fired $/MWh for the same market on the same day, "
            "up to 5.5x apart, because each chose the burner-tip price by a "
            "different rule; at least one headline conclusion inverted between "
            "them. No sanity gate existed on the output — "
            "/api/v1/markets/phoenix/gas-to-grid served a physically "
            "impossible $6.73/MWh stamped data_basis: \"live\". The heat-rate "
            "arithmetic is correct; the input price selection is not, so the "
            "product is withheld rather than caveated."
        ),
        "withdrawn_on": "2026-08-08",
        "audit_ref": AUDIT_REF,
        "what_is_still_published": [
            "/api/v1/markets/<slug>/gas-pricing — Henry Hub spot and the "
            "delivered EIA tariff in $/MMBtu, each with its own source label. "
            "The $/MMBtu inputs are sourced; only the $/MWh derivation is "
            "withdrawn.",
            "/api/v1/markets/gas-pricing/methodology — the heat-rate formula.",
        ],
        "reenable": "set DCHUB_GAS_TO_GRID_ENABLED=1 once price selection is "
                    "deterministic and sanity-gated",
    }
    if surface:
        out["surface"] = surface
    return out


def strip_index_fields(row, fields=GAS_INDEX_FIELDS):
    """Return a copy of `row` with every listed field REMOVED (not nulled).

    Removal, not masking: a null `dcgi` beside a populated `pipelines` reads
    as "we could not compute it this time" and invites a retry. Absence plus
    the sibling `unavailable_reason` reads as what it is — withdrawn.
    """
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in row.items() if k not in fields}
