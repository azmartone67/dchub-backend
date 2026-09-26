# backend/api/decisions.py
# Drop this into your Replit backend

from flask import Blueprint, jsonify, request
from datetime import datetime, timedelta
from functools import wraps
import uuid
import json
import os

decisions_bp = Blueprint('decisions', __name__)

# Simple file-based storage (replace with your DB later)
DECISIONS_FILE = 'data/decisions.json'

def ensure_data_dir():
    os.makedirs('data', exist_ok=True)
    if not os.path.exists(DECISIONS_FILE):
        with open(DECISIONS_FILE, 'w') as f:
            json.dump([], f)

def load_decisions():
    ensure_data_dir()
    with open(DECISIONS_FILE, 'r') as f:
        return json.load(f)

def save_decisions(decisions):
    ensure_data_dir()
    with open(DECISIONS_FILE, 'w') as f:
        json.dump(decisions, f, indent=2, default=str)

def rate_limit(limit=100, per=60):
    """Simple rate limit decorator - replace with your implementation."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapper
    return decorator


@decisions_bp.route('/api/v1/decisions', methods=['POST'])
@rate_limit(limit=100, per=60)
def log_decision():
    """
    Log a new decision.
    
    Request body:
    {
        "title": "Added rate limiting to facility API",
        "description": "Implemented 100 req/min limit to prevent abuse",
        "category": "api",
        "type": "feature",
        "priority": "high",
        "effort_estimate": "small",
        "affected_files": ["backend/api/facilities.py"],
        "technologies": ["python", "flask"],
        "source": "manual"
    }
    """
    data = request.json
    
    # Validate required fields
    required = ['title', 'category', 'type']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'success': False, 'error': f'Missing fields: {missing}'}), 400
    
    # Valid values
    valid_categories = ['frontend', 'backend', 'api', 'database', 'map', 'seo', 'ux', 'infra', 'mobile', 'other']
    valid_types = ['feature', 'bugfix', 'refactor', 'design', 'config', 'reject']
    valid_priorities = ['critical', 'high', 'medium', 'low']
    valid_efforts = ['trivial', 'small', 'medium', 'large', 'epic']
    
    if data['category'] not in valid_categories:
        return jsonify({'success': False, 'error': f'Invalid category. Use: {valid_categories}'}), 400
    if data['type'] not in valid_types:
        return jsonify({'success': False, 'error': f'Invalid type. Use: {valid_types}'}), 400
    
    decision = {
        'id': str(uuid.uuid4()),
        'title': data['title'],
        'description': data.get('description'),
        'category': data['category'],
        'type': data['type'],
        'priority': data.get('priority', 'medium'),
        'effort_estimate': data.get('effort_estimate'),
        'status': 'pending',
        'outcome': None,
        'outcome_notes': None,
        'impact_score': None,
        'user_feedback': None,
        'affected_files': data.get('affected_files', []),
        'technologies': data.get('technologies', []),
        'source': data.get('source', 'manual'),
        'decided_at': datetime.utcnow().isoformat(),
        'completed_at': None,
        'decided_by': 'jonathan',
        'metadata': data.get('metadata', {})
    }
    
    decisions = load_decisions()
    decisions.append(decision)
    save_decisions(decisions)
    
    return jsonify({
        'success': True,
        'id': decision['id'],
        'message': 'Decision logged'
    }), 201


@decisions_bp.route('/api/v1/decisions/<decision_id>', methods=['PATCH'])
@rate_limit(limit=100, per=60)
def update_decision(decision_id):
    """
    Update decision status/outcome.
    
    Request body:
    {
        "status": "completed",
        "outcome": "success",
        "outcome_notes": "Reduced API abuse by 90%",
        "impact_score": 8,
        "user_feedback": "positive"
    }
    """
    decisions = load_decisions()
    
    decision = next((d for d in decisions if d['id'] == decision_id), None)
    if not decision:
        return jsonify({'success': False, 'error': 'Decision not found'}), 404
    
    data = request.json
    
    # Updateable fields
    updateable = [
        'status', 'outcome', 'outcome_notes', 'impact_score',
        'user_feedback', 'description', 'priority'
    ]
    
    for field in updateable:
        if field in data:
            decision[field] = data[field]
    
    # Auto-set completed_at when marked complete
    if data.get('status') == 'completed' and not decision.get('completed_at'):
        decision['completed_at'] = datetime.utcnow().isoformat()
    
    save_decisions(decisions)
    
    return jsonify({
        'success': True,
        'message': 'Decision updated'
    })


# AUTO-REPAIR: duplicate route '/api/v1/decisions' also in decisions.py:42 — review and remove one
@decisions_bp.route('/api/v1/decisions', methods=['GET'])
@rate_limit(limit=100, per=60)
def list_decisions():
    """
    List decisions with filtering.
    
    Query params:
    - category: Filter by category
    - type: Filter by type
    - status: Filter by status
    - outcome: Filter by outcome
    - since: ISO date for decisions after this date
    - limit: Max results (default 50)
    """
    decisions = load_decisions()
    
    # Apply filters
    if category := request.args.get('category'):
        decisions = [d for d in decisions if d['category'] == category]
    if type_ := request.args.get('type'):
        decisions = [d for d in decisions if d['type'] == type_]
    if status := request.args.get('status'):
        decisions = [d for d in decisions if d['status'] == status]
    if outcome := request.args.get('outcome'):
        decisions = [d for d in decisions if d['outcome'] == outcome]
    if since := request.args.get('since'):
        decisions = [d for d in decisions if d['decided_at'] >= since]
    
    # Sort by date descending
    decisions = sorted(decisions, key=lambda d: d['decided_at'], reverse=True)
    
    # Pagination
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    
    paginated = decisions[offset:offset + limit]
    
    return jsonify({
        'success': True,
        'data': paginated,
        'meta': {
            'total': len(decisions),
            'count': len(paginated),
            'offset': offset,
            'limit': limit
        }
    })


@decisions_bp.route('/api/v1/decisions/analytics', methods=['GET'])
@rate_limit(limit=50, per=60)
def decision_analytics():
    """
    Get analytics and patterns from decisions.
    """
    decisions = load_decisions()
    
    # Time range
    days = int(request.args.get('days', 30))
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    recent = [d for d in decisions if d['decided_at'] >= cutoff]
    completed = [d for d in recent if d['status'] == 'completed']
    
    # Category breakdown
    category_stats = {}
    for d in recent:
        cat = d['category']
        if cat not in category_stats:
            category_stats[cat] = {'total': 0, 'successes': 0}
        category_stats[cat]['total'] += 1
        if d['outcome'] == 'success':
            category_stats[cat]['successes'] += 1
    
    # Type breakdown
    type_stats = {}
    for d in completed:
        t = d['type']
        if t not in type_stats:
            type_stats[t] = {'total': 0, 'impact_sum': 0, 'impact_count': 0}
        type_stats[t]['total'] += 1
        if d.get('impact_score'):
            type_stats[t]['impact_sum'] += d['impact_score']
            type_stats[t]['impact_count'] += 1
    
    # Technology frequency
    tech_frequency = {}
    for d in recent:
        for tech in (d.get('technologies') or []):
            tech_frequency[tech] = tech_frequency.get(tech, 0) + 1
    
    # Top successes
    successes = [d for d in completed if d['outcome'] == 'success']
    successes.sort(key=lambda x: x.get('impact_score') or 0, reverse=True)
    
    # Recent failures
    failures = [d for d in completed if d['outcome'] == 'failure']
    failures.sort(key=lambda x: x['decided_at'], reverse=True)
    
    return jsonify({
        'success': True,
        'period_days': days,
        'summary': {
            'total_decisions': len(recent),
            'completed': len(completed),
            'pending': len([d for d in recent if d['status'] == 'pending']),
            'in_progress': len([d for d in recent if d['status'] == 'in_progress'])
        },
        'categories': [
            {
                'name': cat,
                'total': stats['total'],
                'successes': stats['successes'],
                'success_rate': round(stats['successes'] / stats['total'] * 100, 1) if stats['total'] > 0 else 0
            }
            for cat, stats in sorted(category_stats.items(), key=lambda x: x[1]['total'], reverse=True)
        ],
        'types': [
            {
                'name': t,
                'total': stats['total'],
                'avg_impact': round(stats['impact_sum'] / stats['impact_count'], 1) if stats['impact_count'] > 0 else None
            }
            for t, stats in sorted(type_stats.items(), key=lambda x: x[1]['total'], reverse=True)
        ],
        'top_successes': successes[:5],
        'recent_failures': failures[:5],
        'technology_frequency': sorted(
            [{'tech': k, 'count': v} for k, v in tech_frequency.items()],
            key=lambda x: x['count'],
            reverse=True
        )[:10]
    })


@decisions_bp.route('/api/v1/decisions/context', methods=['GET'])
@rate_limit(limit=50, per=60)
def get_agent_context():
    """
    Get full context for AI agent consumption.
    Returns structured data the agent can use for learning.
    """
    decisions = load_decisions()
    
    # Recent decisions
    recent = sorted(decisions, key=lambda d: d['decided_at'], reverse=True)[:20]
    
    # Successful patterns
    successes = [d for d in decisions if d['outcome'] == 'success' and d.get('impact_score', 0) >= 7]
    successes.sort(key=lambda x: x.get('impact_score', 0), reverse=True)
    
    # Failed patterns
    failures = [d for d in decisions if d['outcome'] == 'failure']
    
    # Rejected ideas
    rejections = [d for d in decisions if d['type'] == 'reject']
    
    # In progress
    in_progress = [d for d in decisions if d['status'] in ['pending', 'in_progress']]
    
    return jsonify({
        'success': True,
        'timestamp': datetime.utcnow().isoformat(),
        'recent_activity': {
            'decisions': [
                {'title': d['title'], 'category': d['category'], 'type': d['type'], 'status': d['status']}
                for d in recent[:10]
            ]
        },
        'successful_patterns': [
            {
                'title': d['title'],
                'category': d['category'],
                'technologies': d.get('technologies', []),
                'impact': d.get('impact_score'),
                'notes': d.get('outcome_notes')
            }
            for d in successes[:10]
        ],
        'failed_patterns': [
            {
                'title': d['title'],
                'category': d['category'],
                'reason': d.get('outcome_notes')
            }
            for d in failures[:5]
        ],
        'rejected_ideas': [d['title'] for d in rejections],
        'currently_in_progress': [d['title'] for d in in_progress]
    })


# Register blueprint in your main app:
# from api.decisions import decisions_bp
# app.register_blueprint(decisions_bp)
