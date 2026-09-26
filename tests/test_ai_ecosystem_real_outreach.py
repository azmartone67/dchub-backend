"""r-honest-outreach (2026-09-26): /api/ai-ecosystem/status reads real ledgers.

/ai showed "Recent Outreach Activity" 147 days stale and "10,021+ outreach
pings". Both came from outreach_to_ai_platforms(), which contacted nobody: it
appended canned notes for five platforms and counted each one. These tests pin
that the generator is gone and that the payload is read from the three ledgers
real outbound writes, with an idle lane reported as idle.
"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import ai_ecosystem_agent as eco  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0, 0)


class _Cur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def execute(self, sql, params=None):
        if 'mcp_registry_probe_state' in sql:
            self._rows = self.db.get('probe', [])
        elif 'FROM ai_lab_outreach_drafts' in sql and 'COUNT' in sql:
            self._rows = [self.db['lab_counts']]
        elif 'FROM ai_lab_outreach_drafts' in sql:
            self._rows = self.db.get('lab_rows', [])
        elif 'FROM ai_platform_submissions' in sql and 'COUNT' in sql:
            self._rows = [self.db['sub_counts']]
        elif 'FROM ai_platform_submissions' in sql:
            self._rows = self.db.get('sub_rows', [])
        else:
            raise AssertionError('unexpected SQL: ' + sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        if self.db.get('broken'):
            raise RuntimeError('pool exhausted')
        return _Cur(self.db)

    def close(self):
        pass


def _probe(days_ago, stale=False):
    at = (NOW - timedelta(days=days_ago)).replace(tzinfo=timezone.utc)
    return [(at, {
        'glama_ai': {'registry': 'Glama', 'verdict': 'present',
                     'copy': {'state': 'current'}},
        'awesome': {'registry': 'awesome-mcp-servers', 'verdict': 'present',
                    'copy': {'state': 'stale' if stale else 'current'}},
        'docker': {'registry': 'Docker MCP Catalog', 'verdict': 'http_404',
                   'actionable': True, 'copy': None},
        'cursor': {'registry': 'Cursor', 'verdict': 'redirected',
                   'actionable': False, 'copy': None},
    })]


def _db(**kw):
    db = {
        'probe': _probe(0.5, stale=True),
        # sent_all, sent_30d, queued, last_sent
        'lab_counts': (45, 0, 0, NOW - timedelta(days=28)),
        'lab_rows': [('perplexity', NOW - timedelta(days=28), 'delivered')],
        # total, last30, approved, last_submitted
        'sub_counts': (0, 0, 0, None),
        'sub_rows': [],
    }
    db.update(kw)
    return db


def _run(db):
    return eco.real_outreach_status(conn_factory=lambda: _Conn(db), now=NOW)


def _lane(out, lane_id):
    return next(l for l in out['lanes'] if l['id'] == lane_id)


def test_fake_generator_writes_nothing():
    a = eco.AIEcosystemAgent.__new__(eco.AIEcosystemAgent)
    a.state = {'outreach_log': [], 'total_outreach': 7, 'platforms_registered': []}
    assert a.outreach_to_ai_platforms() == []
    assert a.state == {'outreach_log': [], 'total_outreach': 7,
                       'platforms_registered': []}


def test_idle_lanes_are_idle_not_running():
    out = _run(_db())
    assert _lane(out, 'partner_email')['status'] == 'idle'
    assert 'nothing to send' in _lane(out, 'partner_email')['detail']
    assert _lane(out, 'self_registration')['status'] == 'idle'
    assert _lane(out, 'directories')['status'] == 'active'


def test_recent_activity_is_active():
    out = _run(_db(lab_counts=(46, 1, 2, NOW - timedelta(days=1)),
                   sub_counts=(3, 3, 1, NOW - timedelta(days=2))))
    assert _lane(out, 'partner_email')['status'] == 'active'
    assert _lane(out, 'self_registration')['status'] == 'active'


def test_stale_directory_scan_is_idle():
    out = _run(_db(probe=_probe(5)))
    assert _lane(out, 'directories')['status'] == 'idle'


def test_directory_summary_and_events():
    out = _run(_db())
    s = out['summary']
    assert (s['directories_tracked'], s['directories_listed'],
            s['directories_stale_copy'], s['directories_missing']) == (4, 2, 1, 1)
    notes = {e['platform']: e['notes'] for e in out['events']}
    assert notes['awesome-mcp-servers'] == 'Listed, copy out of date'
    assert notes['Docker MCP Catalog'].startswith('Not listed')
    assert 'Cursor' not in notes          # not actionable: not our outreach


def test_partner_events_carry_no_address_and_name_the_lab():
    out = _run(_db())
    mail = [e for e in out['events'] if e['channel'] == 'email']
    assert mail and mail[0]['platform'] == 'Perplexity'
    assert '@' not in repr(out)


def test_unapproved_self_registration_text_is_never_published():
    out = _run(_db(sub_counts=(2, 2, 1, NOW),
                   sub_rows=[('Acme Agents', NOW)]))
    regs = [e for e in out['events'] if e['channel'] == 'self_registration']
    assert [e['platform'] for e in regs] == ['Acme Agents']
    assert out['summary']['self_registrations'] == 2


def test_unreadable_ledger_is_unknown_not_running():
    out = eco.real_outreach_status(conn_factory=lambda: _Conn({'broken': True}),
                                   now=NOW)
    assert {l['status'] for l in out['lanes']} == {'unknown'}
    assert out['events'] == []


def test_events_newest_first():
    out = _run(_db())
    ts = [e['timestamp'] for e in out['events']]
    assert ts == sorted(ts, reverse=True)
