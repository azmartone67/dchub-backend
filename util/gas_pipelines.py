"""gas_pipelines.py — canonical "is this row natural gas?" predicate. 2026-08-08.

`gas_pipelines` is not a natural-gas table. It is the union of five EIA
ArcGIS feature services (infrastructure_discovery.EIA_PIPELINE_APIS), four of
which carry a different commodity:

    natural_gas      Natural_Gas_Interstate_and_Intrastate_Pipelines_1
    crude_oil        Crude_Oil_Trunk_Pipelines_1                     <- not gas
    hgl              Hydrocarbon_Gas_Liquids_Pipelines_1             <- not gas
    petroleum        Petroleum_Products_Pipelines_1                  <- not gas
    gulf_pipelines   Oil_And_Natural_Gas_Pipelines_Gulf_2024Q4       <- mixed

Measured 2026-08-08: 319 non-natural-gas rows sat inside every published
"natural gas pipeline" total — crude_oil 129, petroleum/refined 118, hgl 70,
offshore 2. Sampled live through /api/v1/gas-pipelines, which returns the
discriminator on every row:

    lat=32.7,lng=-96.8 -> pipeline_type {Intrastate 37, petroleum 6,
                          Interstate 3, hgl 2, crude_oil 1, intrastate 1}
                          source {eia_geodot_lines 40, eia_petroleum 6,
                          eia_hgl 2, eia_crude_oil 1, eia 1}

HGL is the trap in that list. Hydrocarbon gas liquids are ethane, propane and
butane — the word "gas" is in the name and they are not natural gas, cannot be
burned in a CCGT as delivered, and have no bearing on data-center gas access.
The Gulf service is explicitly "Oil AND Natural Gas": its rows are not
attributable to either commodity, so they are excluded rather than assumed.

★ TWO COLUMNS, NOT ONE
Both `pipeline_type` and `source` are tested. Either alone is a single point
of failure: `pipeline_type` is EIA's raw TYPEPIPE attribute passed through
verbatim (see the DCGI case-mismatch defect in util/gas_index.py — that is
what happens when one relies on its exact spelling), and `source` is stamped
by whichever loader last touched the row, of which there have been at least
four. A row is dropped if EITHER column says it is not natural gas.

★ DENYLIST, NOT ALLOWLIST — DELIBERATELY
An allowlist on ('interstate','intrastate') would also drop every row whose
type is NULL or a spelling we have not seen, and the natural-gas bulk loader
writes `pipeline_type` straight from an upstream attribute that has already
changed shape once. Silently dropping unrecognised rows from a published
COUNT is the same class of error as silently including the wrong ones. The
denylist keeps unknown rows in the total and
tests/test_gas_pipeline_commodity_guard.py fences the served call sites.

USAGE — import, do not re-inline:

    from util.gas_pipelines import NG_ONLY
    cur.execute(f"SELECT COUNT(*) FROM gas_pipelines WHERE {NG_ONLY}")

The predicate is a bare SQL fragment with NO parameters and NO literal `%`,
so it is safe to f-string into a query that also passes a params tuple
(psycopg2 would otherwise read a `%` as a placeholder — see
reference_psycopg2_empty_tuple_percent_trap).
"""

__all__ = ["NG_ONLY", "NON_GAS_TYPES", "NON_GAS_SOURCES", "is_natural_gas"]

# pipeline_type values that are NOT natural gas. Compared lowercased.
NON_GAS_TYPES = ("crude_oil", "crude oil", "petroleum", "refined",
                 "refined_products", "hgl", "ngl", "offshore")

# source values that are NOT (purely) natural gas. Compared lowercased.
NON_GAS_SOURCES = ("eia_crude_oil", "eia_petroleum", "eia_hgl", "eia_gulf")

_TYPES_SQL = ",".join("'%s'" % t for t in NON_GAS_TYPES)
_SOURCES_SQL = ",".join("'%s'" % s for s in NON_GAS_SOURCES)

# The canonical predicate. NULL-safe on both columns: a row with no type and
# no source stays in the natural-gas population (see DENYLIST note above).
NG_ONLY = (
    "COALESCE(LOWER(pipeline_type),'') NOT IN (%s) "
    "AND COALESCE(LOWER(source),'') NOT IN (%s)" % (_TYPES_SQL, _SOURCES_SQL)
)

assert "%" not in NG_ONLY, "NG_ONLY must contain no literal %% (psycopg2 trap)"


def is_natural_gas(pipeline_type=None, source=None) -> bool:
    """Python mirror of NG_ONLY, for rows already in memory."""
    return ((pipeline_type or "").strip().lower() not in NON_GAS_TYPES
            and (source or "").strip().lower() not in NON_GAS_SOURCES)
