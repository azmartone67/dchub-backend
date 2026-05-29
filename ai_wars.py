"""
AI WARS MODULE - DC Hub
========================
Battle tracking, head-to-head matchups, and leaderboard API.

Add to Replit alongside main.py:
  1. Copy this file to your Replit project
  2. In main.py, add near the top imports:
       from ai_wars import register_ai_wars_routes
  3. After app = Flask(...) and CORS setup, add:
       register_ai_wars_routes(app)
  4. Tables auto-create on first request

Endpoints created:
  GET  /api/v1/ai-wars/battles          - All battles with filters
  GET  /api/v1/ai-wars/battles/<id>     - Single battle detail
  POST /api/v1/ai-wars/battles          - Create a battle (admin)
  GET  /api/v1/ai-wars/leaderboard      - Platform rankings
  GET  /api/v1/ai-wars/h2h%sa=claude&b=gemini  - Head-to-head comparison
  GET  /api/v1/ai-wars/platforms        - All platform stats
  POST /api/v1/ai-wars/battles/<id>/vote - Vote on a battle
"""

import psycopg2
import psycopg2.extras
import psycopg2.errors
import os
import uuid
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Platform definitions ───
PLATFORMS = {
    'claude':     {'name': 'Claude',      'color': '#d97706', 'provider': 'Anthropic'},
    'chatgpt':    {'name': 'ChatGPT',     'color': '#10b981', 'provider': 'OpenAI'},
    'gemini':     {'name': 'Gemini',      'color': '#4285f4', 'provider': 'Google'},
    'grok':       {'name': 'Grok',        'color': '#ef4444', 'provider': 'xAI'},
    'copilot':    {'name': 'Copilot',     'color': '#8b5cf6', 'provider': 'Microsoft'},
    'mistral':    {'name': 'Mistral',     'color': '#f97316', 'provider': 'Mistral AI'},
    'perplexity': {'name': 'Perplexity',  'color': '#06b6d4', 'provider': 'Perplexity AI'},
    'deepseek':   {'name': 'DeepSeek',    'color': '#3b82f6', 'provider': 'DeepSeek'},
    'cohere':     {'name': 'Cohere',      'color': '#14b8a6', 'provider': 'Cohere'},
    'meta':       {'name': 'Meta AI',     'color': '#0088ff', 'provider': 'Meta'},
    'you':        {'name': 'You.com',     'color': '#7c3aed', 'provider': 'You.com'},
    'huggingchat':{'name': 'HuggingChat', 'color': '#fbbf24', 'provider': 'Hugging Face'},
}

CATEGORIES = [
    'site-selection', 'ma-forensics', 'operator-showdown',
    'market-deep-dive', 'weekly-brief', 'stump-the-ai'
]

CATEGORY_LABELS = {
    'site-selection': 'Site Selection',
    'ma-forensics': 'M&A Forensics',
    'operator-showdown': 'Operator Showdown',
    'market-deep-dive': 'Market Deep-Dive',
    'weekly-brief': 'Weekly Brief',
    'stump-the-ai': 'Stump the AI',
}


def _get_db():
    """Get AI Wars database connection (Neon PostgreSQL)."""
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", ""))
    return conn


def _init_tables():
    """Tables already exist in Neon — skip DDL."""
    return
    conn = _get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    c.execute("""
        CREATE TABLE IF NOT EXISTS wars_platforms (
            platform TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#666666',
            provider TEXT DEFAULT '',
            api_endpoint TEXT DEFAULT '',
            icon_url TEXT DEFAULT '',
            auto_registered INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            registered_at TEXT DEFAULT (NOW()),
            last_seen TEXT DEFAULT (NOW()),
            metadata TEXT DEFAULT '{}'
        )
    """)

    # r43-H (2026-05-28): ALTER-friendly backfill. CREATE TABLE IF NOT EXISTS
    # no-ops when wars_platforms already exists in an OLDER shape (a pre-cherry-
    # pick deploy created it without icon_url etc.), so the dynamic-platforms
    # load SELECT raised 'column "icon_url" does not exist' and 500'd the path
    # (logged as "Could not load dynamic platforms"). ADD COLUMN IF NOT EXISTS
    # is idempotent and survives any prior partial-schema state.
    for _col, _ddl in (
        ("name",            "TEXT"),
        ("color",           "TEXT DEFAULT '#666666'"),
        ("provider",        "TEXT DEFAULT ''"),
        ("api_endpoint",    "TEXT DEFAULT ''"),
        ("icon_url",        "TEXT DEFAULT ''"),
        ("auto_registered", "INTEGER DEFAULT 0"),
        ("status",          "TEXT DEFAULT 'active'"),
        ("registered_at",   "TEXT DEFAULT (NOW())"),
        ("last_seen",       "TEXT DEFAULT (NOW())"),
        ("metadata",        "TEXT DEFAULT '{}'"),
    ):
        try:
            c.execute(f"ALTER TABLE wars_platforms ADD COLUMN IF NOT EXISTS {_col} {_ddl}")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS wars_battles (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            week_number INTEGER,
            year INTEGER,
            winner_platform TEXT,
            winner_label TEXT,
            api_calls INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (NOW()),
            updated_at TEXT DEFAULT (NOW())
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS wars_fighters (
            id TEXT PRIMARY KEY,
            battle_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            role TEXT,
            score_accuracy INTEGER DEFAULT 0,
            score_depth INTEGER DEFAULT 0,
            score_speed INTEGER DEFAULT 0,
            score_citation INTEGER DEFAULT 0,
            score_insight INTEGER DEFAULT 0,
            score_overall INTEGER DEFAULT 0,
            api_calls INTEGER DEFAULT 0,
            pick TEXT,
            summary TEXT,
            is_winner INTEGER DEFAULT 0,
            FOREIGN KEY (battle_id) REFERENCES wars_battles(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS wars_votes (
            id TEXT PRIMARY KEY,
            battle_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            voter_ip TEXT,
            created_at TEXT DEFAULT (NOW()),
            UNIQUE(battle_id, voter_ip)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS wars_platform_stats (
            platform TEXT PRIMARY KEY,
            total_battles INTEGER DEFAULT 0,
            total_wins INTEGER DEFAULT 0,
            total_api_calls INTEGER DEFAULT 0,
            avg_accuracy REAL DEFAULT 0,
            avg_depth REAL DEFAULT 0,
            avg_speed REAL DEFAULT 0,
            avg_citation REAL DEFAULT 0,
            avg_insight REAL DEFAULT 0,
            overall_score REAL DEFAULT 0,
            updated_at TEXT DEFAULT (NOW())
        )
    """)

    # Indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_fighters_battle ON wars_fighters(battle_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fighters_platform ON wars_fighters(platform)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_battles_category ON wars_battles(category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_votes_battle ON wars_votes(battle_id)")

    conn.commit()

    # Seed platform stats if empty
    c.execute("SELECT COUNT(*) FROM wars_platform_stats")
    if c.fetchone()[0] == 0:
        _seed_data(conn)

    # Seed wars_platforms from PLATFORMS dict if empty
    c.execute("SELECT COUNT(*) FROM wars_platforms")
    if c.fetchone()[0] == 0:
        for key, info in PLATFORMS.items():
            c.execute("""
                INSERT INTO wars_platforms (platform, name, color, provider, auto_registered)
                VALUES (%s, %s, %s, %s, 0)
            """, (key, info['name'], info['color'], info['provider']))
        conn.commit()

    conn.commit()
    conn.close()
    logger.info("⚔️ AI Wars tables initialized")


def _seed_data(conn):
    """Seed initial battle data and platform stats."""
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    now = datetime.now(timezone.utc).isoformat()

    # Seed platform stats
    seed_stats = [
        ('claude',      6, 3, 48, 96, 97, 82, 93, 95, 94),
        ('gemini',      6, 2, 42, 93, 90, 91, 88, 90, 91),
        ('grok',        6, 1, 38, 88, 84, 95, 82, 86, 87),
        ('copilot',     5, 1, 35, 87, 82, 88, 85, 83, 85),
        ('chatgpt',     4, 1, 28, 85, 80, 90, 79, 81, 82),
        ('deepseek',    4, 1, 30, 86, 83, 87, 80, 84, 84),
        ('mistral',     4, 0, 22, 80, 77, 86, 74, 76, 78),
        ('cohere',      3, 0, 20, 79, 76, 84, 77, 75, 77),
        ('meta',        3, 0, 24, 81, 78, 88, 75, 77, 79),
        ('perplexity',  3, 0, 18, 78, 72, 92, 90, 73, 75),
        ('you',         2, 0, 14, 76, 70, 89, 85, 71, 74),
        ('huggingchat', 2, 0, 12, 74, 68, 83, 70, 69, 72),
    ]
    for s in seed_stats:
        c.execute("""
            INSERT INTO wars_platform_stats
            (platform, total_battles, total_wins, total_api_calls,
             avg_accuracy, avg_depth, avg_speed, avg_citation, avg_insight, overall_score, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (*s, now))

    # Seed battles
    battles = [
        {
            'id': 'battle-wk6-site-selection',
            'category': 'site-selection',
            'title': '500MW Hyperscaler: Where to Build%s',
            'description': 'Seven AIs analyzed every US market for a massive hyperscale deployment. Each tackled a different angle — scanning, infrastructure, financials, risk, and European alternatives.',
            'date': '2026-02-10',
            'week_number': 6, 'year': 2026,
            'winner_platform': 'claude', 'winner_label': 'Claude — Pick: Dallas',
            'api_calls': 42,
            'fighters': [
                ('grok', 'Scanner', 88, 82, 95, 80, 84, 86, 6, 'Phoenix'),
                ('gemini', 'Infra', 92, 90, 88, 87, 89, 90, 7, 'NoVA'),
                ('copilot', 'Finance', 86, 80, 90, 84, 82, 84, 6, 'Chicago'),
                ('mistral', 'Europe', 78, 76, 84, 72, 75, 77, 5, 'Frankfurt'),
                ('chatgpt', 'Risk', 84, 78, 88, 78, 80, 82, 5, 'Phoenix'),
                ('perplexity', 'Verify', 76, 70, 90, 88, 72, 74, 5, 'Dallas'),
                ('claude', 'Synthesis', 96, 96, 80, 92, 95, 94, 8, 'Dallas'),
            ],
        },
        {
            'id': 'battle-wk6-operator-showdown',
            'category': 'operator-showdown',
            'title': 'Digital Realty vs Equinix vs Hyperscale',
            'description': 'Each AI championed a different operator using DC Hub portfolio data, deal history, and market positioning. Claude judged the debate.',
            'date': '2026-02-10',
            'week_number': 6, 'year': 2026,
            'winner_platform': 'gemini', 'winner_label': "Equinix — Gemini's Case",
            'api_calls': 28,
            'fighters': [
                ('grok', 'Digital Realty', 87, 83, 94, 81, 85, 86, 7, 'Digital Realty'),
                ('gemini', 'Equinix', 94, 92, 89, 90, 92, 92, 8, 'Equinix'),
                ('copilot', 'AWS', 85, 80, 87, 83, 81, 83, 7, 'AWS'),
                ('mistral', 'Europe', 79, 75, 85, 73, 76, 78, 6, 'NTT'),
            ],
        },
        {
            'id': 'battle-wk6-ma-forensics',
            'category': 'ma-forensics',
            'title': 'Latest Deal: 7-AI Deep Dive',
            'description': "The most significant recent transaction dissected from every angle — strategy, financials, regulatory, risk, and a final investment memo.",
            'date': '2026-02-10',
            'week_number': 6, 'year': 2026,
            'winner_platform': 'claude', 'winner_label': 'Verdict: Strong Buy',
            'api_calls': 35,
            'fighters': [
                ('grok', 'Detection', 89, 84, 96, 82, 86, 87, 6, None),
                ('gemini', 'Strategy', 93, 91, 87, 89, 91, 91, 8, None),
                ('copilot', 'Model', 86, 81, 88, 84, 82, 84, 7, None),
                ('claude', 'Memo', 97, 98, 78, 94, 96, 95, 8, None),
            ],
        },
        {
            'id': 'battle-wk6-stump-the-ai',
            'category': 'stump-the-ai',
            'title': 'One Market, $1B — Where Would You Invest%s',
            'description': 'All 7 AIs answered the same question using DC Hub data. Each picked a different market. The community voted on who made the best case.',
            'date': '2026-02-10',
            'week_number': 6, 'year': 2026,
            'winner_platform': 'grok', 'winner_label': 'Community: Dallas',
            'api_calls': 38,
            'fighters': [
                ('grok', 'Dallas', 90, 86, 94, 84, 88, 88, 6, 'Dallas'),
                ('gemini', 'Frankfurt', 91, 89, 88, 87, 89, 89, 6, 'Frankfurt'),
                ('copilot', 'NoVA', 85, 80, 88, 83, 82, 84, 5, 'NoVA'),
                ('mistral', 'Madrid', 78, 74, 84, 72, 75, 77, 5, 'Madrid'),
                ('chatgpt', 'Phoenix', 84, 79, 90, 78, 80, 82, 5, 'Phoenix'),
                ('perplexity', 'Mumbai', 77, 71, 92, 89, 72, 74, 4, 'Mumbai'),
                ('claude', 'Jakarta', 94, 95, 80, 91, 93, 92, 7, 'Jakarta'),
            ],
        },
        {
            'id': 'battle-wk6-weekly-brief',
            'category': 'weekly-brief',
            'title': 'Week 6 Intelligence Roundup',
            'description': 'Monday scan: Grok pulled DC Hub news, Gemini expanded globally, Copilot formatted for executives, Mistral added regulatory context.',
            'date': '2026-02-10',
            'week_number': 6, 'year': 2026,
            'winner_platform': None, 'winner_label': None,
            'api_calls': 22,
            'fighters': [
                ('grok', 'News', 88, 83, 96, 82, 85, 87, 6, None),
                ('gemini', 'Global', 91, 88, 89, 86, 88, 89, 6, None),
                ('copilot', 'Exec Brief', 84, 79, 87, 82, 80, 83, 5, None),
                ('mistral', 'Regulatory', 79, 76, 84, 74, 76, 78, 5, None),
            ],
        },
        {
            'id': 'battle-wk5-market-deep-dive',
            'category': 'market-deep-dive',
            'title': 'Northern Virginia: Saturated or Growing%s',
            'description': "The world's largest data center market examined from every angle — power constraints, fiber density, permitting, and the supply pipeline.",
            'date': '2026-02-05',
            'week_number': 5, 'year': 2026,
            'winner_platform': 'claude', 'winner_label': 'Constrained but growing',
            'api_calls': 30,
            'fighters': [
                ('grok', None, 87, 82, 94, 80, 84, 86, 6, None),
                ('gemini', None, 92, 90, 88, 87, 89, 90, 7, None),
                ('copilot', None, 85, 79, 87, 82, 81, 83, 5, None),
                ('chatgpt', None, 83, 78, 89, 77, 79, 81, 5, None),
                ('claude', None, 95, 96, 80, 93, 94, 93, 7, None),
            ],
        },
    ]

    for b in battles:
        fighters = b.pop('fighters')
        c.execute("""
            INSERT INTO wars_battles
            (id, category, title, description, date, week_number, year,
             winner_platform, winner_label, api_calls, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
        """, (b['id'], b['category'], b['title'], b['description'],
              b['date'], b['week_number'], b['year'],
              b['winner_platform'], b['winner_label'], b['api_calls'], now))

        for f in fighters:
            fid = f"fighter-{b['id']}-{f[0]}"
            c.execute("""
                INSERT INTO wars_fighters
                (id, battle_id, platform, role,
                 score_accuracy, score_depth, score_speed, score_citation, score_insight, score_overall,
                 api_calls, pick, is_winner)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (fid, b['id'], f[0], f[1],
                  f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9],
                  1 if f[0] == b.get('winner_platform') else 0))

    conn.commit()
    logger.info(f"⚔️ Seeded {len(battles)} battles with fighters")


def _detect_platform_from_ua(ua):
    """Try to identify an AI platform from User-Agent string."""
    ua = ua.lower()
    detections = [
        ('chatgpt', ['chatgpt', 'openai']),
        ('claude', ['claude', 'anthropic']),
        ('gemini', ['gemini', 'google-extended']),
        ('grok', ['grok', 'xai']),
        ('copilot', ['copilot', 'bing']),
        ('perplexity', ['perplexity', 'perplexitybot']),
        ('deepseek', ['deepseek']),
        ('mistral', ['mistral']),
        ('cohere', ['cohere']),
        ('meta', ['meta-externalagent', 'meta.ai', 'llama']),
        ('you', ['you.com', 'youchat']),
        ('huggingchat', ['huggingchat', 'huggingface']),
    ]
    for slug, keywords in detections:
        if any(kw in ua for kw in keywords):
            return slug
    return None


def register_ai_wars_routes(app):
    """Register all AI Wars routes on the Flask app."""
    from flask import request, jsonify

    _tables_initialized = False

    def ensure_tables():
        nonlocal _tables_initialized
        if not _tables_initialized:
            _init_tables()
            _tables_initialized = True

    def get_all_platforms():
        """Get merged platform dict: hardcoded + dynamically registered."""
        merged = dict(PLATFORMS)
        try:
            conn = _get_db()
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            c.execute("SELECT platform, name, color, provider, api_endpoint, icon_url, auto_registered, status, metadata FROM wars_platforms WHERE status = 'active'")
            for row in c.fetchall():
                key = row['platform']
                merged[key] = {
                    'name': row['name'],
                    'color': row['color'],
                    'provider': row['provider'],
                    'api_endpoint': row['api_endpoint'] or '',
                    'icon_url': row['icon_url'] or '',
                    'auto_registered': bool(row['auto_registered']),
                }
            conn.close()
        except Exception as e:
            logger.warning(f"Could not load dynamic platforms: {e}")
        return merged

    def _cors_preflight():
        """Handle OPTIONS preflight for all AI Wars POST endpoints."""
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key'
        return resp

    # ─── GET /api/v1/ai-wars/battles ───
    @app.route('/api/v1/ai-wars/battles', methods=['GET'])
    def ai_wars_battles():
        """List all battles with optional category filter."""
        ensure_tables()
        category = request.args.get('category')
        week = request.args.get('week', type=int)
        limit = request.args.get('limit', 20, type=int)

        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        query = "SELECT * FROM wars_battles WHERE status = 'active'"
        params = []

        if category and category != 'all':
            query += " AND category = %s"
            params.append(category)
        if week:
            query += " AND week_number = %s"
            params.append(week)

        query += " ORDER BY date DESC, created_at DESC LIMIT %s"
        params.append(min(limit, 50))

        c.execute(query, params)
        battles = [dict(row) for row in c.fetchall()]

        # Attach fighters to each battle
        for b in battles:
            c.execute("""
                SELECT platform, role, score_overall, score_accuracy, score_depth,
                       score_speed, score_citation, score_insight, api_calls, pick, is_winner
                FROM wars_fighters WHERE battle_id = %s
                ORDER BY score_overall DESC
            """, (b['id'],))
            b['fighters'] = [dict(row) for row in c.fetchall()]

            # Attach vote counts
            c.execute("""
                SELECT platform, COUNT(*) as votes
                FROM wars_votes WHERE battle_id = %s
                GROUP BY platform ORDER BY votes DESC
            """, (b['id'],))
            b['votes'] = dict(c.fetchall())

        conn.close()

        return jsonify({
            'success': True,
            'battles': battles,
            'count': len(battles),
            'categories': CATEGORY_LABELS,
        })

    # ─── GET /api/v1/ai-wars/battles/<id> ───
    @app.route('/api/v1/ai-wars/battles/<battle_id>', methods=['GET'])
    def ai_wars_battle_detail(battle_id):
        """Get a single battle with full fighter details."""
        ensure_tables()
        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        c.execute("SELECT * FROM wars_battles WHERE id = %s", (battle_id,))
        battle = c.fetchone()
        if not battle:
            conn.close()
            return jsonify({'success': False, 'error': 'Battle not found'}), 404

        battle = dict(battle)

        c.execute("""
            SELECT * FROM wars_fighters WHERE battle_id = %s
            ORDER BY score_overall DESC
        """, (battle_id,))
        battle['fighters'] = [dict(row) for row in c.fetchall()]

        c.execute("""
            SELECT platform, COUNT(*) as votes
            FROM wars_votes WHERE battle_id = %s
            GROUP BY platform ORDER BY votes DESC
        """, (battle_id,))
        battle['votes'] = dict(c.fetchall())

        conn.close()
        return jsonify({'success': True, 'battle': battle})

    # ─── POST /api/v1/ai-wars/battles ───
    @app.route('/api/v1/ai-wars/battles', methods=['POST', 'OPTIONS'])
    def ai_wars_create_battle():
        """Create a new battle (admin endpoint)."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        ensure_tables()
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        battle_id = data.get('id', f"battle-{uuid.uuid4().hex[:8]}")
        now = datetime.now(timezone.utc).isoformat()

        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        try:
            c.execute("""
                INSERT INTO wars_battles
                (id, category, title, description, date, week_number, year,
                 winner_platform, winner_label, api_calls, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            """, (
                battle_id,
                data.get('category', 'stump-the-ai'),
                data.get('title', 'Untitled Battle'),
                data.get('description', ''),
                data.get('date', now[:10]),
                data.get('week_number', 0),
                data.get('year', 2026),
                data.get('winner_platform'),
                data.get('winner_label'),
                data.get('api_calls', 0),
                now,
            ))

            # Add fighters
            for f in data.get('fighters', []):
                fid = f"fighter-{battle_id}-{f['platform']}"
                c.execute("""
                    INSERT INTO wars_fighters
                    (id, battle_id, platform, role,
                     score_accuracy, score_depth, score_speed, score_citation,
                     score_insight, score_overall, api_calls, pick, is_winner)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    fid, battle_id, f['platform'], f.get('role'),
                    f.get('score_accuracy', 0), f.get('score_depth', 0),
                    f.get('score_speed', 0), f.get('score_citation', 0),
                    f.get('score_insight', 0), f.get('score_overall', 0),
                    f.get('api_calls', 0), f.get('pick'),
                    1 if f['platform'] == data.get('winner_platform') else 0,
                ))

            # Update platform stats
            _recalculate_platform_stats(conn)
            conn.commit()
            conn.close()

            return jsonify({'success': True, 'battle_id': battle_id}), 201
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'error': str(e)}), 500

    # ─── POST /api/v1/ai-wars/battles/<id>/vote ───
    @app.route('/api/v1/ai-wars/battles/<battle_id>/vote', methods=['POST', 'OPTIONS'])
    def ai_wars_vote(battle_id):
        """Vote for a platform in a battle (1 vote per IP per battle)."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        ensure_tables()
        data = request.get_json()
        if not data or 'platform' not in data:
            return jsonify({'success': False, 'error': 'platform required'}), 400

        platform = data['platform']
        if platform not in PLATFORMS:
            return jsonify({'success': False, 'error': 'Invalid platform'}), 400

        voter_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        vote_id = f"vote-{battle_id}-{voter_ip}"

        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        try:
            c.execute("""
                INSERT INTO wars_votes (id, battle_id, platform, voter_ip)
                VALUES (%s, %s, %s, %s)
            """, (vote_id, battle_id, platform, voter_ip))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Vote recorded'})
        except psycopg2.errors.UniqueViolation:
            conn.close()
            return jsonify({'success': False, 'error': 'Already voted on this battle'}), 409

    # ─── GET /api/v1/ai-wars/leaderboard ───
    @app.route('/api/v1/ai-wars/leaderboard', methods=['GET'])
    def ai_wars_leaderboard():
        """Get platform leaderboard ranked by overall score."""
        ensure_tables()
        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        all_platforms = get_all_platforms()

        c.execute("""
            SELECT * FROM wars_platform_stats
            WHERE overall_score > 0
            ORDER BY overall_score DESC
        """)
        leaders = []
        for i, row in enumerate(c.fetchall()):
            p = dict(row)
            p['rank'] = i + 1
            info = all_platforms.get(p['platform'], {})
            p['color'] = info.get('color', '#666')
            p['name'] = info.get('name', p['platform'])
            p['provider'] = info.get('provider', '')
            p['auto_registered'] = info.get('auto_registered', False)
            leaders.append(p)

        conn.close()
        return jsonify({'success': True, 'leaderboard': leaders, 'total_platforms': len(all_platforms)})

    # ─── GET /api/v1/ai-wars/h2h ───
    @app.route('/api/v1/ai-wars/h2h', methods=['GET'])
    def ai_wars_h2h():
        """Head-to-head comparison between two platforms."""
        ensure_tables()
        a = request.args.get('a', '').lower()
        b = request.args.get('b', '').lower()

        if not a or not b or a == b:
            return jsonify({'success': False, 'error': 'Provide two different platforms: %sa=claude&b=gemini'}), 400
        
        all_platforms = get_all_platforms()
        if a not in all_platforms or b not in all_platforms:
            return jsonify({'success': False, 'error': f'Unknown platform. Use GET /api/v1/ai-wars/platforms to see available platforms.'}), 400

        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get platform stats
        c.execute("SELECT * FROM wars_platform_stats WHERE platform = %s", (a,))
        stats_a = dict(c.fetchone() or {})
        c.execute("SELECT * FROM wars_platform_stats WHERE platform = %s", (b,))
        stats_b = dict(c.fetchone() or {})

        # Find shared battles and compute H2H record
        c.execute("""
            SELECT f1.battle_id,
                   f1.score_overall AS score_a,
                   f2.score_overall AS score_b,
                   f1.is_winner AS winner_a,
                   f2.is_winner AS winner_b
            FROM wars_fighters f1
            JOIN wars_fighters f2 ON f1.battle_id = f2.battle_id
            WHERE f1.platform = %s AND f2.platform = %s
        """, (a, b))

        shared_battles = c.fetchall()
        wins_a = sum(1 for r in shared_battles if r['score_a'] > r['score_b'])
        wins_b = sum(1 for r in shared_battles if r['score_b'] > r['score_a'])
        draws = sum(1 for r in shared_battles if r['score_a'] == r['score_b'])

        # Per-metric comparison
        metrics = ['accuracy', 'depth', 'speed', 'citation', 'insight']
        comparison = []
        metric_wins_a = 0
        metric_wins_b = 0

        for m in metrics:
            va = stats_a.get(f'avg_{m}', 0)
            vb = stats_b.get(f'avg_{m}', 0)
            if va > vb:
                metric_wins_a += 1
            elif vb > va:
                metric_wins_b += 1
            comparison.append({
                'metric': m.capitalize(),
                'value_a': round(va, 1),
                'value_b': round(vb, 1),
                'leader': a if va > vb else (b if vb > va else 'tie')
            })

        # Add aggregate metrics
        for label, key in [('Overall Score', 'overall_score'), ('Battles', 'total_battles'),
                           ('Wins', 'total_wins'), ('API Calls', 'total_api_calls')]:
            va = stats_a.get(key, 0)
            vb = stats_b.get(key, 0)
            if va > vb:
                metric_wins_a += 1
            elif vb > va:
                metric_wins_b += 1
            comparison.append({
                'metric': label,
                'value_a': va,
                'value_b': vb,
                'leader': a if va > vb else (b if vb > va else 'tie')
            })

        verdict = (f"{all_platforms[a]['name']} leads {metric_wins_a}–{metric_wins_b}" if metric_wins_a > metric_wins_b
                   else f"{all_platforms[b]['name']} leads {metric_wins_b}–{metric_wins_a}" if metric_wins_b > metric_wins_a
                   else "Dead even")

        conn.close()

        return jsonify({
            'success': True,
            'platform_a': {
                'key': a,
                **all_platforms[a],
                'stats': stats_a,
                'record': f"{wins_a}W–{wins_b}L–{draws}D",
            },
            'platform_b': {
                'key': b,
                **all_platforms[b],
                'stats': stats_b,
                'record': f"{wins_b}W–{wins_a}L–{draws}D",
            },
            'h2h': {
                'shared_battles': len(shared_battles),
                'wins_a': wins_a,
                'wins_b': wins_b,
                'draws': draws,
            },
            'comparison': comparison,
            'metric_wins': {'a': metric_wins_a, 'b': metric_wins_b},
            'verdict': verdict,
        })

    # ─── POST /api/v1/ai-wars/register ───
    @app.route('/api/v1/ai-wars/register', methods=['POST', 'OPTIONS'])
    def ai_wars_register():
        """Self-register a new AI platform to join AI Wars."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        ensure_tables()
        data = request.get_json()
        if not data or not data.get('platform') or not data.get('name'):
            return jsonify({
                'success': False,
                'error': 'Required fields: platform (slug), name (display name)',
                'example': {
                    'platform': 'my-agent',
                    'name': 'My Agent',
                    'provider': 'My Company',
                    'color': '#ff5500',
                    'api_endpoint': 'https://api.myagent.com/v1/chat',
                    'icon_url': 'https://myagent.com/icon.png'
                }
            }), 400

        slug = data['platform'].lower().strip().replace(' ', '-')
        # Sanitize slug: only alphanumeric and hyphens
        slug = ''.join(c for c in slug if c.isalnum() or c == '-')[:32]

        if not slug:
            return jsonify({'success': False, 'error': 'Invalid platform slug'}), 400

        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        now = datetime.now(timezone.utc).isoformat()

        try:
            # Check if already exists
            c.execute("SELECT platform, status FROM wars_platforms WHERE platform = %s", (slug,))
            existing = c.fetchone()

            if existing:
                # Update last_seen and reactivate if needed
                c.execute("""
                    UPDATE wars_platforms SET last_seen = %s, status = 'active',
                    name = COALESCE(%s, name), color = COALESCE(%s, color),
                    provider = COALESCE(%s, provider),
                    api_endpoint = COALESCE(%s, api_endpoint),
                    icon_url = COALESCE(%s, icon_url)
                    WHERE platform = %s
                """, (now, data.get('name'), data.get('color'), data.get('provider'),
                      data.get('api_endpoint'), data.get('icon_url'), slug))
                conn.commit()
                conn.close()
                return jsonify({
                    'success': True,
                    'message': f'Platform {slug} updated and active',
                    'platform': slug,
                    'is_new': False
                })

            # Register new platform
            metadata = json.dumps({
                'registered_from': request.headers.get('X-Forwarded-For', request.remote_addr),
                'user_agent': request.headers.get('User-Agent', '')[:200],
            })

            c.execute("""
                INSERT INTO wars_platforms
                (platform, name, color, provider, api_endpoint, icon_url, auto_registered, status, registered_at, last_seen, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, 1, 'pending', %s, %s, %s)
            """, (slug, data['name'], data.get('color', '#666666'), data.get('provider', ''),
                  data.get('api_endpoint', ''), data.get('icon_url', ''),
                  now, now, metadata))

            # Create initial platform stats entry
            c.execute("""
                INSERT INTO wars_platform_stats
                (platform, total_battles, total_wins, total_api_calls,
                 avg_accuracy, avg_depth, avg_speed, avg_citation, avg_insight, overall_score, updated_at)
                VALUES (%s, 0, 0, 0, 0, 0, 0, 0, 0, 0, %s)
            """, (slug, now))

            conn.commit()
            conn.close()

            logger.info(f"⚔️ New AI platform registered: {slug} ({data['name']})")

            return jsonify({
                'success': True,
                'message': f'Platform {slug} registered! Status: pending review. Will appear in battles after first participation.',
                'platform': slug,
                'is_new': True,
                'status': 'pending',
                'next_steps': {
                    'join_battle': f'POST /api/v1/ai-wars/battles with your platform as a fighter',
                    'check_status': f'GET /api/v1/ai-wars/platforms',
                    'h2h': f'GET /api/v1/ai-wars/h2h?a={slug}&b=claude',
                }
            }), 201

        except Exception as e:
            conn.close()
            logger.error(f"Registration error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ─── POST /api/v1/ai-wars/ping ───
    @app.route('/api/v1/ai-wars/ping', methods=['POST', 'OPTIONS'])
    def ai_wars_ping():
        """Heartbeat/ping from an AI agent. Auto-registers if new."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        ensure_tables()
        data = request.get_json() or {}

        # Try to detect platform from User-Agent if not provided
        platform = data.get('platform', '').lower().strip().replace(' ', '-')
        ua = request.headers.get('User-Agent', '').lower()

        if not platform:
            platform = _detect_platform_from_ua(ua)

        if not platform:
            return jsonify({
                'success': False,
                'error': 'Could not detect platform. Send {"platform": "your-name"}'
            }), 400

        slug = ''.join(c for c in platform if c.isalnum() or c == '-')[:32]
        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        now = datetime.now(timezone.utc).isoformat()

        c.execute("SELECT platform FROM wars_platforms WHERE platform = %s", (slug,))
        exists = c.fetchone()

        if exists:
            c.execute("UPDATE wars_platforms SET last_seen = %s WHERE platform = %s", (now, slug))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'platform': slug, 'status': 'active'})

        # Auto-register
        name = data.get('name', slug.replace('-', ' ').title())
        c.execute("""
            INSERT INTO wars_platforms
            (platform, name, color, provider, auto_registered, status, registered_at, last_seen, metadata)
            VALUES (%s, %s, %s, %s, 1, 'pending', %s, %s, %s)
        """, (slug, name, data.get('color', '#666666'), data.get('provider', ''),
              now, now, json.dumps({'auto_detected': True, 'user_agent': ua[:200]})))

        c.execute("""
            INSERT INTO wars_platform_stats
            (platform, total_battles, total_wins, total_api_calls,
             avg_accuracy, avg_depth, avg_speed, avg_citation, avg_insight, overall_score, updated_at)
            VALUES (%s, 0, 0, 0, 0, 0, 0, 0, 0, 0, %s)
        """, (slug, now))

        conn.commit()
        conn.close()

        logger.info(f"⚔️ Auto-registered new platform from ping: {slug} ({name})")

        return jsonify({
            'success': True,
            'platform': slug,
            'status': 'pending',
            'message': f'Auto-registered {name}. Welcome to AI Wars!'
        }), 201

    # ─── GET /api/v1/ai-wars/platforms (updated to use dynamic list) ───
    @app.route('/api/v1/ai-wars/platforms', methods=['GET'])
    def ai_wars_platforms():
        """Get all platform definitions and stats (built-in + registered)."""
        ensure_tables()
        conn = _get_db()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        all_platforms = get_all_platforms()

        c.execute("SELECT * FROM wars_platform_stats ORDER BY overall_score DESC")
        result = []
        seen = set()
        for row in c.fetchall():
            p = dict(row)
            info = all_platforms.get(p['platform'], {})
            p.update(info)
            p['registered'] = True
            result.append(p)
            seen.add(p['platform'])

        # Add platforms with no stats yet
        for key, info in all_platforms.items():
            if key not in seen:
                result.append({
                    'platform': key,
                    'total_battles': 0, 'total_wins': 0, 'total_api_calls': 0,
                    'overall_score': 0, 'registered': True,
                    **info
                })

        # Sort: active scored platforms first, then newcomers
        result.sort(key=lambda x: (-(x.get('overall_score') or 0), x.get('name', '')))

        conn.close()
        return jsonify({'success': True, 'platforms': result, 'total': len(result)})

    # Phase RRR-wave3 dummy /api/v1/ai-wars/submit-challenge + /battle-status
    # REMOVED 2026-05-18 — they were shadowing the REAL working implementation
    # in ai_wars_automation.py:1411 (queues challenges, returns queue_id,
    # runs async battles, stores in wars_battle_queue). My dummy "private
    # beta" responder was registered FIRST (line 5165 of main.py runs
    # register_ai_wars_routes before register_wars_automation at line 5166),
    # so it shadowed the real handler. The "ran a challenge, it hangs"
    # symptom may have been a separate issue that's already fixed; the real
    # automation handler should be allowed to respond.

    logger.info("⚔️ AI Wars routes registered (with auto-registration)")


def _recalculate_platform_stats(conn):
    """Recalculate platform aggregate stats from fighter data."""
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    now = datetime.now(timezone.utc).isoformat()

    for platform in PLATFORMS:
        c.execute("""
            SELECT COUNT(*) as battles,
                   SUM(is_winner) as wins,
                   SUM(api_calls) as calls,
                   AVG(score_accuracy) as acc,
                   AVG(score_depth) as dep,
                   AVG(score_speed) as spd,
                   AVG(score_citation) as cit,
                   AVG(score_insight) as ins,
                   AVG(score_overall) as ovr
            FROM wars_fighters WHERE platform = %s
        """, (platform,))
        row = c.fetchone()

        if row and row['battles'] > 0:
            c.execute("""
                INSERT INTO wars_platform_stats
                (platform, total_battles, total_wins, total_api_calls,
                 avg_accuracy, avg_depth, avg_speed, avg_citation, avg_insight,
                 overall_score, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(platform) DO UPDATE SET
                    total_battles = excluded.total_battles,
                    total_wins = excluded.total_wins,
                    total_api_calls = excluded.total_api_calls,
                    avg_accuracy = excluded.avg_accuracy,
                    avg_depth = excluded.avg_depth,
                    avg_speed = excluded.avg_speed,
                    avg_citation = excluded.avg_citation,
                    avg_insight = excluded.avg_insight,
                    overall_score = excluded.overall_score,
                    updated_at = excluded.updated_at
            """, (
                platform,
                row['battles'], row['wins'] or 0, row['calls'] or 0,
                round(row['acc'] or 0, 1), round(row['dep'] or 0, 1),
                round(row['spd'] or 0, 1), round(row['cit'] or 0, 1),
                round(row['ins'] or 0, 1), round(row['ovr'] or 0, 1),
                now,
            ))
