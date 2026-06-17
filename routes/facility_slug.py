"""Canonical STABLE facility-slug hash (r-stable-slug 2026-06-16).
Keyed on provider|name (invariant across re-ingestion), NOT the volatile DB id,
to stop Google-indexing churn. Python helper + SQL expression MUST stay byte-
identical: raw values, pipe separator, COALESCE/''-for-null, UTF-8, no lower/trim."""
import hashlib

def stable_hash8(provider, name):
    return hashlib.md5(f"{provider or ''}|{name or ''}".encode("utf-8")).hexdigest()[:8]

def hash_sql(alias=""):
    p = f"{alias}.provider" if alias else "provider"
    n = f"{alias}.name" if alias else "name"
    return f"LEFT(MD5(COALESCE({p},'')||'|'||COALESCE({n},'')),8)"
