"""util/listing_ledger.py — the pocket-listing lead register (2026-09-11).

WHY THIS EXISTS. DC Hub introduces prospects to the operators behind off-market
("pocket") capacity listings, and the program is only worth something if DC Hub
can show an operator that a prospect came through DC Hub. The failure to design
against is not a missing record — it is a record the operator has no reason to
trust: a row DC Hub could have inserted, edited or back-dated after the prospect
turned up at the operator's door on their own.

So every lead event — a requirement registered, a listing opened by an
identified user, an introduction requested, an inbox confirmed, an operator
notified — is one entry in a single append-only hash chain:

    entry_json = canonical JSON of the event, including prev_hash and key_id
    entry_hash = sha256(entry_json)
    signature  = HMAC-SHA256(ledger key, "entry|" + entry_hash)

Editing, deleting, re-ordering or back-dating any entry breaks every link after
it, and `verify_chain` names the first broken sequence number.

★ The chain is anchored OUTSIDE DC Hub by the notices themselves. Every operator
notice quotes the entry hashes, so the operator's own inbox holds a copy DC Hub
cannot rewrite, and their mail server's receipt time is an independent witness
that the registration existed before any direct contact. That — not the HMAC,
which only DC Hub can check — is what makes the record persuasive to the other
side.

★ An entry signed by a key DC Hub does not hold is a BREAK, not "unverifiable".
key_id sits inside entry_json, so anyone able to write rows could otherwise
rewrite an entry, re-hash every entry after it, and stamp them all with a key id
nobody holds — every hash and link would check out and the chain would read
intact. Key rotation is supported the honest way: list retired keys in
DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS and old entries verify against them.

★ PII is COMMITTED, not chained. entry_json carries an HMAC commitment to the
prospect's email, name, role, company, message and requirement — never the raw
values. The raw values live in ordinary columns beside the entry, so a deletion
request can null them without breaking a single hash; the verifier then reports
that entry as intact but redacted instead of as tampered.

★ The hash covers `entry_json` exactly as stored (TEXT), not a re-serialisation
of the typed columns. JSONB and TIMESTAMPTZ round-trips normalise numbers and
time zones, and a verifier that re-serialised them would call honest rows
tampered. The typed columns exist for querying; the verifier cross-checks the
ones that matter against entry_json (`columns_consistent`).

This module is pure apart from `append`, which must take the advisory lock, read
the chain head and insert in ONE transaction — two concurrent appends reading
the same head would fork the chain.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone

GENESIS_HASH = "0" * 64

# pg_advisory_xact_lock key serialising appends. Arbitrary but fixed: every
# writer must use the same value or the chain can fork.
LEDGER_LOCK_KEY = 7304522619

# Crockford base32 (no I, L, O, U) — lead ids get read aloud and retyped from
# email, so ambiguous glyphs are out. 10 symbols = 50 bits.
_LEAD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LEAD_ID_PREFIX = "LD-"

EVENTS = (
    "interest_registered",   # a standing requirement for upcoming listings
    "listing_viewed",        # an identified user opened a walled listing
    "intro_requested",       # an introduction to one listing's operator
    "email_confirmed",       # the prospect proved the inbox from the request
    "operator_notified",     # DC Hub sent the operator a registration notice
    "introduced",            # DC Hub made the introduction
    "withdrawn",             # the request was withdrawn
)
LEAD_OPENING_EVENTS = ("interest_registered", "intro_requested")

CONFIRM_TOKEN_TTL_S = 14 * 24 * 3600
OPERATOR_TOKEN_TTL_S = 180 * 24 * 3600
_CLOCK_SKEW_S = 300

# Raw, redactable columns. Deliberately NOT part of entry_json.
PII_FIELDS = ("email", "name", "role", "company", "message", "requirement")

_DERIVE_LABEL = b"dchub/listing-lead-ledger/v1"

SCHEMA_STATEMENTS = (
    "SET LOCAL lock_timeout = '3s'",
    """
    CREATE TABLE IF NOT EXISTS listing_lead_ledger (
        seq             BIGSERIAL PRIMARY KEY,
        lead_id         TEXT,
        event           TEXT NOT NULL,
        listing_id      BIGINT,
        listing_slug    TEXT,
        listing_title   TEXT,
        user_ref        TEXT,
        email           TEXT,
        name            TEXT,
        role            TEXT,
        company         TEXT,
        message         TEXT,
        requirement     JSONB,
        email_domain    TEXT,
        email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
        verified_via    TEXT,
        channel         TEXT,
        platform        TEXT,
        client          JSONB,
        session_hash    TEXT,
        ip_hash         TEXT,
        user_agent      TEXT,
        terms_version   TEXT,
        meta            JSONB,
        created_at      TIMESTAMPTZ NOT NULL,
        prev_hash       TEXT NOT NULL,
        entry_json      TEXT NOT NULL,
        entry_hash      TEXT NOT NULL UNIQUE,
        signature       TEXT NOT NULL,
        key_id          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_listing_lead_ledger_lead "
    "ON listing_lead_ledger (lead_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_listing_lead_ledger_listing "
    "ON listing_lead_ledger (listing_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_listing_lead_ledger_user "
    "ON listing_lead_ledger (user_ref, event, created_at DESC)",
)

# Defence in depth, kept separate from SCHEMA_STATEMENTS so a managed database
# that refuses plpgsql cannot also block the table itself. The hash chain is the
# real evidence; this only stops an ordinary UPDATE or DELETE from quietly
# rewriting it. Redaction (nulling the raw PII columns) stays possible.
GUARD_STATEMENTS = (
    "SET LOCAL lock_timeout = '3s'",
    """
    CREATE OR REPLACE FUNCTION listing_lead_ledger_guard() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'listing_lead_ledger is append-only';
        END IF;
        IF NEW.seq IS DISTINCT FROM OLD.seq
           OR NEW.lead_id IS DISTINCT FROM OLD.lead_id
           OR NEW.event IS DISTINCT FROM OLD.event
           OR NEW.listing_id IS DISTINCT FROM OLD.listing_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.prev_hash IS DISTINCT FROM OLD.prev_hash
           OR NEW.entry_json IS DISTINCT FROM OLD.entry_json
           OR NEW.entry_hash IS DISTINCT FROM OLD.entry_hash
           OR NEW.signature IS DISTINCT FROM OLD.signature
           OR NEW.key_id IS DISTINCT FROM OLD.key_id THEN
            RAISE EXCEPTION 'listing_lead_ledger evidence columns are immutable';
        END IF;
        RETURN NEW;
    END
    $$ LANGUAGE plpgsql
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgname = 'trg_listing_lead_ledger_guard') THEN
            CREATE TRIGGER trg_listing_lead_ledger_guard
                BEFORE UPDATE OR DELETE ON listing_lead_ledger
                FOR EACH ROW EXECUTE FUNCTION listing_lead_ledger_guard();
        END IF;
    END
    $$
    """,
)


class LedgerUnavailable(RuntimeError):
    """The register cannot take a write. Callers must refuse to register a lead
    rather than record one they cannot evidence."""


# ── keys ──────────────────────────────────────────────────────────────────

def _derive(jwt_secret: str) -> bytes:
    return hmac.new(jwt_secret.encode("utf-8"), _DERIVE_LABEL, hashlib.sha256).digest()


def ledger_secret(environ=None) -> bytes | None:
    """The CURRENT signing and commitment key, or None — never a literal fallback.

    DCHUB_LEAD_LEDGER_SECRET when set. Otherwise a sub-key derived from
    JWT_SECRET with a fixed label, so the ledger never shares raw key material
    with session tokens. main.py refuses to boot without a real JWT_SECRET, so
    in production this resolves; anywhere it does not, writes fail CLOSED.
    ★ Rotating JWT_SECRET rotates this key too: list the old JWT_SECRET in
    DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS or every earlier entry stops verifying.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("DCHUB_LEAD_LEDGER_SECRET") or "").strip()
    if explicit:
        return explicit.encode("utf-8")
    jwt_secret = (env.get("JWT_SECRET") or "").strip()
    if jwt_secret and "change-in-production" not in jwt_secret.lower():
        return _derive(jwt_secret)
    return None


def ledger_keys(environ=None) -> list:
    """Every key an entry may legitimately carry: the current key first, then
    retired ones from DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS (whitespace-separated).
    Each retired value is tried both as a raw ledger secret and as a former
    JWT_SECRET, since the default key is derived from JWT_SECRET."""
    env = os.environ if environ is None else environ
    keys = []
    current = ledger_secret(env)
    if current:
        keys.append(current)
    for value in (env.get("DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS") or "").split():
        for candidate in (value.encode("utf-8"), _derive(value)):
            if candidate not in keys:
                keys.append(candidate)
    return keys


def key_id(secret: bytes) -> str:
    """Non-secret fingerprint of a key, recorded per entry so a verifier can
    pick the right key after a rotation."""
    return hashlib.sha256(b"dchub-ledger-key-id|" + secret).hexdigest()[:12]


def _as_keys(keys) -> list:
    if keys is None:
        return []
    if isinstance(keys, (bytes, bytearray)):
        return [bytes(keys)]
    return [bytes(k) for k in keys if k]


def _key_for(entry: dict, keys: list):
    kid = entry.get("key_id")
    return next((k for k in keys if key_id(k) == kid), None)


# ── small pure helpers ────────────────────────────────────────────────────

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def new_lead_id() -> str:
    return LEAD_ID_PREFIX + "".join(secrets.choice(_LEAD_ALPHABET) for _ in range(10))


def looks_like_lead_id(value: str) -> bool:
    v = (value or "").strip().upper()
    body = v[len(LEAD_ID_PREFIX):]
    return (v.startswith(LEAD_ID_PREFIX) and len(body) == 10
            and all(ch in _LEAD_ALPHABET for ch in body))


def mask_email(email) -> str | None:
    e = (email or "").strip()
    local, sep, domain = e.rpartition("@")
    if not sep or not local or not domain:
        return None
    return f"{local[0]}***@{domain.lower()}"


def email_domain(email) -> str | None:
    e = (email or "").strip()
    _, sep, domain = e.rpartition("@")
    return domain.lower() if sep and domain else None


def pii_commitment(secret: bytes, pii: dict) -> str:
    """HMAC commitment to the raw prospect fields. Keyed, so the public entry
    cannot be brute-forced back to an email address."""
    norm = {k: pii.get(k) for k in PII_FIELDS}
    if isinstance(norm.get("email"), str):
        norm["email"] = norm["email"].strip().lower()
    return hmac.new(secret, b"pii|" + canonical_json(norm).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def sign(secret: bytes, entry_hash: str) -> str:
    return hmac.new(secret, ("entry|" + entry_hash).encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ── entries ───────────────────────────────────────────────────────────────

def build_entry(*, secret: bytes, prev_hash: str, created_at: datetime,
                lead_id, event: str, listing: dict | None = None,
                user_ref=None, pii: dict | None = None, email_verified=False,
                verified_via=None, channel=None, platform=None, client=None,
                session_hash=None, ip_hash=None, terms_version=None,
                meta=None) -> dict:
    if event not in EVENTS:
        raise ValueError(f"unknown ledger event {event!r}")
    listing = listing or {}
    entry = {
        "v": 1,
        "lead_id": lead_id,
        "event": event,
        "listing_id": listing.get("id"),
        "listing_slug": listing.get("slug"),
        "listing_title": listing.get("title"),
        "user_ref": user_ref,
        "pii_commitment": pii_commitment(secret, pii) if pii else None,
        "email_domain": email_domain((pii or {}).get("email")),
        "email_verified": bool(email_verified),
        "verified_via": verified_via,
        "channel": channel,
        "platform": platform,
        "client": client or None,
        "session_hash": session_hash,
        "ip_hash": ip_hash,
        "terms_version": terms_version,
        "meta": meta or None,
        "created_at": iso_utc(created_at),
        "prev_hash": prev_hash,
        "key_id": key_id(secret),
    }
    entry_json = canonical_json(entry)
    entry_hash = sha256_hex(entry_json)
    return {"entry": entry, "entry_json": entry_json, "entry_hash": entry_hash,
            "signature": sign(secret, entry_hash)}


def append(conn, secret: bytes | None, *, lead_id, event: str,
           listing: dict | None = None, user_ref=None, pii: dict | None = None,
           email_verified=False, verified_via=None, channel=None, platform=None,
           client=None, session_hash=None, ip_hash=None, user_agent=None,
           terms_version=None, meta=None, now: datetime | None = None) -> dict:
    """Append one entry. Lock, read head, insert and commit in ONE transaction.

    Raises LedgerUnavailable when no secret is configured or the row did not
    land — a lead is never reported registered on a write that did not happen.
    """
    if secret is None:
        raise LedgerUnavailable("no ledger secret configured")
    pii = pii or None
    listing = listing or {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LEDGER_LOCK_KEY,))
            cur.execute("SELECT entry_hash FROM listing_lead_ledger "
                        "ORDER BY seq DESC LIMIT 1")
            head = cur.fetchone()
            prev = head[0] if head else GENESIS_HASH
            built = build_entry(
                secret=secret, prev_hash=prev,
                created_at=now or datetime.now(timezone.utc), lead_id=lead_id,
                event=event, listing=listing, user_ref=user_ref, pii=pii,
                email_verified=email_verified, verified_via=verified_via,
                channel=channel, platform=platform, client=client,
                session_hash=session_hash, ip_hash=ip_hash,
                terms_version=terms_version, meta=meta)
            e = built["entry"]
            raw = pii or {}
            requirement = raw.get("requirement")
            cur.execute(
                """INSERT INTO listing_lead_ledger
                       (lead_id, event, listing_id, listing_slug, listing_title,
                        user_ref, email, name, role, company, message,
                        requirement, email_domain, email_verified, verified_via,
                        channel, platform, client, session_hash, ip_hash,
                        user_agent, terms_version, meta, created_at, prev_hash,
                        entry_json, entry_hash, signature, key_id)
                   VALUES (%s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s,
                           %s::jsonb, %s, %s, %s,
                           %s, %s, %s::jsonb, %s, %s,
                           %s, %s, %s::jsonb, %s, %s,
                           %s, %s, %s, %s)
                   ON CONFLICT (entry_hash) DO NOTHING
                   RETURNING seq""",
                (e["lead_id"], e["event"], e["listing_id"], e["listing_slug"],
                 e["listing_title"], e["user_ref"],
                 (raw.get("email") or None), raw.get("name"), raw.get("role"),
                 raw.get("company"), raw.get("message"),
                 canonical_json(requirement) if requirement is not None else None,
                 e["email_domain"], e["email_verified"], e["verified_via"],
                 e["channel"], e["platform"],
                 canonical_json(e["client"]) if e["client"] is not None else None,
                 e["session_hash"], e["ip_hash"], user_agent, e["terms_version"],
                 canonical_json(e["meta"]) if e["meta"] is not None else None,
                 e["created_at"], e["prev_hash"], built["entry_json"],
                 built["entry_hash"], built["signature"], e["key_id"]))
            got = cur.fetchone()
        if not got:
            # ON CONFLICT swallowed it: an identical entry_hash already exists.
            # Never report success for a row that did not land.
            raise LedgerUnavailable("ledger entry was not written")
        conn.commit()
    except LedgerUnavailable:
        _rollback(conn)
        raise
    except Exception as exc:  # noqa: BLE001 — any DB failure is "not written"
        _rollback(conn)
        raise LedgerUnavailable(f"ledger write failed: {type(exc).__name__}") from exc
    return {"seq": got[0], "entry_hash": built["entry_hash"],
            "prev_hash": built["entry"]["prev_hash"],
            "created_at": built["entry"]["created_at"],
            "pii_commitment": built["entry"]["pii_commitment"]}


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


# ── verification ──────────────────────────────────────────────────────────

def _row_created_iso(row) -> str | None:
    v = row.get("created_at")
    if isinstance(v, datetime):
        return iso_utc(v)
    return v if isinstance(v, str) else None


def check_entry(row: dict, keys) -> dict:
    """Integrity of ONE stored entry, independent of its neighbours.

    signature_valid: True / False, or None only when NO key is configured at
    all. A key id we do not hold is False — see the module docstring."""
    keys = _as_keys(keys)
    entry_json = row.get("entry_json") or ""
    hash_valid = hmac.compare_digest(sha256_hex(entry_json), row.get("entry_hash") or "")
    try:
        parsed = json.loads(entry_json)
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if not keys:
        signature_valid = None
    else:
        key = _key_for(parsed, keys)
        signature_valid = key is not None and hmac.compare_digest(
            sign(key, row.get("entry_hash") or ""), row.get("signature") or "")
    columns_consistent = (
        parsed.get("lead_id") == row.get("lead_id")
        and parsed.get("event") == row.get("event")
        and parsed.get("listing_id") == row.get("listing_id")
        and parsed.get("prev_hash") == row.get("prev_hash")
        and parsed.get("created_at") == _row_created_iso(row))
    return {"hash_valid": hash_valid, "signature_valid": signature_valid,
            "columns_consistent": columns_consistent, "entry": parsed}


def pii_state(row: dict, entry: dict, keys) -> str:
    """intact | redacted | mismatch | none | unverifiable."""
    committed = entry.get("pii_commitment")
    if not committed:
        return "none"
    if not row.get("email"):
        return "redacted"
    key = _key_for(entry, _as_keys(keys))
    if key is None:
        return "unverifiable"
    requirement = row.get("requirement")
    if isinstance(requirement, str):
        try:
            requirement = json.loads(requirement)
        except Exception:
            pass
    raw = {"email": row.get("email"), "name": row.get("name"),
           "role": row.get("role"), "company": row.get("company"),
           "message": row.get("message"), "requirement": requirement}
    return "intact" if hmac.compare_digest(pii_commitment(key, raw), committed) else "mismatch"


def verify_chain(rows, keys) -> dict:
    """Walk rows in ascending seq from the FIRST entry. Every row must hash to
    its stored hash, agree with its own columns, link to the previous row's
    hash (the first row to GENESIS_HASH) and — whenever any key is configured —
    carry a signature from a key we hold."""
    keys = _as_keys(keys)
    expected_prev = GENESIS_HASH
    checked = 0
    first_break = None
    head_seq = head_hash = None
    for row in rows:
        checked += 1
        res = check_entry(row, keys)
        linked = (row.get("prev_hash") == expected_prev
                  and res["entry"].get("prev_hash") == expected_prev)
        signed = res["signature_valid"] is True if keys else True
        ok = res["hash_valid"] and linked and res["columns_consistent"] and signed
        if not ok and first_break is None:
            first_break = row.get("seq")
        expected_prev = row.get("entry_hash")
        head_seq, head_hash = row.get("seq"), row.get("entry_hash")
    return {"intact": first_break is None, "entries_checked": checked,
            "first_break_seq": first_break, "head_seq": head_seq,
            "head_hash": head_hash, "signatures_checked": bool(keys)}


# ── lead status ───────────────────────────────────────────────────────────

_STATUS_RANK = {"pending_email_confirmation": 0, "registered": 1,
                "operator_notified": 2, "introduced": 3}
_STATUS_BY_EVENT = {"email_confirmed": "registered",
                    "operator_notified": "operator_notified",
                    "introduced": "introduced"}


def lead_status(events) -> str | None:
    """Furthest-along status a lead has reached; `withdrawn` is terminal."""
    status = None
    for ev in events:
        name = ev.get("event")
        if name == "withdrawn":
            return "withdrawn"
        if name in LEAD_OPENING_EVENTS:
            candidate = ("registered" if ev.get("email_verified")
                         else "pending_email_confirmation")
        else:
            candidate = _STATUS_BY_EVENT.get(name)
        if candidate and (status is None
                          or _STATUS_RANK[candidate] > _STATUS_RANK[status]):
            status = candidate
    return status


# ── tokens ────────────────────────────────────────────────────────────────

def _mac(secret: bytes, message: str) -> str:
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def confirm_token(secret: bytes, lead_id: str, commitment: str | None,
                  issued_at: float | None = None) -> str:
    """Bound to the lead AND the committed prospect fields, so a token cannot be
    replayed against another lead or survive an edit of who asked."""
    ts = int(time.time() if issued_at is None else issued_at)
    return f"{ts}.{_mac(secret, f'confirm|{lead_id}|{commitment or ''}|{ts}')}"


def check_confirm_token(secret: bytes, lead_id: str, commitment: str | None,
                        token: str, now: float | None = None,
                        max_age_s: int = CONFIRM_TOKEN_TTL_S) -> bool:
    ts, mac = _split_token(token, 2)
    if ts is None:
        return False
    if not _fresh(ts, now, max_age_s):
        return False
    expected = _mac(secret, f"confirm|{lead_id}|{commitment or ''}|{ts}")
    return hmac.compare_digest(mac, expected)


def operator_token(secret: bytes, listing_id: int, issued_at: float | None = None) -> str:
    ts = int(time.time() if issued_at is None else issued_at)
    return f"{int(listing_id)}.{ts}.{_mac(secret, f'operator|{int(listing_id)}|{ts}')}"


def check_operator_token(secret: bytes, listing_id: int, token: str,
                         now: float | None = None,
                         max_age_s: int = OPERATOR_TOKEN_TTL_S) -> bool:
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return False
    if int(parts[0]) != int(listing_id):
        return False
    ts = int(parts[1])
    if not _fresh(ts, now, max_age_s):
        return False
    expected = _mac(secret, f"operator|{int(listing_id)}|{ts}")
    return hmac.compare_digest(parts[2], expected)


def _split_token(token: str, n: int):
    parts = (token or "").strip().split(".")
    if len(parts) != n or not parts[0].isdigit():
        return None, None
    return int(parts[0]), parts[1]


def _fresh(ts: int, now: float | None, max_age_s: int) -> bool:
    current = time.time() if now is None else now
    if ts > current + _CLOCK_SKEW_S:
        return False
    return (current - ts) <= max_age_s
