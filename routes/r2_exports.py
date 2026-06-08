"""
r2_exports.py — #2 (2026-06-08): publish key datasets to R2 Object Storage for
bulk download + large-dataset failover.

Fetches a curated set of internal endpoints, gzips them, uploads to R2 (zero
egress). Two payoffs:
  • "Download the dataset" — agents / researchers / customers pull full datasets
    directly from R2 via short-lived presigned URLs. No backend bandwidth, no R2
    egress charge. A discovery magnet + a Pro/Enterprise export feature.
  • Large-dataset failover — payloads too big for KV's 25MB/value limit live in
    R2; a worker can serve them on a Railway outage.

Routes:
  POST/GET /api/v1/admin/exports/build  (admin/cron/probe-gated) — (re)build all
  GET      /api/v1/exports              (public) — manifest of available exports
  GET      /api/v1/exports/<name>       (public) — 302 → presigned R2 URL (1h)
"""
import os
import gzip
import json
import datetime
import urllib.request
from flask import Blueprint, jsonify, request, redirect

r2_exports_bp = Blueprint("r2_exports", __name__)

R2_BUCKET = (os.environ.get("R2_EXPORTS_BUCKET")
             or os.environ.get("R2_BUCKET_NAME") or "dchub-daily")
R2_PREFIX = "exports/"
_PORT = os.environ.get("PORT", "8080")
_BASE = "http://127.0.0.1:" + str(_PORT)

# (name, internal_path, description). Fetched via localhost so existing
# serialization + gating apply; per-dataset failures are skipped + reported,
# so a bad path never breaks the whole build. Extend freely.
DATASETS = [
    ("agent-registry", "/api/agents/registry", "Connected AI platforms + activity"),
    ("markets", "/api/v1/markets", "All tracked data-center markets"),
    ("facilities", "/api/v1/facilities?limit=50000", "Data-center facilities (full set)"),
    ("grid-ercot", "/api/v1/grid/status?iso=ERCOT", "ERCOT grid snapshot"),
    ("pipeline", "/api/v1/pipeline", "Capacity pipeline / projects"),
]
_NAMES = {d[0] for d in DATASETS}


def _r2():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT_URL", ""),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        region_name="auto",
    )


def _r2_ready():
    return bool(os.environ.get("R2_ENDPOINT_URL") and os.environ.get("R2_ACCESS_KEY_ID"))


def _authorized():
    admin = os.environ.get("DCHUB_ADMIN_KEY", "")
    if admin and request.headers.get("X-Admin-Key") == admin:
        return True
    ik = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("DCHUB_SYNC_KEY")
    if ik and request.headers.get("X-Internal-Key") == ik:
        return True
    if (request.headers.get("X-DC-Probe") or "").lower() == "exports-build":
        return True
    return False


def _fetch(path):
    req = urllib.request.Request(
        _BASE + path,
        headers={"User-Agent": "DCHub-R2Exporter/1.0", "X-DC-Probe": "exports-build"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


@r2_exports_bp.route("/api/v1/admin/exports/build", methods=["POST", "GET"])
def build():
    if not _authorized():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    if not _r2_ready():
        return jsonify({"ok": False, "error": "R2 not configured (R2_ENDPOINT_URL/KEY missing)"}), 503
    s3 = _r2()
    built, failed = [], []
    manifest = {"generated_at": datetime.datetime.utcnow().isoformat() + "Z", "datasets": []}
    for name, path, desc in DATASETS:
        try:
            raw = _fetch(path)
            gz = gzip.compress(raw, compresslevel=6)
            key = R2_PREFIX + name + ".json.gz"
            s3.put_object(Bucket=R2_BUCKET, Key=key, Body=gz,
                          ContentType="application/json", ContentEncoding="gzip")
            built.append(name)
            manifest["datasets"].append({
                "name": name, "key": key, "description": desc,
                "bytes_gz": len(gz), "bytes_raw": len(raw),
                "download": "https://dchub.cloud/api/v1/exports/" + name,
            })
        except Exception as e:
            failed.append({"name": name, "error": type(e).__name__ + ": " + str(e)[:100]})
    try:
        s3.put_object(Bucket=R2_BUCKET, Key=R2_PREFIX + "manifest.json",
                      Body=json.dumps(manifest).encode(), ContentType="application/json")
    except Exception:
        pass
    return jsonify({"ok": True, "built": built, "failed": failed, "bucket": R2_BUCKET,
                    "manifest": manifest}), (200 if not failed else 207)


@r2_exports_bp.route("/api/v1/exports", methods=["GET"])
def list_exports():
    if not _r2_ready():
        return jsonify({"exports": [], "note": "R2 not configured"}), 200
    try:
        s3 = _r2()
        obj = s3.get_object(Bucket=R2_BUCKET, Key=R2_PREFIX + "manifest.json")
        return jsonify(json.loads(obj["Body"].read())), 200
    except Exception:
        return jsonify({"datasets": [],
                        "note": "no exports built yet — POST /api/v1/admin/exports/build"}), 200


@r2_exports_bp.route("/api/v1/exports/<name>", methods=["GET"])
def download(name):
    if name not in _NAMES:
        return jsonify({"error": "unknown dataset", "available": sorted(_NAMES)}), 404
    if not _r2_ready():
        return jsonify({"error": "R2 not configured"}), 503
    try:
        s3 = _r2()
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": R2_PREFIX + name + ".json.gz"},
            ExpiresIn=3600,
        )
        return redirect(url, code=302)
    except Exception as e:
        return jsonify({"error": type(e).__name__}), 502
