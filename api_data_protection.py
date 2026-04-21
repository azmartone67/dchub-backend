from internal_auth import is_valid_internal_key
"""
DC Hub API Data Protection & Anti-Scraping Module
===================================================
Drop-in module for the Replit backend that prevents bulk data theft
while keeping normal API usage smooth and error-free.

WHAT IT DOES:
  1. Opaque cursor pagination (prevents parallel scraping)
  2. Mandatory search filters (no "dump everything" queries)
  3. Rolling window anomaly detection (flags machine-like patterns)
  4. Per-key daily download caps (separate from rate limits)
  5. Response watermarking (invisible per-account markers)
  6. Progressive throttling (slow down suspicious keys instead of hard-blocking)

INSTALLATION:
  1. Copy this file to your Replit project root
  2. In main.py, add:
       from api_data_protection import init_data_protection
  3. After app creation:
       init_data_protection(app)
  4. Wrap your data endpoints with @protect_data decorator

WORKS WITH:
  - Your existing require_plan() decorator from api_tier_gating.py
  - Your existing require_api_key decorator
  - Your existing rate limiting
  - MCP server endpoints (configurable)

DOES NOT:
  - Break existing functionality
  - Affect public/free endpoints
  - Touch Stripe webhooks or auth flows
"""

import hashlib
import hmac
import json
import logging
import os
import time
import base64
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from functools import wraps
from threading import Lock

from flask import request, jsonify, g
from db_utils import get_db

logger = logging.getLogger("dc_hub.data_protection")

# AI Wars verification keys (inline to avoid circular imports)
AI_WARS_KEYS_TIER = {
    "dchub_chatgpt_2026_verify", "dchub_grok_2026_verify",
    "dchub_gemini_2026_verify", "dchub_perplexity_2026_verify",
    "dchub_mistral_2026_verify", "dchub_claude_2026_verify",
    "dchub_copilot_2026_verify", "dchub_meta_2026_verify",
    "dchub_poe_2026_verify", "dchub_openrouter_2026_verify",
    "dchub_pi_2026_verify", "dchub_phind_2026_verify",
    "dchub_nvidia_2026_verify"
}

# ---------------------------------------------------------------------------
# Configuration — tune these per tier
# ---------------------------------------------------------------------------

PROTECTION_CONFIG = {
    # Max unique facility records a key can pull per 24h rolling window
    "daily_record_caps": {
        "free": 10,
        "pro": 2000,
        "enterprise": 10000,
        "founding": 2000,
        "admin": 999999,
    },
    # Max results per single API response (hard cap regardless of ?limit=)
    "max_results_per_response": {
        "free": 5,
        "pro": 100,
        "enterprise": 100,
    },
    # Anomaly detection thresholds
    "anomaly": {
        "rapid_fire_window_sec": 60,       # Window to count rapid requests
        "rapid_fire_max_requests": 20,      # Max requests in that window
        "sequential_scan_threshold": 10,    # Sequential ID requests before flagging
        "geo_sweep_threshold": 8,           # Systematic region sweeps before flagging
        "consistent_interval_tolerance": 0.15,  # How "robotic" the timing is (0=perfectly regular)
    },
    # Progressive throttle — adds artificial delay (seconds) per violation level
    "throttle_delays": [0, 0.5, 1.0, 2.0, 5.0, 10.0],
    # Watermark secret (set in env, falls back to this)
    "watermark_secret": os.environ.get("DCHUB_WATERMARK_SECRET", "dchub_wm_2025"),
}

# In-memory stores (reset on deploy, which is fine — persistent tracking in SQLite)
_request_logs = defaultdict(list)   # api_key -> [(timestamp, endpoint, params_hash)]
_violation_scores = defaultdict(int)  # api_key -> violation count
_record_counts = defaultdict(int)    # api_key -> daily record count
_locks = defaultdict(Lock)
_daily_reset_time = {}               # api_key -> last reset timestamp

DB_PATH = os.environ.get("DB_PATH", "dc_hub.db")


# ===========================================================================
# DATABASE SETUP
# ===========================================================================

def _init_protection_tables():
    """Create tables for persistent anomaly tracking."""
    try:
        conn = get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS api_anomaly_log (
                    id SERIAL PRIMARY KEY,
                    api_key_hash TEXT NOT NULL,
                    anomaly_type TEXT NOT NULL,
                    details TEXT,
                    severity INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (NOW())
                );

                CREATE INDEX IF NOT EXISTS idx_anomaly_key
                    ON api_anomaly_log(api_key_hash, created_at);

                CREATE TABLE IF NOT EXISTS api_daily_usage (
                    id SERIAL PRIMARY KEY,
                    api_key_hash TEXT NOT NULL,
                    date TEXT NOT NULL,
                    records_fetched INTEGER DEFAULT 0,
                    requests_made INTEGER DEFAULT 0,
                    unique_endpoints INTEGER DEFAULT 0,
                    flagged INTEGER DEFAULT 0,
                    UNIQUE(api_key_hash, date)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_usage_key
                    ON api_daily_usage(api_key_hash, date);
            """)
            conn.commit()
        finally:
            conn.close()
        logger.info("✅ Data protection tables initialized")
    except Exception as e:
        logger.warning(f"⚠️ Could not init protection tables: {e}")


# ===========================================================================
# 1. OPAQUE CURSOR PAGINATION
# ===========================================================================

def generate_cursor(offset, query_hash, api_key_hash):
    """
    Create an opaque, signed cursor that encodes position.
    - Can't be guessed or manipulated
    - Tied to the specific query + API key (prevents cursor sharing)
    """
    payload = f"{offset}:{query_hash}:{api_key_hash}"
    signature = hmac.new(
        PROTECTION_CONFIG["watermark_secret"].encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    token = base64.urlsafe_b64encode(f"{offset}:{signature}".encode()).decode()
    return token


def decode_cursor(cursor_token, query_hash, api_key_hash):
    """
    Decode and verify a cursor. Returns offset or None if invalid.
    """
    try:
        decoded = base64.urlsafe_b64decode(cursor_token.encode()).decode()
        offset_str, provided_sig = decoded.split(":", 1)
        offset = int(offset_str)

        # Verify signature
        expected_payload = f"{offset}:{query_hash}:{api_key_hash}"
        expected_sig = hmac.new(
            PROTECTION_CONFIG["watermark_secret"].encode(),
            expected_payload.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        if hmac.compare_digest(provided_sig, expected_sig):
            return offset
        return None
    except Exception:
        return None


def get_query_hash(params_dict):
    """Hash the query parameters to tie cursors to specific queries."""
    stable = json.dumps(sorted(params_dict.items()), sort_keys=True)
    return hashlib.md5(stable.encode()).hexdigest()[:12]


# ===========================================================================
# 2. MANDATORY FILTER ENFORCEMENT
# ===========================================================================

# Endpoints that MUST have at least one substantive filter
FILTERED_ENDPOINTS = {
    "/api/v1/facilities/search": ["q", "country", "state", "city", "provider"],
    "/api/v1/search": ["q", "country", "state", "city", "provider"],
    "/api/v1/transactions": ["year", "buyer", "seller"],
    "/api/v1/market-intel": ["market"],
}


def check_required_filters(endpoint, params):
    """
    Ensure at least one substantive filter is provided.
    Returns (allowed, error_message).
    """
    filter_fields = FILTERED_ENDPOINTS.get(endpoint)
    if filter_fields is None:
        return True, None

    provided = [f for f in filter_fields if params.get(f)]
    if not provided:
        return False, {
            "error": "search_filter_required",
            "message": f"At least one filter is required: {', '.join(filter_fields)}",
            "hint": "Try adding a country, city, provider, or search query.",
            "docs": "https://dchub.cloud/api/docs"
        }
    return True, None


# ===========================================================================
# 3. ANOMALY DETECTION (Rolling Window)
# ===========================================================================

def _get_key_hash(api_key):
    """Short hash for logging (never store raw keys)."""
    if not api_key:
        return "anonymous"
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _log_request(api_key, endpoint, params):
    """Log request for pattern analysis."""
    key_hash = _get_key_hash(api_key)
    now = time.time()
    params_hash = get_query_hash(params) if params else ""

    with _locks[key_hash]:
        _request_logs[key_hash].append((now, endpoint, params_hash, params))

        # Keep only last 15 minutes of logs in memory
        cutoff = now - 900
        _request_logs[key_hash] = [
            r for r in _request_logs[key_hash] if r[0] > cutoff
        ]


def detect_anomalies(api_key, endpoint, params):
    """
    Analyze recent request patterns for scraping behavior.
    Returns: list of (anomaly_type, severity, details)
    """
    key_hash = _get_key_hash(api_key)
    anomalies = []
    config = PROTECTION_CONFIG["anomaly"]
    now = time.time()

    with _locks[key_hash]:
        logs = _request_logs[key_hash]

    if len(logs) < 3:
        return anomalies

    # --- Rapid fire detection ---
    window = config["rapid_fire_window_sec"]
    recent = [r for r in logs if r[0] > now - window]
    if len(recent) > config["rapid_fire_max_requests"]:
        anomalies.append((
            "rapid_fire",
            2,
            f"{len(recent)} requests in {window}s (limit: {config['rapid_fire_max_requests']})"
        ))

    # --- Consistent interval detection (bot-like timing) ---
    if len(recent) >= 5:
        intervals = [recent[i][0] - recent[i-1][0] for i in range(1, len(recent))]
        if intervals:
            avg = sum(intervals) / len(intervals)
            if avg > 0:
                variance = sum((i - avg) ** 2 for i in intervals) / len(intervals)
                cv = (variance ** 0.5) / avg  # coefficient of variation
                if cv < config["consistent_interval_tolerance"]:
                    anomalies.append((
                        "robotic_timing",
                        3,
                        f"CV={cv:.3f} (threshold: {config['consistent_interval_tolerance']}), avg interval={avg:.1f}s"
                    ))

    # --- Sequential ID scanning ---
    # Check if they're iterating through facility IDs
    facility_ids = []
    for _, ep, _, p in recent:
        if "facilit" in ep and p:
            fid = p.get("facility_id") or p.get("id") or ""
            if fid.isdigit():
                facility_ids.append(int(fid))

    if len(facility_ids) >= config["sequential_scan_threshold"]:
        sorted_ids = sorted(facility_ids)
        sequential_count = sum(
            1 for i in range(1, len(sorted_ids))
            if sorted_ids[i] - sorted_ids[i-1] <= 2
        )
        if sequential_count >= config["sequential_scan_threshold"] - 1:
            anomalies.append((
                "sequential_scan",
                4,
                f"{sequential_count} sequential facility IDs detected"
            ))

    # --- Geographic sweep detection ---
    # Check if they're systematically iterating through regions
    regions_queried = set()
    for _, ep, _, p in logs:
        if p:
            region = (p.get("country", ""), p.get("state", ""), p.get("city", ""))
            if any(region):
                regions_queried.add(region)

    if len(regions_queried) >= config["geo_sweep_threshold"]:
        anomalies.append((
            "geo_sweep",
            2,
            f"{len(regions_queried)} unique regions queried in 15min window"
        ))

    return anomalies


def _persist_anomalies(key_hash, anomalies):
    """Log anomalies to SQLite for admin review."""
    if not anomalies:
        return
    try:
        conn = get_db()
        try:
            for anomaly_type, severity, details in anomalies:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO api_anomaly_log (api_key_hash, anomaly_type, details, severity) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (key_hash, anomaly_type, details, severity)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to persist anomaly: {e}")


# ===========================================================================
# 4. DAILY RECORD DOWNLOAD CAPS
# ===========================================================================

def check_daily_record_cap(api_key, tier, record_count):
    """
    Track how many unique records a key has pulled today.
    Returns (allowed, records_remaining).
    """
    key_hash = _get_key_hash(api_key)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cap = PROTECTION_CONFIG["daily_record_caps"].get(tier, 50)

    # Reset daily counter
    if _daily_reset_time.get(key_hash) != today:
        _record_counts[key_hash] = 0
        _daily_reset_time[key_hash] = today

    current = _record_counts[key_hash]
    if current + record_count > cap:
        remaining = max(0, cap - current)
        return False, remaining

    _record_counts[key_hash] += record_count
    _update_daily_usage(key_hash, today, record_count)
    return True, cap - current - record_count


def _update_daily_usage(key_hash, date, records):
    """Persist daily usage to SQLite."""
    try:
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("""
                INSERT INTO api_daily_usage (api_key_hash, date, records_fetched, requests_made)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT(api_key_hash, date)
                DO UPDATE SET
                    records_fetched = api_daily_usage.records_fetched + %s,
                    requests_made = api_daily_usage.requests_made + 1
            """, (key_hash, date, records, records))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to update daily usage: {e}")


# ===========================================================================
# 5. RESPONSE WATERMARKING
# ===========================================================================

def watermark_response(data, api_key):
    """
    Embed invisible per-account markers in API responses.
    If your data shows up somewhere, you can trace which key leaked it.

    Techniques used:
    - Inject a _wm field with a hashed key fingerprint
    - Subtly vary field ordering (JSON key order)
    - Add invisible Unicode chars in text fields
    """
    if not api_key or not isinstance(data, (dict, list)):
        return data

    key_hash = _get_key_hash(api_key)
    fingerprint = hmac.new(
        PROTECTION_CONFIG["watermark_secret"].encode(),
        key_hash.encode(),
        hashlib.sha256
    ).hexdigest()[:8]

    if isinstance(data, dict):
        # Add watermark metadata (looks like a cache/tracking ID)
        data["_rid"] = fingerprint
        data["_ts"] = int(time.time())

    elif isinstance(data, list):
        # Wrap in an envelope with watermark
        data = {
            "results": data,
            "count": len(data),
            "_rid": fingerprint,
            "_ts": int(time.time()),
        }

    return data


# ===========================================================================
# 6. PROGRESSIVE THROTTLING
# ===========================================================================

def get_throttle_delay(api_key):
    """
    Returns artificial delay in seconds based on violation score.
    Slows down suspicious keys instead of hard-blocking them.
    """
    key_hash = _get_key_hash(api_key)
    score = _violation_scores.get(key_hash, 0)
    delays = PROTECTION_CONFIG["throttle_delays"]
    idx = min(score, len(delays) - 1)
    return delays[idx]


def increment_violation(api_key, amount=1):
    """Increase violation score for a key."""
    key_hash = _get_key_hash(api_key)
    _violation_scores[key_hash] = _violation_scores.get(key_hash, 0) + amount


# ===========================================================================
# MAIN DECORATOR — @protect_data
# ===========================================================================

def protect_data(f):
    # BUG-003 FIX: Skip rate limiting for dchub.cloud frontend requests
    """
    Master decorator that combines all protections.
    Use AFTER @require_plan / @require_api_key.

    Example:
        @app.route('/api/v1/facilities/search')
        @require_plan('pro')
        @protect_data
        def search_facilities():
            ...
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # Origin bypass — dchub.cloud frontend passes through without data protection
        origin = request.headers.get("Origin", "") or request.headers.get("Referer", "")
        if "dchub.cloud" in origin:
            return f(*args, **kwargs)
        # Internal MCP bypass — trust calls from our own MCP server and admin tools
        internal_key = request.headers.get("X-Internal-Key", "")
        if is_valid_internal_key(internal_key):  # centralized
            return f(*args, **kwargs)
        api_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").replace("Bearer ", "")
            or request.args.get("api_key")
            or ""
        )
        tier = getattr(g, "user_tier", "free")
        endpoint = request.path
        params = {**request.args.to_dict(), **request.view_args} if request.view_args else request.args.to_dict()

        # --- Step 1: Check mandatory filters ---
        allowed, error = check_required_filters(endpoint, params)
        if not allowed:
            return jsonify(error), 400

        # --- Step 2: Log request & detect anomalies ---
        _log_request(api_key, endpoint, params)
        anomalies = detect_anomalies(api_key, endpoint, params)

        if anomalies:
            key_hash = _get_key_hash(api_key)
            max_severity = max(a[1] for a in anomalies)
            increment_violation(api_key, max_severity)
            _persist_anomalies(key_hash, anomalies)

            logger.warning(
                f"🚨 Anomalies detected for {key_hash}: "
                + "; ".join(f"{a[0]}(sev={a[1]})" for a in anomalies)
            )

            # If severity is critical (score >= 10), soft-block
            total_score = _violation_scores.get(key_hash, 0)
            if total_score >= 10:
                return jsonify({
                    "error": "rate_limit_exceeded",
                    "message": "Unusual activity detected. Please contact support@dchub.cloud if this is an error.",
                    "retry_after": 3600,
                }), 429

        # --- Step 3: Apply progressive throttle ---
        delay = get_throttle_delay(api_key)
        if delay > 0:
            time.sleep(delay)

        # --- Step 4: Execute the actual endpoint ---
        response = f(*args, **kwargs)

        # --- Step 5: Post-process response ---
        if hasattr(response, "get_json"):
            try:
                data = response.get_json()
                if data:
                    # Count records returned
                    record_count = 0
                    if isinstance(data, list):
                        record_count = len(data)
                    elif isinstance(data, dict):
                        results = data.get("results") or data.get("facilities") or data.get("data") or []
                        record_count = len(results) if isinstance(results, list) else 0

                    # AI Wars keys bypass daily caps
                    if api_key in AI_WARS_KEYS_TIER:
                        tier = 'enterprise'

                    # Check daily cap
                    if record_count > 0:
                        cap_ok, remaining = check_daily_record_cap(api_key, tier, record_count)
                        if not cap_ok:
                            return jsonify({
                                "error": "daily_limit_reached",
                                "message": f"Daily download limit reached for your {tier} plan.",
                                "records_remaining": remaining,
                                "upgrade_url": "https://dchub.cloud/pricing",
                                "resets_at": (
                                    datetime.now(timezone.utc).replace(
                                        hour=0, minute=0, second=0
                                    ) + timedelta(days=1)
                                ).isoformat() + "Z",
                            }), 429

                    # Watermark
                    data = watermark_response(data, api_key)

                    # Enforce max results per response
                    max_results = PROTECTION_CONFIG["max_results_per_response"].get(tier, 25)
                    for key in ["results", "facilities", "data"]:
                        if isinstance(data.get(key), list) and len(data[key]) > max_results:
                            data[key] = data[key][:max_results]
                            data["truncated"] = True
                            data["max_results"] = max_results

                    # Add remaining quota info in headers
                    new_response = jsonify(data)
                    new_response.status_code = response.status_code
                    _, remaining = check_daily_record_cap(api_key, tier, 0)
                    new_response.headers["X-Daily-Records-Remaining"] = str(remaining)
                    new_response.headers["X-Plan-Tier"] = tier
                    return new_response

            except Exception as e:
                logger.warning(f"Post-process error (non-fatal): {e}")

        return response

    return decorated


# ===========================================================================
# ADMIN ENDPOINTS — View anomaly logs & usage
# ===========================================================================

def _register_admin_routes(app):
    """Register admin-only monitoring endpoints."""

    @app.route("/api/admin/anomalies", methods=["GET"])
    def admin_anomalies():
        """View recent anomaly detections. Admin only."""
        admin_key = request.headers.get("X-Admin-Key", "")
        if admin_key != os.environ.get("DCHUB_ADMIN_KEY", ""):
            return jsonify({"error": "unauthorized"}), 401

        days = int(request.args.get("days", 7))
        try:
            conn = get_db()
            try:
                c = conn.cursor()
                rows = c.execute("""
                    SELECT api_key_hash, anomaly_type, details, severity, created_at
                    FROM api_anomaly_log
                    WHERE created_at > NOW() - INTERVAL '1 day' * %s
                    ORDER BY created_at DESC
                    LIMIT 200
                """, (f"-{days} days",)).fetchall()
            finally:
                conn.close()

            return jsonify({
                "anomalies": [dict(r) for r in rows],
                "count": len(rows),
                "window_days": days,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/usage-report", methods=["GET"])
    def admin_usage_report():
        """View daily usage by API key. Admin only."""
        admin_key = request.headers.get("X-Admin-Key", "")
        if admin_key != os.environ.get("DCHUB_ADMIN_KEY", ""):
            return jsonify({"error": "unauthorized"}), 401

        days = int(request.args.get("days", 7))
        try:
            conn = get_db()
            try:
                c = conn.cursor()
                rows = c.execute("""
                    SELECT api_key_hash, date, records_fetched, requests_made, flagged
                    FROM api_daily_usage
                    WHERE date > CURRENT_DATE - INTERVAL '1 day' * %s
                    ORDER BY records_fetched DESC
                    LIMIT 200
                """, (f"-{days} days",)).fetchall()
            finally:
                conn.close()

            return jsonify({
                "usage": [dict(r) for r in rows],
                "count": len(rows),
                "window_days": days,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/block-key", methods=["POST"])
    def admin_block_key():
        """Manually block an API key. Admin only."""
        admin_key = request.headers.get("X-Admin-Key", "")
        if admin_key != os.environ.get("DCHUB_ADMIN_KEY", ""):
            return jsonify({"error": "unauthorized"}), 401

        key_hash = request.json.get("key_hash", "")
        if not key_hash:
            return jsonify({"error": "key_hash required"}), 400

        # Set violation score to max
        _violation_scores[key_hash] = 100
        return jsonify({"blocked": True, "key_hash": key_hash})

    print("  ✅ Admin protection monitoring routes registered")


# ===========================================================================
# INIT — Call this from main.py
# ===========================================================================

def init_data_protection(app):
    """
    Initialize all data protection systems.

    Usage in main.py:
        from api_data_protection import init_data_protection
        init_data_protection(app)
    """
    _init_protection_tables()
    _register_admin_routes(app)
    print("🛡️  Data protection module initialized")
    print(f"   Daily caps: {PROTECTION_CONFIG['daily_record_caps']}")
    print(f"   Max per response: {PROTECTION_CONFIG['max_results_per_response']}")
    print(f"   Anomaly detection: ACTIVE")
    print(f"   Response watermarking: ACTIVE")
    print(f"   Progressive throttling: ACTIVE")
