"""tests/test_probe_timeout_honesty.py — a probe timeout must exceed the
callee's own budget (2026-08-12).

Shell #63 lane 1 reported `predictions (ReadTimeout ... read timeout=8)` on
every tick. The endpoint was not broken. `_gather_predictions()` in
routes/brain_layer6_predictive.py makes THREE nested loopback calls before it
computes anything —

    /api/v1/mcp/funnel                @ 5s
    /api/v1/marketing/worker-status   @ 5s
    /api/v1/reach                     @ 8s

— an 18-second internal budget, plus a per-metric _velocity() query that opens
its own connection. L14 probed it with an 8s timeout, so a perfectly healthy
service could not answer in time.

★A timeout you know the callee cannot meet does not measure the callee. It
measures the timeout, and reports the result as the callee's failure. That is
the same class as the bare {} this series has been removing: a fact about our
own instrument, presented as a fact about the world.

Guards:
  (1) REGRESSION — the predictions probe timeout drops back below the callee's
      own internal budget.
  (2) DRIFT — someone raises brain_layer6's internal timeouts without raising
      the probe, silently recreating the gap from the other side.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_probe_timeout_honesty.py -v
"""
from __future__ import annotations

import importlib
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _probe_timeout(name: str) -> int:
    m = importlib.import_module("routes.brain_layer14_causal")
    for probe, _path, timeout in m._CONTEXT_PROBES:
        if probe == name:
            return timeout
    raise AssertionError("no %r probe in _CONTEXT_PROBES" % name)


def _l6_internal_budget() -> int:
    """Sum of the loopback timeouts _gather_predictions() spends before it
    returns. Read from the source so this guard tracks the real number rather
    than a copy of it — the copied-constant bug that produced the false DCPI
    alarm on the same day."""
    src = (_ROOT / "routes" / "brain_layer6_predictive.py").read_text(
        encoding="utf-8")
    body = src.split("def _gather_predictions")[1].split("\ndef ")[0]
    return sum(int(t) for t in re.findall(r"timeout=(\d+)", body))


def test_predictions_probe_outlasts_the_endpoints_own_budget():
    budget = _l6_internal_budget()
    assert budget > 0, "could not read _gather_predictions' loopback timeouts"
    assert _probe_timeout("predictions") > budget, (
        "L14 probes /api/v1/brain/predictions with %ds, but the endpoint spends "
        "up to %ds on its own nested loopback calls before returning. A healthy "
        "service cannot answer in time, and lane 1 reports it as an instrument "
        "failure." % (_probe_timeout("predictions"), budget))


def test_the_radar_probe_keeps_its_documented_headroom():
    """/api/v1/brain/consistency-radar cold-start was documented at ~20s in
    routes/health_json.py ('the brain radar cold-start can take 20s'), which is
    why health.json capped itself at 4s rather than wait for it. That file was
    deleted 2026-08-28 — all seven of its routes were shadowed by dchub-frontend
    static assets and never served — but the 20s cold start it measured is a
    property of the radar endpoint, not of the deleted caller, so this floor
    stays."""
    assert _probe_timeout("findings") >= 20


def test_every_probe_timeout_is_positive_and_bounded():
    """A missing timeout hangs the whole causal tick; an enormous one blows the
    cron budget. Both fail the same way — L14 stops producing."""
    m = importlib.import_module("routes.brain_layer14_causal")
    for probe, path, timeout in m._CONTEXT_PROBES:
        assert isinstance(timeout, int) and 0 < timeout <= 30, \
            "probe %r (%s) has an unusable timeout: %r" % (probe, path, timeout)
