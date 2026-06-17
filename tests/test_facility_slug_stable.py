"""Consistency tests for the STABLE facility-slug hash (r-stable-slug 2026-06-16).

Guards two invariants:
  1. stable_hash8 is keyed on (provider, name) ONLY — id-independent and stable
     across re-ingestion (the whole point: stop Google-indexing churn).
  2. The Python helper stays byte-identical to the SQL expression in hash_sql():
     same field order (provider then name), '|' separator, raw values (no
     lower/trim), NULL/None -> '' both sides, UTF-8 both sides.
"""
import hashlib

from routes.facility_slug import stable_hash8, hash_sql


def test_stable_hash8_is_id_independent_and_stable():
    # Same (provider, name) -> same hash regardless of any id. The function does
    # not even take an id, so this is structurally guaranteed; we also pin the
    # exact value so a future refactor that changes the recipe (separator/order/
    # encoding) is caught.
    expected = hashlib.md5("Equinix|Equinix FR7".encode("utf-8")).hexdigest()[:8]
    assert stable_hash8("Equinix", "Equinix FR7") == expected
    # Stable across repeated calls (no hidden state / no id leakage).
    assert stable_hash8("Equinix", "Equinix FR7") == expected
    assert stable_hash8("Equinix", "Equinix FR7") == stable_hash8("Equinix", "Equinix FR7")


def test_unicode_is_supported_and_deterministic():
    h1 = stable_hash8("NorthC", "NorthC Zürich")
    h2 = stable_hash8("NorthC", "NorthC Zürich")
    assert h1 == h2
    assert len(h1) == 8
    assert all(ch in "0123456789abcdef" for ch in h1)
    # Must equal an explicit UTF-8 md5 (proves the encoding the SQL side relies on).
    assert h1 == hashlib.md5("NorthC|NorthC Zürich".encode("utf-8")).hexdigest()[:8]


def test_null_safety_none_and_empty_provider_are_equal():
    # None provider and '' provider must both be treated as '' -> identical hash.
    assert stable_hash8(None, "x") == stable_hash8("", "x")
    # Both equal the explicit '' -> 'x' form.
    assert stable_hash8(None, "x") == hashlib.md5("|x".encode("utf-8")).hexdigest()[:8]
    # Name null-safety too.
    assert stable_hash8("x", None) == stable_hash8("x", "")


def test_python_matches_sql_recipe_byte_for_byte():
    # Mirror the SQL: LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8).
    # For non-null values COALESCE is a no-op, so the SQL reduces to
    # md5(provider || '|' || name). Assert the Python helper produces exactly that.
    p, n = "Digital Realty", "Digital Realty FRA"
    assert stable_hash8(p, n) == hashlib.md5(f"{p}|{n}".encode("utf-8")).hexdigest()[:8]


def test_hash_sql_expression_shape():
    # The SQL fragment must use COALESCE for null-safety, the pipe separator, and
    # LEFT(...,8) — and honor the alias. (No DB needed; we assert the emitted SQL.)
    assert hash_sql("") == "LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)"
    assert hash_sql("df") == "LEFT(MD5(COALESCE(df.provider,'')||'|'||COALESCE(df.name,'')),8)"
