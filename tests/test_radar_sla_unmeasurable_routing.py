"""Where an unmeasurable table-age SLA row goes once the radar reports it
(2026-09-23). The SQL side — that it IS reported — is
tests/test_radar_sla_columns_sql.py.

sla_column_unmeasurable is its own issue, not data_freshness_sla_breach,
because a breach is autonomous: brain_autopilot maps it through REFRESH_MAP to
a refresh endpoint (gas-refresh, substations-refresh, osm-crawl …). A column
the radar cannot read is no evidence the table is stale, so it must reach a
human instead — and triage must not hand it the refresh recipe.

  R1  the autopilot has an escalation-only entry for it: no endpoint, no method
  R2  a missing-column report is triaged as schema drift, not as a dataset refresh
  R3  a text-column report is not triaged as a dataset refresh either
  R4  transmission_lines has no autonomous refresh, so the armed SLA row
      escalates on breach (its refresher TRUNCATEd the table; retired in #5314)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import brain_autopilot, brain_consistency_radar as radar  # noqa: E402
from routes.brain_error_classes import triage_findings  # noqa: E402
from routes.brain_findings_reader import _label_for  # noqa: E402


def _triaged(finding):
    """(bucket, entry) for a finding, classified through the label production
    composes for it."""
    label = _label_for(finding["issue"], finding["detail"])
    buckets = triage_findings({finding["url"]: {label: 1}})["buckets"]
    return [(name, e) for name, entries in buckets.items() for e in entries]


def test_r1_unmeasurable_escalates_and_never_dispatches():
    pat = brain_autopilot._lookup_pattern("sla_column_unmeasurable")
    assert pat is not None, "no autopilot entry: the finding would be counted no_action"
    assert pat["method"] is None
    f = radar._sla_unmeasurable("gas_pipelines", "updated_at", 720, "EIA gas pipelines",
                                'column "updated_at" does not exist')
    assert pat["action"](f) == (None, None)


def test_r2_missing_column_is_triaged_as_schema_drift():
    f = radar._sla_unmeasurable("transmission_lines", "updated_at", 720,
                                "EIA transmission lines",
                                'column "updated_at" does not exist')
    [(bucket, entry)] = _triaged(f)
    assert (bucket, entry.get("error_class")) == ("code_bug", "schema_drift_column_missing"), entry


def test_r3_text_column_is_not_triaged_as_a_refresh():
    f = radar._sla_unmeasurable("facilities", "first_seen", 336, "canonical facilities",
                                'column "first_seen" is text, not a timestamp')
    [(bucket, entry)] = _triaged(f)
    assert bucket != "data", (bucket, entry)  # the "auto-recovered" bucket
    assert entry.get("error_class") != "data_freshness_sla_breach", entry
    assert entry.get("fix_template") != "kick_dataset_refresh_cron", entry


def test_r4_transmission_breach_has_no_autonomous_refresh():
    breach = {"issue": "data_freshness_sla_breach", "url": "table:transmission_lines"}
    assert brain_autopilot._action_data_freshness_breach(breach) == (None, None)
    # Control: a table that does have a refresher still gets one.
    gas = {"issue": "data_freshness_sla_breach", "url": "table:gas_pipelines"}
    assert brain_autopilot._action_data_freshness_breach(gas)[0] == "/api/jobs/gas-refresh"
