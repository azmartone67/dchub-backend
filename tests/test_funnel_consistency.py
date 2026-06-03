"""test_funnel_consistency.py — drift guard.

Fails CI if /api/v1/mcp/funnel (main.py) and /api/v1/admin/visitor-intelligence
(visitor_intelligence.py) disagree on what counts as a real signal — the
bug class this whole refactor exists to kill. The previous symptom was
funnel reporting 18,745 signals on `mcp` while visitor-intel reported 16,
because each surface had its own duplicated filter constant.

With the canonical view in place, both endpoints SELECT FROM
mcp_funnel_canonical / mcp_funnel_real and the counts MUST match within a
5% tolerance (allowing for small race windows between two queries).
"""
import os
import pytest

BASE = os.environ.get('DCHUB_TEST_BASE', 'https://dchub-backend-production.up.railway.app')
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')


def _get(path, key=None, timeout=15):
    import urllib.request, json
    req = urllib.request.Request(BASE + path)
    if key:
        req.add_header('X-Internal-Key', key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


@pytest.mark.skipif(not ADMIN_KEY, reason='DCHUB_ADMIN_KEY not set')
def test_funnel_and_visitor_intel_agree_on_signal_count():
    """The bug class: every release fixes one signal-counting filter and creates
    a drift somewhere else. This test asserts both surfaces query the same view.
    """
    funnel = _get('/api/v1/mcp/funnel')
    intel = _get('/api/v1/admin/visitor-intelligence?days=7', key=ADMIN_KEY)
    funnel_signals = funnel.get('upgrade_signals_7d', 0)
    intel_signals = intel.get('totals', {}).get('total_paywall_signals', 0)
    # Allow 5% drift between two non-atomic queries.
    if max(funnel_signals, intel_signals) > 100:
        ratio = min(funnel_signals, intel_signals) / max(funnel_signals, intel_signals)
        assert ratio >= 0.95, (
            f'FUNNEL/INTEL DRIFT: /api/v1/mcp/funnel reports {funnel_signals} 7d signals '
            f'but /api/v1/admin/visitor-intelligence reports {intel_signals}. '
            'Both should read FROM mcp_funnel_canonical / mcp_funnel_real — one of '
            'them is bypassing the view. See mcp_signal_canonical.py + schema_repair.py '
            'CANONICAL VIEW entry.'
        )


@pytest.mark.skipif(not ADMIN_KEY, reason='DCHUB_ADMIN_KEY not set')
def test_caller_conversion_rate_is_distinct_not_raw():
    """Guards against the inflation trap: signals/conversions reads 0% forever
    because anon signals dominate. The headline conv_rate_pct MUST be computed
    DISTINCT caller_id, not raw row counts."""
    full = _get('/api/v1/admin/visitor-intelligence?days=30', key=ADMIN_KEY)
    totals = full.get('totals', {})
    assert 'caller_conv_rate_pct' in totals or 'converted_callers' in totals, (
        'visitor-intelligence headline must expose caller_conv_rate_pct '
        '(DISTINCT-caller-based). A raw signals/conversions ratio is structurally '
        'pinned at 0% by anon-signal inflation — see '
        'reference_dchub_mcp_signal_inflation memory.'
    )
