"""2026-07-10 funnel audit — the role-localpart validator soft-rejected the
shared-inbox addresses real operators actually use (info@/admin@/support@/
it@/ops@ are often THE working address at a small data-center/infra shop),
bouncing genuine binds with reason='role_account'. The list is now dead-ends
only. Pure unit tests: no DB, and has_mx is monkeypatched so no network.
"""
import routes.email_validation as ev


# Human-monitored shared inboxes that MUST bind (the audit's named examples
# plus the rest of the operator-role family the old list rejected).
OPERATOR_ROLES = [
    "info", "admin", "administrator", "support", "it", "ops", "operations",
    "contact", "sales", "billing", "security", "legal", "hr", "press",
    "team", "office", "noc", "help", "helpdesk", "webmaster", "root",
    "sysadmin", "hostmaster", "feedback", "inquiries", "alerts",
    "notifications",
]

# Machine/dead-end localparts that must STAY rejected — mail to these reaches
# no human by construction, so a bound key becomes unrecoverable.
DEAD_END_ROLES = [
    "postmaster", "abuse", "noreply", "no-reply", "no_reply", "donotreply",
    "do-not-reply", "mailer-daemon", "mailerdaemon", "bounce", "bounces",
    "spam", "nobody", "null", "void", "daemon", "robot", "automated",
    "test", "testing", "newsletter", "subscribe", "unsubscribe",
]


def test_operator_roles_are_not_role_accounts():
    for lp in OPERATOR_ROLES:
        assert not ev.is_role_account(lp), (
            f"{lp}@ is a real operator address and must be bindable")


def test_dead_end_roles_still_rejected():
    for lp in DEAD_END_ROLES:
        assert ev.is_role_account(lp), (
            f"{lp}@ is a machine dead end and must stay rejected")


def test_plus_tag_stripping_still_applies():
    assert ev.is_role_account("noreply+ci")
    assert not ev.is_role_account("info+dchub")


def test_validate_email_accepts_operator_role(monkeypatch):
    # No network: pretend every domain has MX.
    monkeypatch.setattr(ev, "has_mx", lambda d, timeout=None: True)
    ok, reason, norm = ev.validate_email("Info@Operator-Site.com")
    assert ok and reason is None and norm == "info@operator-site.com"


def test_validate_email_still_rejects_dead_end(monkeypatch):
    monkeypatch.setattr(ev, "has_mx", lambda d, timeout=None: True)
    ok, reason, _ = ev.validate_email("noreply@operator-site.com")
    assert not ok and reason == "role_account"


def test_validate_email_still_rejects_disposable_and_placeholder(monkeypatch):
    monkeypatch.setattr(ev, "has_mx", lambda d, timeout=None: True)
    assert ev.validate_email("info@mailinator.com")[1] == "disposable"
    assert ev.validate_email("info@example.com")[1] == "placeholder"
