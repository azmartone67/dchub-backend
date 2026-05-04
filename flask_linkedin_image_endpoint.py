"""
Flask Backend Snippet — LinkedIn Image Upload Endpoint
======================================================
Add this to your main.py on Railway (after the existing /api/linkedin/post route).

This adds:
1. POST /api/linkedin/upload-image  — uploads an image and returns a LinkedIn asset URN
2. POST /api/linkedin/delete        — deletes a LinkedIn post by URN

Both use the same X-Admin-Key auth as your existing LinkedIn routes.

How to add to main.py:
    Search for `def linkedin_post():` in main.py and paste these routes directly below it.
"""

import os
import io
import requests
from flask import request, jsonify

# Phase 30C — daily landing URL (LinkedIn renders rich card from this URL's OG)
def _phase30c_landing_url(d=None):
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"  # phase31_canonical_url


# ── Auth helper (already in your main.py — don't duplicate) ──────────────────

def check_admin_key():
    key = request.headers.get("X-Admin-Key") or request.args.get("admin_key", "")
    return key == os.environ.get("DCHUB_ADMIN_KEY", "")


def get_linkedin_token():
    """Fetch OAuth token from linkedin_tokens table (already implemented in your main.py)."""
    # This is a placeholder — your existing get_linkedin_token() function handles this.
    pass


# ── Image Upload ──────────────────────────────────────────────────────────────

@app.route("/api/linkedin/upload-image", methods=["POST"])
def linkedin_upload_image():
    """
    POST /api/linkedin/upload-image
    Multipart form: image file + company_id
    Auth: X-Admin-Key header
    Returns: {"asset": "urn:li:digitalmediaAsset:..."}
    """
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    image_file = request.files["image"]
    company_id = request.form.get("company_id", "110894959")
    token      = get_linkedin_access_token()   # your existing helper

    if not token:
        return jsonify({"error": "No LinkedIn token available"}), 503

    # Step 1: Register upload with LinkedIn
    register_url = "https://api.linkedin.com/v2/assets?action=registerUpload"
    register_headers = {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    register_body = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": f"urn:li:organization:{company_id}",
            "serviceRelationships": [{
                "relationshipType": "OWNER",
                "identifier": "urn:li:userGeneratedContent",
            }],
        }
    }

    try:
        r1 = requests.post(register_url, headers=register_headers, json=register_body, timeout=15)
        if r1.status_code != 200:
            return jsonify({"error": f"LinkedIn register failed: {r1.status_code} {r1.text[:200]}"}), 502
        reg_data = r1.json()
    except Exception as e:
        return jsonify({"error": f"Register request failed: {str(e)}"}), 502

    upload_url = reg_data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
    asset_urn  = reg_data["value"]["asset"]

    # Step 2: Upload the binary image
    try:
        image_bytes = image_file.read()
        r2 = requests.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  image_file.content_type or "image/png",
            },
            data=image_bytes,
            timeout=60,
        )
        if r2.status_code not in (200, 201):
            return jsonify({"error": f"LinkedIn image upload failed: {r2.status_code} {r2.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Image upload failed: {str(e)}"}), 502

    return jsonify({"asset": asset_urn, "asset_urn": asset_urn}), 201


# ── Delete Post ───────────────────────────────────────────────────────────────

@app.route("/api/linkedin/delete", methods=["POST"])
def linkedin_delete_post():
    """
    POST /api/linkedin/delete
    Body: {"urn": "urn:li:share:XXXXX"}
    Auth: X-Admin-Key header
    Returns: {"deleted": true}
    """
    if not check_admin_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True)
    urn  = data.get("urn", "").strip()
    if not urn:
        return jsonify({"error": "urn is required"}), 400

    token = get_linkedin_access_token()
    if not token:
        return jsonify({"error": "No LinkedIn token available"}), 503

    # Encode the URN for use as a URL path segment
    from urllib.parse import quote
    encoded_urn = quote(urn, safe="")
    delete_url  = f"https://api.linkedin.com/v2/shares/{encoded_urn}"

    try:
        r = requests.delete(
            delete_url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=15,
        )
        if r.status_code in (200, 204):
            return jsonify({"deleted": True, "urn": urn}), 200
        elif r.status_code == 404:
            return jsonify({"deleted": False, "error": "Post not found (already deleted?)"}), 404
        else:
            return jsonify({"error": f"LinkedIn delete failed: {r.status_code} {r.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Delete request failed: {str(e)}"}), 502
