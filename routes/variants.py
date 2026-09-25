"""Phase 111 — page variants & A/B harness.

Each surface can have multiple variants. We serve weighted-random,
log impressions + engagement, surface winners after enough samples.
"""
import os, json, random, datetime
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

variants_bp = Blueprint("variants", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS page_variants (
                id SERIAL PRIMARY KEY,
                surface TEXT NOT NULL,
                variant_label TEXT NOT NULL,
                content_json JSONB NOT NULL,
                weight INT DEFAULT 100,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                impressions BIGINT DEFAULT 0,
                engagements BIGINT DEFAULT 0,
                UNIQUE (surface, variant_label)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pv_surface ON page_variants(surface)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS page_variant_events (
                id SERIAL PRIMARY KEY,
                variant_id INT REFERENCES page_variants(id) ON DELETE CASCADE,
                event TEXT,    -- impression | engagement
                session_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        c.commit()


def serve_variant(surface):
    """Return (variant_id, content_json) for the given surface, weighted-random."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id, content_json, weight FROM page_variants
                       WHERE surface=%s AND is_active=TRUE""", (surface,))
        rows = cur.fetchall()
    if not rows: return None, None
    total = sum(r[2] for r in rows)
    pick = random.uniform(0, total)
    cum = 0
    for vid, content, weight in rows:
        cum += weight
        if pick <= cum:
            with _conn() as c, c.cursor() as cur:
                cur.execute("UPDATE page_variants SET impressions = impressions+1 WHERE id=%s", (vid,))
                cur.execute("INSERT INTO page_variant_events (variant_id, event) VALUES (%s, 'impression') ON CONFLICT DO NOTHING", (vid,))
                c.commit()
            return vid, content
    return rows[0][0], rows[0][1]


@variants_bp.route("/api/v1/variants", methods=["POST"])
def create_variant():
    body = request.get_json(silent=True) or {}
    surface = body.get("surface"); label = body.get("label")
    content = body.get("content"); weight = body.get("weight", 100)
    if not (surface and label and content):
        return jsonify(error="surface, label, content required"), 400
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO page_variants (surface, variant_label, content_json, weight)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (surface, variant_label) DO UPDATE
              SET content_json = EXCLUDED.content_json, weight = EXCLUDED.weight
            RETURNING id""", (surface, label, json.dumps(content), weight))
        vid = cur.fetchone()[0]; c.commit()
    return jsonify(id=vid, ok=True), 200


@variants_bp.route("/api/v1/variants/<int:vid>/engage", methods=["POST"])
def engage(vid):
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE page_variants SET engagements = engagements+1 WHERE id=%s", (vid,))
        cur.execute("INSERT INTO page_variant_events (variant_id, event) VALUES (%s, 'engagement') ON CONFLICT DO NOTHING", (vid,))
        c.commit()
    return jsonify(ok=True), 200


@variants_bp.route("/api/v1/variants/results", methods=["GET"])
def results():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, surface, variant_label, weight, is_active,
                   impressions, engagements,
                   CASE WHEN impressions>0
                        THEN ROUND(100.0*engagements/impressions, 2)
                        ELSE 0 END AS engagement_rate
            FROM page_variants ORDER BY surface, engagement_rate DESC
        """)
        rows = cur.fetchall()
    return jsonify(variants=rows, count=len(rows)), 200


@variants_bp.route("/api/v1/variants/promote", methods=["POST"])
def promote_winners():
    """For each surface with N variants where one has >= 1000 impressions
    and >= 1.5x engagement of others, mark it as the only active."""
    _ensure()
    promoted = []
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT surface, COUNT(*) AS n FROM page_variants
                       WHERE is_active=TRUE GROUP BY surface HAVING COUNT(*) > 1""")
        for r in cur.fetchall():
            cur.execute("""SELECT id, variant_label, impressions,
                           CASE WHEN impressions>0 THEN engagements::float/impressions ELSE 0 END AS er
                           FROM page_variants WHERE surface=%s AND is_active=TRUE""", (r["surface"],))
            vars_ = cur.fetchall()
            if not vars_: continue
            top = max(vars_, key=lambda x: x["er"])
            others_max = max((v["er"] for v in vars_ if v["id"] != top["id"]), default=0)
            if top["impressions"] >= 1000 and (others_max == 0 or top["er"] >= 1.5*others_max):
                cur.execute("UPDATE page_variants SET is_active=FALSE WHERE surface=%s AND id<>%s",
                            (r["surface"], top["id"]))
                promoted.append({"surface": r["surface"], "winner": top["variant_label"],
                                 "engagement_rate": round(top["er"]*100, 2)})
        c.commit()
    return jsonify(promoted=promoted, count=len(promoted)), 200
