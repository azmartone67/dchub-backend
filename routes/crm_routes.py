"""
DC Hub CRM Routes
Add to main.py or register as a blueprint.

Pattern: late-binding decorator injection (matches existing extract pattern)

Usage in main.py:
    from routes.crm_routes import register_crm_routes
    register_crm_routes(app, get_db_connection, require_admin)
"""

from flask import jsonify, request
from datetime import datetime, timezone
import traceback


def register_crm_routes(app, get_db_connection, require_admin):
    """Register CRM admin routes with late-binding dependencies."""

    @app.route('/api/admin/crm/customers', methods=['GET'])
    @require_admin
    def crm_get_customers():
        """Return all real customers with CRM fields for the dashboard."""
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("""
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    u.plan,
                    u.company,
                    u.lifecycle_stage,
                    u.last_touched_at,
                    u.created_at,
                    u.tags,
                    u.notes,
                    u.stripe_customer_id,
                    EXTRACT(DAY FROM NOW() - COALESCE(u.last_touched_at, u.created_at::timestamptz))::int AS days_since_touch,
                    (SELECT COUNT(*) FROM customer_touchpoints t WHERE t.user_id = u.id) AS total_touches,
                    (SELECT t.subject FROM customer_touchpoints t WHERE t.user_id = u.id ORDER BY t.created_at DESC LIMIT 1) AS last_touch_subject,
                    (SELECT t.follow_up_date::text FROM customer_touchpoints t
                     WHERE t.user_id = u.id AND NOT t.follow_up_done AND t.follow_up_date IS NOT NULL
                     ORDER BY t.follow_up_date LIMIT 1) AS next_follow_up
                FROM users u
                WHERE u.email NOT LIKE '%%test%%'
                  AND u.email NOT LIKE '%%example.com'
                  AND u.email NOT LIKE '%%@dchub.cloud'
                  AND u.email NOT LIKE '%%azmartone%%'
                  AND u.email NOT LIKE '%%nicotest%%'
                  AND u.email NOT LIKE '%%nicomartone%%'
                  AND u.email != 'nico@dchub.cloud'
                  AND u.name NOT ILIKE '%%test%%'
                  AND u.name NOT ILIKE '%%sendgrid%%'
                  AND u.name NOT ILIKE '%%postfix%%'
                  AND COALESCE(u.name, '') != ''
                ORDER BY
                  CASE WHEN u.plan IN ('pro', 'founding') THEN 0 ELSE 1 END,
                  COALESCE(u.last_touched_at, u.created_at::timestamptz) ASC
            """)

            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            customers = []
            for row in rows:
                c = dict(zip(columns, row))
                # Serialize datetimes
                for k in ['last_touched_at', 'created_at']:
                    if c.get(k) and hasattr(c[k], 'isoformat'):
                        c[k] = c[k].isoformat()
                customers.append(c)

            cur.close()
            conn.close()

            return jsonify({'customers': customers, 'total': len(customers)})

        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/admin/crm/touchpoints/<user_id>', methods=['GET'])
    @require_admin
    def crm_get_touchpoints(user_id):
        """Get all touchpoints for a specific user, newest first."""
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("""
                SELECT id, touch_type, subject, details, channel, outcome,
                       follow_up_date::text, follow_up_done, created_at, created_by
                FROM customer_touchpoints
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 50
            """, (user_id,))

            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            touchpoints = []
            for row in rows:
                t = dict(zip(columns, row))
                if t.get('created_at') and hasattr(t['created_at'], 'isoformat'):
                    t['created_at'] = t['created_at'].isoformat()
                touchpoints.append(t)

            cur.close()
            conn.close()

            return jsonify({'touchpoints': touchpoints})

        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/admin/crm/touchpoint', methods=['POST'])
    @require_admin
    def crm_log_touchpoint():
        """Log a new touchpoint for a customer."""
        try:
            data = request.get_json()
            user_id = data.get('user_id')
            touch_type = data.get('touch_type')
            subject = data.get('subject')

            if not all([user_id, touch_type, subject]):
                return jsonify({'error': 'user_id, touch_type, and subject are required'}), 400

            conn = get_db_connection()
            cur = conn.cursor()

            # Insert touchpoint
            cur.execute("""
                INSERT INTO customer_touchpoints
                    (user_id, touch_type, subject, details, channel, outcome, follow_up_date, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'jonathan')
                RETURNING id
            """, (
                user_id,
                touch_type,
                subject,
                data.get('details'),
                data.get('channel', 'email'),
                data.get('outcome'),
                data.get('follow_up_date') or None,
            ))
            tp_id = cur.fetchone()[0]

            # Update lifecycle stage if requested
            new_stage = data.get('new_stage')
            if new_stage:
                cur.execute("""
                    UPDATE users SET lifecycle_stage = %s WHERE id = %s
                """, (new_stage, user_id))

            # Mark previous follow-ups as done if this is a follow-up completion
            if touch_type in ('email_reply', 'call', 'meeting', 'demo_completed'):
                cur.execute("""
                    UPDATE customer_touchpoints
                    SET follow_up_done = TRUE
                    WHERE user_id = %s AND NOT follow_up_done AND follow_up_date <= CURRENT_DATE AND id != %s
                """, (user_id, tp_id))

            conn.commit()
            cur.close()
            conn.close()

            return jsonify({'success': True, 'touchpoint_id': tp_id})

        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/admin/crm/customer/<user_id>', methods=['PATCH'])
    @require_admin
    def crm_update_customer(user_id):
        """Update customer fields (company, notes, tags, lifecycle_stage)."""
        try:
            data = request.get_json()
            allowed = {'company', 'notes', 'tags', 'lifecycle_stage', 'title', 'lead_source'}
            updates = {k: v for k, v in data.items() if k in allowed}

            if not updates:
                return jsonify({'error': 'No valid fields to update'}), 400

            conn = get_db_connection()
            cur = conn.cursor()

            set_clauses = []
            values = []
            for k, v in updates.items():
                set_clauses.append(f"{k} = %s")
                values.append(v)
            values.append(user_id)

            cur.execute(f"""
                UPDATE users SET {', '.join(set_clauses)} WHERE id = %s
            """, values)

            conn.commit()
            cur.close()
            conn.close()

            return jsonify({'success': True, 'updated': list(updates.keys())})

        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    print("[CRM] 4 CRM admin routes registered")
