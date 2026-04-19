"""
NEON HEALTH MONITOR — Detects DB failures, hostname changes, latency spikes.
Sends email alert via SendGrid. Register as Flask blueprint.
  GET  /api/health/neon       — Public health check
  POST /api/jobs/neon-health  — Cron job with alerting (requires DCHUB_ADMIN_KEY)
"""
import os, time, json, logging, threading
from datetime import datetime, timezone

logger = logging.getLogger('neon_health')

_state = {
    'consecutive_failures': 0, 'last_success': None, 'last_failure': None,
    'last_hostname': None, 'last_latency_ms': None, 'alert_sent_at': None,
    'hostname_change_alerted': False, 'total_checks': 0, 'total_failures': 0,
}

ALERT_COOLDOWN_SECONDS = 1800
FAILURE_THRESHOLD = 3
LATENCY_CRITICAL_MS = 2000

def _send_alert(subject, body_html):
    sg_key = os.environ.get('SENDGRID_API_KEY', '')
    admin_email = os.environ.get('ADMIN_ALERT_EMAIL', 'jonathan@dchub.cloud')
    if not sg_key:
        return False
    if _state['alert_sent_at'] and (time.time() - _state['alert_sent_at']) < ALERT_COOLDOWN_SECONDS:
        return False
    try:
        import urllib.request
        payload = json.dumps({
            "personalizations": [{"to": [{"email": admin_email}]}],
            "from": {"email": os.environ.get('SENDGRID_FROM_EMAIL', 'info@dchub.cloud'), "name": "DC Hub Alerts"},
            "subject": subject,
            "content": [{"type": "text/html", "value": body_html}]
        }).encode('utf-8')
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=payload, method='POST')
        req.add_header('Authorization', f'Bearer {sg_key}')
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=10)
        _state['alert_sent_at'] = time.time()
        logger.info("NEON HEALTH: Alert sent to %s", admin_email)
        return True
    except Exception as e:
        logger.error("NEON HEALTH: Alert send failed: %s", e)
        return False

def check_neon_health():
    import psycopg2
    db_url = os.environ.get('DATABASE_URL', '') or os.environ.get('NEON_DATABASE_URL', '')
    if not db_url:
        return {'status': 'error', 'message': 'No DATABASE_URL configured'}
    _state['total_checks'] += 1
    result = {'timestamp': datetime.now(timezone.utc).isoformat(), 'status': 'unknown', 'latency_ms': None, 'hostname': None}
    try:
        from urllib.parse import urlparse
        current_hostname = urlparse(db_url).hostname
        result['hostname'] = current_hostname
    except Exception:
        current_hostname = None
    if current_hostname and _state['last_hostname'] and current_hostname != _state['last_hostname']:
        result['hostname_changed'] = True
        result['previous_hostname'] = _state['last_hostname']
        if not _state['hostname_change_alerted']:
            _send_alert('🚨 DC Hub: Neon Hostname Changed',
                f"<h2>⚠️ Neon Hostname Change</h2><p>Previous: {_state['last_hostname']}</p><p>Current: {current_hostname}</p>")
            _state['hostname_change_alerted'] = True
    else:
        _state['hostname_change_alerted'] = False
    _state['last_hostname'] = current_hostname
    conn = None
    try:
        start = time.time()
        conn = psycopg2.connect(db_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT version(), inet_server_addr()")
        row = cur.fetchone()
        latency_ms = round((time.time() - start) * 1000, 1)
        cur.close(); conn.close(); conn = None
        result['status'] = 'healthy'
        result['latency_ms'] = latency_ms
        result['server_ip'] = str(row[1]) if row and row[1] else None
        _state['consecutive_failures'] = 0
        _state['last_success'] = time.time()
        _state['last_latency_ms'] = latency_ms
        if latency_ms > LATENCY_CRITICAL_MS:
            result['status'] = 'degraded'
            _send_alert(f'⚠️ DC Hub: Neon Latency {latency_ms}ms',
                f"<p>Latency: {latency_ms}ms (critical threshold: {LATENCY_CRITICAL_MS}ms)</p>")
    except Exception as e:
        if conn:
            try: conn.close()
            except: pass
        _state['consecutive_failures'] += 1
        _state['total_failures'] += 1
        _state['last_failure'] = time.time()
        result['status'] = 'unhealthy'
        result['error'] = str(e)[:500]
        result['consecutive_failures'] = _state['consecutive_failures']
        if _state['consecutive_failures'] >= FAILURE_THRESHOLD:
            _send_alert(f"🚨 DC Hub: Neon DOWN ({_state['consecutive_failures']} failures)",
                f"<h2>🔴 Neon Unreachable</h2><p>Failures: {_state['consecutive_failures']}</p><p>Error: {str(e)[:300]}</p>")
    result['state'] = {
        'total_checks': _state['total_checks'], 'total_failures': _state['total_failures'],
        'last_success_ago_s': round(time.time() - _state['last_success']) if _state['last_success'] else None,
    }
    return result

def register_neon_health_routes(app):
    from flask import jsonify, request
    @app.route('/api/health/neon', methods=['GET'])
    def neon_health_endpoint():
        result = check_neon_health()
        return jsonify(result), 200 if result['status'] == 'healthy' else 503
    @app.route('/api/jobs/neon-health', methods=['POST'])
    def job_neon_health():
        provided = request.headers.get('X-Admin-Key', '') or request.args.get('admin_key', '') or request.args.get('key', '')
        expected = os.environ.get('DCHUB_ADMIN_KEY', '')
        if not provided or not expected or provided.strip() != expected.strip():
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        result = check_neon_health()
        return jsonify({'success': True, 'job': 'neon-health', 'result': result})
    logger.info("✅ Neon Health Monitor registered: /api/health/neon, /api/jobs/neon-health")
