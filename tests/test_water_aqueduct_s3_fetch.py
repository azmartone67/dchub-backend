"""Tests for the WRI Aqueduct authenticated-R2 fetch ladder (2026-07-11).

Covers routes/water_aqueduct_ingest.py:
  - _parse_s3_path        ("bucket/key" env contract)
  - _derive_s3_from_url   (bucket/key recovered from an R2 presigned URL)
  - _resolve_s3_target    (explicit env wins over URL derivation)
  - _fetch_source ladder  (authenticated S3 first, presigned fallback,
                           fail-soft: S3 failure never breaks the legacy path)
  - run_ingest source gates (no source → honest no-op; all fetches dead → error)

No network, no DB, no Flask app — pure-function + monkeypatched-fetch tests,
per the green-main rule (unit tests must never import main).
"""
import json

import pytest

from routes import water_aqueduct_ingest as wai

_ACCOUNT = "4bb33ec40ef02f9f4b41dc97668d5a52"
_PRESIGNED_PATH_STYLE = (
    f"https://{_ACCOUNT}.r2.cloudflarestorage.com/dchub-daily/wri/"
    "wri_aqueduct_us_states.json?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=604800"
)
_PRESIGNED_VIRTUAL = (
    f"https://dchub-daily.{_ACCOUNT}.r2.cloudflarestorage.com/wri/"
    "wri_aqueduct_us_states.json?X-Amz-Algorithm=AWS4-HMAC-SHA256"
)

# A minimal payload the flexible parser accepts (passes the direction gate too:
# arid AZ/NV out-score wet IL/OH by far more than the 10pt margin).
_SAMPLE_PAYLOAD = json.dumps([
    {"state": "AZ", "bws_cat": 3},
    {"state": "NV", "bws_cat": 4},
    {"state": "IL", "bws_cat": 1},
    {"state": "OH", "bws_cat": 0},
])


def _clear_env(monkeypatch):
    for var in ("WRI_AQUEDUCT_URL", "WRI_AQUEDUCT_S3_PATH", "R2_ENDPOINT_URL",
                "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# _parse_s3_path — the WRI_AQUEDUCT_S3_PATH "bucket/key" contract
# ---------------------------------------------------------------------------

def test_parse_s3_path_simple():
    assert wai._parse_s3_path("dchub-daily/wri/wri_aqueduct_us_states.json") == (
        "dchub-daily", "wri/wri_aqueduct_us_states.json")


def test_parse_s3_path_key_keeps_inner_slashes():
    assert wai._parse_s3_path("bucket/a/b/c.json") == ("bucket", "a/b/c.json")


def test_parse_s3_path_accepts_s3_scheme_and_whitespace():
    assert wai._parse_s3_path("  s3://dchub-daily/wri/x.json ") == ("dchub-daily", "wri/x.json")


def test_parse_s3_path_strips_leading_slash():
    assert wai._parse_s3_path("/dchub-daily/wri/x.json") == ("dchub-daily", "wri/x.json")


@pytest.mark.parametrize("bad", ["", None, "bucketonly", "bucket/", "/",
                                 "s3://bucket", "   "])
def test_parse_s3_path_rejects_incomplete(bad):
    assert wai._parse_s3_path(bad) is None


# ---------------------------------------------------------------------------
# _derive_s3_from_url — recover bucket/key from an R2 presigned URL
# ---------------------------------------------------------------------------

def test_derive_from_path_style_presigned_url():
    assert wai._derive_s3_from_url(_PRESIGNED_PATH_STYLE) == (
        "dchub-daily", "wri/wri_aqueduct_us_states.json")


def test_derive_from_virtual_hosted_presigned_url():
    assert wai._derive_s3_from_url(_PRESIGNED_VIRTUAL) == (
        "dchub-daily", "wri/wri_aqueduct_us_states.json")


def test_derive_unquotes_percent_encoding():
    url = f"https://{_ACCOUNT}.r2.cloudflarestorage.com/dchub-daily/wri/us%20states.json?X-Amz-Expires=1"
    assert wai._derive_s3_from_url(url) == ("dchub-daily", "wri/us states.json")


@pytest.mark.parametrize("bad", [
    "",
    None,
    "https://example.com/dchub-daily/wri/x.json",          # non-R2 host: never guess
    "https://raw.githubusercontent.com/o/r/main/x.csv",     # legacy raw hosting
    f"https://{_ACCOUNT}.r2.cloudflarestorage.com/",        # no path
    f"https://{_ACCOUNT}.r2.cloudflarestorage.com/bucketonly",  # bucket but no key
    f"https://x.y.{_ACCOUNT}.r2.cloudflarestorage.com/k",   # too many host labels
    "not a url at all",
])
def test_derive_rejects_non_r2_or_incomplete(bad):
    assert wai._derive_s3_from_url(bad) is None


# ---------------------------------------------------------------------------
# _resolve_s3_target — explicit env beats URL derivation
# ---------------------------------------------------------------------------

def test_resolve_prefers_explicit_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "explicit-bucket/explicit/key.json")
    monkeypatch.setenv("WRI_AQUEDUCT_URL", _PRESIGNED_PATH_STYLE)
    assert wai._resolve_s3_target() == ("explicit-bucket", "explicit/key.json", "env")


def test_resolve_derives_from_url_when_env_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_URL", _PRESIGNED_PATH_STYLE)
    assert wai._resolve_s3_target() == (
        "dchub-daily", "wri/wri_aqueduct_us_states.json", "derived_from_url")


def test_resolve_none_when_nothing_configured(monkeypatch):
    _clear_env(monkeypatch)
    assert wai._resolve_s3_target() is None


def test_resolve_none_for_non_r2_url(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/data.json")
    assert wai._resolve_s3_target() is None


# ---------------------------------------------------------------------------
# _fetch_source ladder — selection + fail-soft fallback (no network)
# ---------------------------------------------------------------------------

def test_ladder_uses_s3_when_it_succeeds(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    calls = {"presigned": 0}
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (_SAMPLE_PAYLOAD, None))
    monkeypatch.setattr(wai, "_fetch_via_presigned",
                        lambda u: calls.__setitem__("presigned", calls["presigned"] + 1)
                        or (_SAMPLE_PAYLOAD, None))
    payload, fetch_path, errors = wai._fetch_source()
    assert payload == _SAMPLE_PAYLOAD
    assert fetch_path.startswith("s3_authenticated(env:dchub-daily/wri/x.json")
    assert calls["presigned"] == 0          # presigned never touched
    assert errors == {}


def test_ladder_falls_back_to_presigned_on_s3_failure(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/wri.json")
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (None, "NoSuchKey"))
    monkeypatch.setattr(wai, "_fetch_via_presigned", lambda u: (_SAMPLE_PAYLOAD, None))
    payload, fetch_path, errors = wai._fetch_source()
    assert payload == _SAMPLE_PAYLOAD
    assert fetch_path == "presigned_url"
    assert "s3_authenticated" in errors     # failure recorded, not fatal


def test_ladder_falls_back_when_creds_missing(monkeypatch):
    # S3 path set but no R2 creds in env → real _fetch_via_s3 short-circuits
    # (no boto3 call) and the presigned rung serves.
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/wri.json")
    monkeypatch.setattr(wai, "_fetch_via_presigned", lambda u: (_SAMPLE_PAYLOAD, None))
    payload, fetch_path, errors = wai._fetch_source()
    assert payload == _SAMPLE_PAYLOAD
    assert fetch_path == "presigned_url"
    assert "r2_creds_missing" in errors["s3_authenticated"]


def test_ladder_presigned_only_config_still_works(monkeypatch):
    # Legacy config untouched: URL-only (non-R2 host) never attempts S3.
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/wri.json")
    monkeypatch.setattr(wai, "_fetch_via_s3",
                        lambda b, k: (_ for _ in ()).throw(AssertionError("S3 must not be tried")))
    monkeypatch.setattr(wai, "_fetch_via_presigned", lambda u: (_SAMPLE_PAYLOAD, None))
    payload, fetch_path, errors = wai._fetch_source()
    assert payload == _SAMPLE_PAYLOAD
    assert fetch_path == "presigned_url"


def test_ladder_all_rungs_dead(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/wri.json")
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (None, "AccessDenied"))
    monkeypatch.setattr(wai, "_fetch_via_presigned", lambda u: (None, "HTTPError: 403"))
    payload, fetch_path, errors = wai._fetch_source()
    assert payload is None and fetch_path is None
    assert set(errors) == {"s3_authenticated", "presigned_url"}


# ---------------------------------------------------------------------------
# run_ingest source gates (dry-run only — no DB, no network)
# ---------------------------------------------------------------------------

def test_run_ingest_no_source_is_honest_noop(monkeypatch):
    _clear_env(monkeypatch)
    out = wai.run_ingest(dry_run=True)
    assert out["ok"] is True and out["wrote"] == 0
    assert "skipped" in out
    assert "WRI_AQUEDUCT_S3_PATH" in out["skipped"]   # tells the operator both envs


def test_run_ingest_s3_only_fetch_failure_is_error_not_noop(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (None, "NoSuchKey"))
    out = wai.run_ingest(dry_run=True)
    assert out["ok"] is False and out["wrote"] == 0
    assert out["error"].startswith("fetch_failed")
    assert "NoSuchKey" in out["error"]


def test_run_ingest_dry_run_reports_fetch_path(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (_SAMPLE_PAYLOAD, None))
    out = wai.run_ingest(dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True and out["wrote"] == 0
    assert out["parsed_states"] == 4
    assert out["fetch_path"].startswith("s3_authenticated(env:")


def test_run_ingest_dry_run_via_presigned_fallback_reports_path(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WRI_AQUEDUCT_S3_PATH", "dchub-daily/wri/x.json")
    monkeypatch.setenv("WRI_AQUEDUCT_URL", "https://example.com/wri.json")
    monkeypatch.setattr(wai, "_fetch_via_s3", lambda b, k: (None, "ExpiredToken"))
    monkeypatch.setattr(wai, "_fetch_via_presigned", lambda u: (_SAMPLE_PAYLOAD, None))
    out = wai.run_ingest(dry_run=True)
    assert out["ok"] is True and out["fetch_path"] == "presigned_url"
