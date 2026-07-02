"""Shared email deliverability validator (2026-06-18).

This is a PURE, stdlib-only module (plus an OPTIONAL, guarded dnspython import)
used on the identify/bind path — bind_email, /keys/identify, and the recover
flow all lean on it to decide whether an email is good enough to bind to a key
and to send transactional mail (key recovery + upgrade receipts ONLY — we make
no marketing/digest promise).

SOFT-FAIL PHILOSOPHY
--------------------
The cardinal rule on the identify/bind path is: never punish the user for OUR
infrastructure failing. We only return ok=False when the email is DEFINITIVELY
bad for a reason that is the *sender's* fault:
  * format invalid
  * a role account (postmaster@, abuse@, noreply@, ...) — mail to these is
    routinely auto-rejected or black-holed, so binding one is a dead end
  * a known disposable/throwaway domain — the bound address evaporates and key
    recovery becomes impossible
  * the domain DEFINITIVELY has no MX and no A record (it can receive no mail)

For everything else — a DNS resolver timeout, a transient SERVFAIL, dnspython
not installed, a flaky network — we SOFT-FAIL to ok=True. A real address must
never be blocked because our resolver hiccupped. The key keeps working
regardless; email is purely an optional convenience for recovery/receipts, and
a false reject is far worse than a false accept here.

Public interface (stable contract):
  validate_email(email) -> (ok: bool, reason: str|None, normalized: str)
  is_role_account(localpart) -> bool
  has_mx(domain, timeout=2.5) -> bool|None   # None = couldn't determine
"""
import re
import socket

# Optional dnspython — guarded so a missing dep can NEVER break boot/import.
try:  # pragma: no cover - environment dependent
    import dns.resolver as _dns_resolver  # type: ignore
    _HAVE_DNSPYTHON = True
except Exception:  # ImportError, or a broken partial install
    _dns_resolver = None
    _HAVE_DNSPYTHON = False

# Default DNS timeout — kept short so the identify/bind path never stalls on us.
_DNS_TIMEOUT = 2.5

# A simple-but-sane email shape. We deliberately do NOT chase RFC 5322 fully:
# the MX/A check and the role/disposable filters do the real work, and an
# over-strict regex would reject valid-but-unusual addresses (a false reject,
# the exact thing the soft-fail philosophy forbids).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Role / functional accounts. Mail to these is commonly auto-handled, shared, or
# rejected — a poor target to BIND a personal key to. Compared case-folded
# against the localpart (and against the localpart before any "+tag").
ROLE_LOCALPARTS = frozenset({
    "postmaster", "abuse", "noreply", "no-reply", "no_reply", "donotreply",
    "do-not-reply", "mailer-daemon", "mailerdaemon", "hostmaster", "webmaster",
    "admin", "administrator", "root", "sysadmin", "support", "help", "helpdesk",
    "info", "contact", "sales", "marketing", "billing", "accounts", "accounting",
    "security", "privacy", "legal", "compliance", "hr", "jobs", "careers",
    "press", "media", "team", "office", "mail", "email", "spam", "bounce",
    "bounces", "notifications", "notification", "alerts", "noc", "ops",
    "operations", "it", "service", "services", "feedback", "enquiries",
    "enquiry", "inquiries", "inquiry", "newsletter", "subscribe", "unsubscribe",
    "automated", "robot", "daemon", "nobody", "null", "void", "test", "testing",
})

# Placeholder / documentation domains. These PASS the MX/A check (example.com
# has real DNS) but no human reads them — AI agents copying a docs example bind
# owner@example.com verbatim, which makes the key unrecoverable and pollutes
# the identified-keys funnel metric (live audit 2026-07-01: 11 of 51 bound
# mcp_dev_keys emails were @example.com). RFC 2606/6761 reserved names plus the
# generic fill-ins agents actually send. Hard reject, reason='placeholder'.
PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "example.edu",
    "yourdomain.com", "yourcompany.com", "a.com",
})
_RESERVED_TLDS = (".test", ".invalid", ".localhost", ".example", ".local")

# Curated disposable / throwaway / temp-mail domains. Vendored as a real set
# (not a handful of substrings) so we catch the common throwaways without an
# external dependency or network call. Binding to one of these makes key
# recovery impossible, so it's a hard reject. List is intentionally broad but
# conservative — only domains whose *whole purpose* is disposable inboxes.
DISPOSABLE_DOMAINS = frozenset({
    # mailinator family
    "mailinator.com", "mailinator.net", "mailinator2.com", "reallymymail.com",
    "binkmail.com", "bobmail.info", "chammy.info", "devnullmail.com",
    "letthemeatspam.com", "mailin8r.com", "notmailinator.com", "sogetthis.com",
    "spamhereplease.com", "streetwisemail.com", "suremail.info", "thisisnotmyrealemail.com",
    "tradermail.info", "veryrealemail.com", "zippymail.info",
    # guerrillamail family
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "guerrillamail.de", "guerrillamailblock.com", "grr.la", "sharklasers.com",
    "spam4.me", "pokemail.net", "guerrillamail.info",
    # 10minute / temp families
    "10minutemail.com", "10minutemail.net", "10minutemail.org", "10minutemail.co.uk",
    "10minemail.com", "20minutemail.com", "20minutemail.it", "30minutemail.com",
    "33mail.com", "tempmail.com", "temp-mail.org", "temp-mail.com", "temp-mail.io",
    "tempmail.net", "tempmailo.com", "tempmail.plus", "tempail.com", "tempmailaddress.com",
    "tempinbox.com", "tempemail.com", "tempemail.net", "tempr.email", "tempmailer.com",
    "tempmailer.de", "minuteinbox.com", "emailondeck.com", "fakemailgenerator.com",
    # yopmail family
    "yopmail.com", "yopmail.net", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf",
    "nospam.ze.tc", "nomail.xl.cx", "mega.zik.dj", "speed.1s.fr", "courriel.fr.nf",
    "moncourrier.fr.nf", "monemail.fr.nf", "monmail.fr.nf",
    # throwaway / trashmail families
    "throwawaymail.com", "throwawayemailaddresses.com", "throwam.com",
    "trashmail.com", "trashmail.net", "trashmail.org", "trashmail.de", "trashmail.me",
    "trashmail.ws", "trash-mail.com", "trash-mail.de", "trash-mail.at",
    "kurzepost.de", "objectmail.com", "proxymail.eu", "rcpt.at", "wegwerfmail.de",
    "wegwerfmail.net", "wegwerfmail.org", "wegwerfemail.de", "wegwerfemailadresse.com",
    # getnada / nada
    "getnada.com", "nada.email", "nada.ltd",
    # dispostable / fakeinbox
    "dispostable.com", "fakeinbox.com", "fakeinbox.net", "fakemail.net",
    "fake-mail.ml", "fakemailbox.com",
    # mohmal / discard / spambog / mvrht / etc.
    "mohmal.com", "mohmal.in", "mohmal.tech", "discard.email", "discardmail.com",
    "discardmail.de", "spambog.com", "spambog.de", "spambog.ru", "spambox.us",
    "spam.la", "spamgourmet.com", "spamfree24.org", "spamfree24.com",
    "spamfree24.de", "spamfree24.eu", "spamfree24.info", "spamfree24.net",
    # maildrop / mailcatch / mailnesia / mailnull
    "maildrop.cc", "mailcatch.com", "mailnesia.com", "mailnull.com", "mailexpire.com",
    "mailmetrash.com", "mailimate.com", "mailtemp.info", "mailtothis.com",
    "mailzilla.com", "mailzilla.org", "mintemail.com", "mt2009.com", "mytrashmail.com",
    # inbox families
    "inboxalias.com", "inboxbear.com", "incognitomail.com", "incognitomail.org",
    "inboxkitten.com", "tempinbox.xyz",
    # one-off / well-known throwaways
    "anonbox.net", "anonymbox.com", "bugmenot.com", "burnermail.io", "byom.de",
    "deadaddress.com", "despam.it", "dodgeit.com", "dodgit.com", "dontreg.com",
    "dontsendmespam.de", "e4ward.com", "emailtemporanea.com", "emailtemporanea.net",
    "emailwarden.com", "emailxfer.com", "explodemail.com", "fastacura.com",
    "filzmail.com", "fizmail.com", "frapmail.com", "garliclife.com", "get1mail.com",
    "get2mail.fr", "gishpuppy.com", "great-host.in", "haltospam.com", "harakirimail.com",
    "hidemail.de", "hochsitze.com", "hotpop.com", "ieh-mail.de", "imails.info",
    "jetable.com", "jetable.net", "jetable.org", "jnxjn.com", "junk1e.com",
    "kasmail.com", "killmail.com", "killmail.net", "klzlk.com", "kulturbetrieb.info",
    "lifebyfood.com", "link2mail.net", "litedrop.com", "lookugly.com", "lr78.com",
    "maboard.com", "mail-temporaire.fr", "mail.by", "mail114.net", "mail4trash.com",
    "mailbidon.com", "mailblocks.com", "mailcat.biz", "maileater.com", "mailfreeonline.com",
    "mailguard.me", "mailin8r.com", "mailinater.com", "mailme.lv", "mailmoat.com",
    "mailquack.com", "mailseal.de", "mailshell.com", "mailsiphon.com", "mailslapping.com",
    "manybrain.com", "mbx.cc", "meltmail.com", "messagebeamer.de", "mierdamail.com",
    "moburl.com", "msa.minsmail.com", "mspeciosa.com", "myphantomemail.com", "neomailbox.com",
    "nepwk.com", "nervmich.net", "nervtmich.net", "netmails.com", "netmails.net",
    "no-spam.ws", "nobulk.com", "noclickemail.com", "nogmailspam.info", "nomail2me.com",
    "nospamfor.us", "nowmymail.com", "objectmail.com", "obobbo.com", "onewaymail.com",
    "owlpic.com", "pjjkp.com", "punkass.com", "putthisinyourspamdatabase.com",
    "quickinbox.com", "rejectmail.com", "rklips.com", "rppkn.com", "safe-mail.net",
    "sandelf.de", "saynotospams.com", "selfdestructingmail.com", "shieldedmail.com",
    "shitmail.me", "shortmail.net", "sibmail.com", "skeefmail.com", "slopsbox.com",
    "smellfear.com", "snakemail.com", "sneakemail.com", "snkmail.com", "sofort-mail.de",
    "sofimail.com", "spamavert.com", "spambob.com", "spambob.net", "spambob.org",
    "spamcero.com", "spamday.com", "spamex.com", "spamherelots.com", "spamhole.com",
    "spamify.com", "spaml.com", "spaml.de", "spammotel.com", "spamobox.com",
    "spamslicer.com", "spamspot.com", "spamthis.co.uk", "spamtrail.com", "spamtroll.net",
    "supergreatmail.com", "supermailer.jp", "talkinator.com", "teleworm.com",
    "teleworm.us", "thanksnospam.info", "thankyou2010.com", "thecloudindex.com",
    "thismail.net", "tilien.com", "tmail.ws", "tmailinator.com", "toomail.biz",
    "tortmail.com", "trbvm.com", "turual.com", "twinmail.de", "tyldd.com",
    "uggsrock.com", "upliftnow.com", "uplipht.com", "venompen.com", "viditag.com",
    "viralplays.com", "vpn.st", "vsimcard.com", "vubby.com", "wasteland.rfc822.org",
    "webm4il.info", "wh4f.org", "willhackforfood.biz", "willselfdestruct.com",
    "winemaven.info", "wronghead.com", "wuzup.net", "wuzupmail.net", "xagloo.com",
    "xemaps.com", "xents.com", "xmaily.com", "xoxy.net", "yep.it", "yogamaven.com",
    "you-spam.com", "yuurok.com", "zoaxe.com", "zoemail.com", "zoemail.net",
    "zoemail.org", "fivemail.de", "mailde.de", "mailde.info", "fixmail.tk",
})


def _normalize(email):
    """Lowercase + strip surrounding whitespace. Returns "" for non-strings."""
    if not isinstance(email, str):
        return ""
    return email.strip().lower()


def _split(norm):
    """Return (localpart, domain) for a normalized email, or ("", "") if it has
    no single '@'."""
    if norm.count("@") != 1:
        return "", ""
    local, domain = norm.split("@", 1)
    return local, domain


def is_role_account(localpart):
    """True if the localpart looks like a role/functional account.

    The '+tag' suffix is stripped first (admin+ci@ is still an admin account),
    and matching is case-insensitive. A bare localpart of e.g. "admin" or
    "no-reply" returns True."""
    if not isinstance(localpart, str):
        return False
    lp = localpart.strip().lower()
    # Drop any plus-addressing tag: "admin+foo" -> "admin".
    if "+" in lp:
        lp = lp.split("+", 1)[0]
    return lp in ROLE_LOCALPARTS


def has_mx(domain, timeout=_DNS_TIMEOUT):
    """Best-effort 'can this domain receive mail?' check.

    Returns:
      True  -> domain has an MX record (or, via the A-record fallback, at least
               resolves to an address that could plausibly accept mail).
      False -> domain DEFINITIVELY has no MX and no A/AAAA record.
      None  -> we couldn't determine it (resolver error/timeout, no dnspython
               AND a non-NXDOMAIN socket error). Callers MUST soft-fail on None
               — None is "our problem, not the user's", so do not block on it.

    Prefers dnspython (real MX lookup) when available; otherwise falls back to
    socket.getaddrinfo for an A/AAAA record. Per RFC 5321, a domain with an A
    record but no MX can still receive mail (the A record is the implicit MX),
    so an A-record hit counts as deliverable-enough."""
    if not domain or not isinstance(domain, str):
        return None
    domain = domain.strip().rstrip(".").lower()
    if not domain:
        return None

    if _HAVE_DNSPYTHON and _dns_resolver is not None:
        try:
            resolver = _dns_resolver.Resolver()
            resolver.lifetime = timeout
            resolver.timeout = timeout
            answers = resolver.resolve(domain, "MX")
            if len(answers) > 0:
                return True
            # Empty MX answer set is unusual; fall through to A-record check.
        except _dns_resolver.NXDOMAIN:
            # Domain definitively does not exist -> no MX, no A. Hard False.
            return False
        except _dns_resolver.NoAnswer:
            # Domain exists but has no MX record — try the A-record fallback
            # below before concluding it can't receive mail.
            pass
        except Exception:
            # Timeout / SERVFAIL / resolver config / network — OUR problem.
            # Try the socket fallback; if that's also inconclusive we return
            # None and the caller soft-fails.
            pass

    # A/AAAA fallback (also the no-dnspython path). A successful lookup means
    # the domain resolves and is deliverable-enough (implicit MX per RFC 5321).
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(domain, None)
        return bool(infos)
    except socket.gaierror as e:
        # EAI_NONAME / EAI_NODATA -> the name truly doesn't resolve: hard False.
        # Any other gaierror (e.g. temporary failure EAI_AGAIN) -> inconclusive.
        err = getattr(e, "errno", None)
        if err in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
            return False
        return None
    except Exception:
        # Resolver/network error on our side -> inconclusive, soft-fail.
        return None
    finally:
        # Restore the global default so we don't leak a timeout onto unrelated
        # socket users in the same process.
        socket.setdefaulttimeout(None)


def validate_email(email):
    """Validate an email for binding/transactional use.

    Returns (ok: bool, reason: str|None, normalized: str).
      ok=True  -> deliverable-enough to accept (reason is None).
      ok=False -> definitively bad; reason is one of:
                    'format'        (not a valid email shape)
                    'role_account'  (postmaster/abuse/noreply/... localpart)
                    'disposable'    (known throwaway domain)
                    'placeholder'   (RFC-2606 reserved / docs-example domain)
                    'no_mx'         (domain has no MX AND no A record)

    SOFT-FAIL: a resolver error/timeout (has_mx -> None) yields (True, None,
    norm). We never block a real user on our own DNS troubles. An empty/None
    email is treated as 'format' (the caller decides whether email was even
    required — on the bind path it is OPTIONAL and absence gates nothing)."""
    norm = _normalize(email)
    if not norm or not _EMAIL_RE.match(norm):
        return False, "format", norm

    local, domain = _split(norm)
    if not local or not domain:
        return False, "format", norm

    # Basic structural sanity that the regex alone doesn't guarantee.
    if ".." in norm or domain.startswith(".") or domain.endswith(".") \
            or "." not in domain:
        return False, "format", norm

    if is_role_account(local):
        return False, "role_account", norm

    if domain in DISPOSABLE_DOMAINS:
        return False, "disposable", norm

    if domain in PLACEHOLDER_DOMAINS or domain.endswith(_RESERVED_TLDS):
        return False, "placeholder", norm

    mx = has_mx(domain)
    if mx is False:
        return False, "no_mx", norm
    # mx is True  -> deliverable.
    # mx is None  -> SOFT-FAIL: our resolver couldn't decide; accept the user.
    return True, None, norm
