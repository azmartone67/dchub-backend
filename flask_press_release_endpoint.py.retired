"""
Flask Backend Snippet — Add to your DC Hub app.py (Railway / Replit)
=====================================================================
This adds a protected API endpoint that the post_announcement.py script calls
to create press releases in your database.

1. Add the PressRelease model if you don't have one.
2. Add the /api/admin/press-releases route.
3. Redeploy on Railway.
"""

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
import os

app = Flask(__name__)
db  = SQLAlchemy(app)


# ── Model ─────────────────────────────────────────────────────────────────────

class PressRelease(db.Model):
    __tablename__ = "press_releases"

    id               = db.Column(db.Integer, primary_key=True)
    title            = db.Column(db.String(300), nullable=False)
    slug             = db.Column(db.String(300), unique=True, nullable=False)
    category         = db.Column(db.String(100), default="Press Release")
    date             = db.Column(db.String(20))          # e.g. "2026-04-07"
    subheadline      = db.Column(db.Text)
    body             = db.Column(db.Text)
    meta_description = db.Column(db.Text)
    published        = db.Column(db.Boolean, default=True)
    created_at       = db.Column(db.DateTime, server_default=db.func.now())

    def to_dict(self):
        return {
            "id":               self.id,
            "title":            self.title,
            "slug":             self.slug,
            "category":         self.category,
            "date":             self.date,
            "subheadline":      self.subheadline,
            "body":             self.body,
            "meta_description": self.meta_description,
            "published":        self.published,
            "url":              f"/press/{self.slug}",
        }


# ── Auth decorator ────────────────────────────────────────────────────────────

def require_api_key(f):
    """Check Bearer token matches DCHUB_ADMIN_API_KEY env var."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth   = request.headers.get("Authorization", "")
        token  = auth.replace("Bearer ", "").strip()
        secret = os.getenv("DCHUB_ADMIN_API_KEY")   # set this in Railway env vars
        if not secret or token != secret:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.route("/api/admin/press-releases", methods=["POST"])
@require_api_key
def create_press_release():
    """
    POST /api/admin/press-releases
    Body (JSON): title, slug, category, date, subheadline, body,
                 meta_description, published
    """
    data = request.get_json(force=True)

    required = ["title", "slug", "body"]
    missing  = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    # Check for duplicate slug
    if PressRelease.query.filter_by(slug=data["slug"]).first():
        return jsonify({"error": f"Slug '{data['slug']}' already exists"}), 409

    pr = PressRelease(
        title            = data["title"],
        slug             = data["slug"],
        category         = data.get("category", "Press Release"),
        date             = data.get("date"),
        subheadline      = data.get("subheadline"),
        body             = data.get("body"),
        meta_description = data.get("meta_description"),
        published        = data.get("published", True),
    )
    db.session.add(pr)
    db.session.commit()

    return jsonify(pr.to_dict()), 201


@app.route("/api/admin/press-releases", methods=["GET"])
@require_api_key
def list_press_releases():
    """GET /api/admin/press-releases — list all press releases."""
    releases = PressRelease.query.order_by(PressRelease.created_at.desc()).all()
    return jsonify([r.to_dict() for r in releases])


# ── Press page route (public) ─────────────────────────────────────────────────

@app.route("/press")
def press_page():
    """Public press & media page — renders all published press releases."""
    releases = (
        PressRelease.query
        .filter_by(published=True)
        .order_by(PressRelease.date.desc())
        .all()
    )
    # return render_template("press.html", releases=releases)
    return jsonify([r.to_dict() for r in releases])   # replace with render_template


@app.route("/press/<slug>")
def press_detail(slug):
    """Individual press release page."""
    release = PressRelease.query.filter_by(slug=slug, published=True).first_or_404()
    # return render_template("press_detail.html", release=release)
    return jsonify(release.to_dict())                  # replace with render_template
