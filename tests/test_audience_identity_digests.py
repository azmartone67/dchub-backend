"""Named address lists stored as digests (2026-09-25).

dchub-backend is a public repo, so the harness-persona list and the extra
operator mailboxes are stored as SHA-256 of normalize_email(address). These
tests pin the mechanism without spelling any of those addresses out.
"""
import hashlib

import routes._audience_identity as ai


def test_digest_is_sha256_of_the_normalized_address():
    want = hashlib.sha256(b"ab@gmail.com").hexdigest()
    assert ai.email_digest(" A.B+tag@GoogleMail.com ") == want


def test_every_stored_digest_is_a_sha256_hex():
    for d in ai._HARNESS_PERSONA_DIGESTS | ai._OPERATOR_EMAIL_DIGESTS:
        assert len(d) == 64 and int(d, 16) >= 0
    assert len(ai._HARNESS_PERSONA_DIGESTS) == 11
    assert len(ai._OPERATOR_EMAIL_DIGESTS) == 2


def test_a_digest_listed_address_is_a_persona_in_any_spelling(monkeypatch):
    monkeypatch.setattr(ai, "_HARNESS_PERSONA_DIGESTS",
                        frozenset({ai.email_digest("x.y@gmail.com")}))
    assert ai.is_harness_persona_email("X.Y+1@googlemail.com")
    assert not ai.is_harness_persona_email("x.z@gmail.com")
    assert not ai.is_harness_persona_email("")
    assert not ai.is_harness_persona_email(None)


def test_the_env_list_only_widens(monkeypatch):
    monkeypatch.setenv("DCHUB_HARNESS_PERSONA_EMAILS", "new.persona@acme.io, junk")
    assert ai.is_harness_persona_email("new.persona@acme.io")
    monkeypatch.setenv("DCHUB_HARNESS_PERSONA_EMAILS", "")
    assert not ai.is_harness_persona_email("new.persona@acme.io")


def test_a_digest_listed_operator_is_internal(monkeypatch):
    monkeypatch.setattr(ai, "_OPERATOR_EMAIL_DIGESTS",
                        frozenset({ai.email_digest("boss@corp.example")}))
    assert ai.is_operator_email("Boss@Corp.Example")
    assert ai.is_internal_email("boss@corp.example")
    assert not ai.is_operator_email("someone@corp.example")
