"""
DC Hub Stripe Webhook Fixes
============================
Replace the stripe_webhook function in main.py (around line 5222-5269)
with this fixed version.

BUGS FIXED:
1. request.get_data(as_text=True) → request.get_data() 
   Stripe's construct_event needs raw bytes, not decoded text.
   With as_text=True, signature verification can fail on payloads
   with special characters.

2. Added detailed logging so you can see exactly what's happening
   when a webhook arrives (email found, plan detected, rows updated).

3. Added a diagnostic endpoint at /api/stripe/webhook-test
   so you can verify the webhook is reachable without needing Stripe.

ALSO CHECK:
- In Stripe Dashboard → Webhooks, confirm the endpoint URL is:
  https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/stripe/webhook
- Check Event Deliveries tab for failures
- Make sure STRIPE_WEBHOOK_SECRET in Replit Secrets matches the signing
  secret shown in Stripe Dashboard for this webhook endpoint
"""


# =============================================================================
# REPLACEMENT: stripe_webhook function (replace lines 5222-5269 in main.py)
# =============================================================================

# AUTO-REPAIR: duplicate route '/api/stripe/webhook' also in main.py:7861 — review and remove one
@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    if not STRIPE_AVAILABLE:
        print("❌ Stripe webhook called but STRIPE_AVAILABLE is False")
        return jsonify({'error': 'Stripe not available'}), 503
    
    # FIX: Use raw bytes, NOT as_text=True
    # Stripe signature verification requires the raw payload bytes
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    print(f"💳 Webhook received - payload size: {len(payload)} bytes, sig present: {bool(sig_header)}")
    
    # Verify webhook signature if secret is configured
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            print(f"❌ Webhook error: Invalid payload - {e}")
            return jsonify({'error': 'Invalid payload'}), 400
        except stripe.error.SignatureVerificationError as e:
            print(f"❌ Webhook error: Invalid signature - {e}")
            print(f"   Hint: Make sure STRIPE_WEBHOOK_SECRET in Replit matches Stripe Dashboard")
            return jsonify({'error': 'Invalid signature'}), 400
        
        # construct_event returns a stripe.Event object
        event_type = event['type']
        data = event['data']['object']
    else:
        # Without webhook secret, parse event directly (less secure)
        print("⚠️ WARNING: No STRIPE_WEBHOOK_SECRET set - skipping signature verification")
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({'error': 'Invalid JSON'}), 400
        event_type = event.get('type', '')
        data = event.get('data', {}).get('object', {})
    
    print(f"💳 Stripe webhook event: {event_type}")
    
    # Handle different event types
    if event_type == 'checkout.session.completed':
        handle_checkout_completed(data)
    elif event_type == 'customer.subscription.created':
        handle_subscription_created(data)
    elif event_type == 'customer.subscription.updated':
        handle_subscription_updated(data)
    elif event_type == 'customer.subscription.deleted':
        handle_subscription_deleted(data)
    elif event_type == 'invoice.paid':
        handle_invoice_paid(data)
    elif event_type == 'invoice.payment_failed':
        handle_payment_failed(data)
    else:
        print(f"ℹ️ Unhandled webhook event type: {event_type}")
    
    return jsonify({'received': True})


# =============================================================================
# NEW: Diagnostic endpoint - ADD this to main.py (does NOT replace anything)
# =============================================================================
# AUTO-REPAIR: duplicate route '/api/stripe/webhook-test' also in main.py:8398 — review and remove one

@app.route('/api/stripe/webhook-test', methods=['GET'])
def stripe_webhook_test():
    """Diagnostic endpoint to verify Stripe webhook configuration.
    
    Access at: https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/stripe/webhook-test
    
    This checks:
    - Is Stripe library available?
    - Is STRIPE_SECRET_KEY set?
    - Is STRIPE_WEBHOOK_SECRET set?
    - Can we reach Stripe API?
    - How many users have been upgraded from free%s
    """
    import traceback
    
    checks = {
        'stripe_available': STRIPE_AVAILABLE,
        'stripe_secret_key_set': bool(STRIPE_SECRET_KEY),
        'stripe_webhook_secret_set': bool(STRIPE_WEBHOOK_SECRET),
        'stripe_publishable_key': STRIPE_PUBLISHABLE_KEY[:20] + '...' if STRIPE_PUBLISHABLE_KEY else 'NOT SET',
        'webhook_url': 'https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/stripe/webhook',
    }
    
    # Check database for subscription stats
    try:
        conn = get_db()
        try:
            c = conn.cursor()

            c.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan")
            plan_counts = {row[0]: row[1] for row in c.fetchall()}
            checks['user_plans'] = plan_counts

            c.execute("SELECT subscription_status, COUNT(*) FROM users WHERE subscription_status IS NOT NULL GROUP BY subscription_status")
            status_counts = {row[0]: row[1] for row in c.fetchall()}
            checks['subscription_statuses'] = status_counts

            c.execute("SELECT COUNT(*) FROM users WHERE stripe_customer_id IS NOT NULL AND stripe_customer_id != ''")
            checks['users_with_stripe_id'] = c.fetchone()[0]

            # Show recent upgrades
            c.execute("""
                SELECT email, plan, subscription_status, stripe_customer_id
                FROM users
                WHERE plan != 'free'
                ORDER BY created_at DESC
                LIMIT 10
            """)
            paid_users = []
            for row in c.fetchall():
                paid_users.append({
                    'email': row[0][:3] + '***' if row[0] else 'N/A',  # Partially mask
                    'plan': row[1],
                    'status': row[2],
                    'has_stripe_id': bool(row[3])
                })
            checks['recent_paid_users'] = paid_users

        finally:
            conn.close()
    except Exception as e:
        checks['db_error'] = str(e)
    
    # Test Stripe API connectivity
    if STRIPE_AVAILABLE and STRIPE_SECRET_KEY:
        try:
            # Quick API test - list recent events
            events = stripe.Event.list(limit=3, type='checkout.session.completed')
            checks['stripe_api_connected'] = True
            checks['recent_checkout_events'] = len(events.data)
            
            if events.data:
                latest = events.data[0]
                session = latest.data.object
                checks['latest_checkout'] = {
                    'created': datetime.fromtimestamp(latest.created).isoformat(),
                    'email': (session.get('customer_email') or 
                             session.get('customer_details', {}).get('email') or 'unknown')[:3] + '***',
                    'amount': session.get('amount_total', 0) / 100,
                    'payment_status': session.get('payment_status', 'unknown'),
                }
        except Exception as e:
            checks['stripe_api_connected'] = False
            checks['stripe_api_error'] = str(e)
    
    # Overall health
    checks['healthy'] = all([
        checks.get('stripe_available'),
        checks.get('stripe_secret_key_set'),
        checks.get('stripe_webhook_secret_set'),
    ])
    
    if not checks['healthy']:
        missing = []
        if not checks.get('stripe_available'):
            missing.append('stripe library not installed')
        if not checks.get('stripe_secret_key_set'):
            missing.append('STRIPE_SECRET_KEY not set in Replit Secrets')
        if not checks.get('stripe_webhook_secret_set'):
            missing.append('STRIPE_WEBHOOK_SECRET not set in Replit Secrets')
        checks['fix_needed'] = missing
    
    return jsonify(checks)
