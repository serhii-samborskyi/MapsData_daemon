from __future__ import annotations

import re
from typing import Iterable, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

PREFERRED_MAILBOX_ORDER = ["info@", "contact@", "hello@", "support@", "sales@", "admin@"]

PUBLIC_PROVIDERS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "yandex.com",
    "gmx.com",
    "mail.com",
    "sbcglobal.net",
    "att.net",
    "bellsouth.net",
    "verizon.net",
    "comcast.net",
}

BLOCK_SUBSTRINGS = [
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    "wixpress.com",
    "sentry.io",
    "noreply",
    "no-reply",
    "abuse",
    "subscribe",
    "mailer-daemon",
    "example.com",
    "domain.com",
    "email.com",
    "yourname",
    "wix.com",
    ".js",
    ".css",
    ".html",
    ".php",
    ".asp",
]

ALLOWED_DOMAIN_SUFFIXES = {
    ".com",
    ".net",
    ".org",
    ".edu",
    ".gov",
    ".us",
}

COMMON_SECOND_LEVEL_DOMAINS = {"co", "com", "org", "net", "gov", "edu", "ac"}

BLOCKED_EMAIL_DOMAINS = {
    "cloudflareinsights.com",
    "mysite.com",
    "example.com",
    "domain.com",
    "email.com",
    "localhost",
    "invalid",
    "h.com",
    "ic.com",
    "wp.com",
    "linkedin.com",
}

GENERIC_LOCAL_PARTS = {
    "info",
    "contact",
    "hello",
    "support",
    "sales",
    "admin",
    "office",
    "mail",
    "email",
    "team",
    "usa",
}


def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" not in v:
        v = "https://" + v
    parsed = urlparse(v)
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    host = host.split("@")[-1].split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(value: str) -> str:
    host = normalize_domain(value)
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) < 2:
        return host
    if len(parts) >= 3 and parts[-2] in COMMON_SECOND_LEVEL_DOMAINS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_email_candidate(raw: str) -> str:
    if not raw:
        return ""
    s = unquote(raw).strip().strip("<>\"'")
    if s.lower().startswith("mailto:"):
        s = s.split(":", 1)[1]
    for sep in ("?", "#", "&", ",", ";"):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = s.strip().strip("()[]{}<>\"'.,:")
    return s.lower()


def deobfuscate_text_for_emails(text: str) -> str:
    t = " " + (text or "") + " "
    t = re.sub(r"\s*(?:\(|\[)?at(?:\)|\])?\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*(?:\(|\[)?dot(?:\)|\])?\s*", ".", t, flags=re.I)
    t = re.sub(r"\s+@\s+", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)
    return t


def extract_candidate_emails_from_text(text: str) -> Set[str]:
    found: Set[str] = set()
    if not text:
        return found
    for m in EMAIL_RE.finditer(text):
        email = normalize_email_candidate(m.group(0))
        if email:
            found.add(email)
    deob = deobfuscate_text_for_emails(text)
    if deob != text:
        for m in EMAIL_RE.finditer(deob):
            email = normalize_email_candidate(m.group(0))
            if email:
                found.add(email)
    return found


def is_allowed_domain(domain_part: str) -> bool:
    host = normalize_domain(domain_part)
    if not host or "." not in host:
        return False
    if host in PUBLIC_PROVIDERS:
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_DOMAIN_SUFFIXES)


def _is_blocked_pattern(email: str) -> bool:
    lower = email.lower()
    if any(sub in lower for sub in BLOCK_SUBSTRINGS):
        return True
    if any(token in lower for token in ("test@", "example@", "noreply@", "no-reply@", "privacy@")):
        return True
    return False


def _split_email(email: str) -> Optional[Tuple[str, str]]:
    if email.count("@") != 1:
        return None
    local, domain = email.split("@", 1)
    local = local.strip().lower()
    domain = normalize_domain(domain)
    if not local or not domain:
        return None
    return local, domain


def _is_blocked_email_domain(domain: str) -> bool:
    d = normalize_domain(domain)
    if not d:
        return True
    if d in BLOCKED_EMAIL_DOMAINS:
        return True
    for blocked in BLOCKED_EMAIL_DOMAINS:
        if d.endswith("." + blocked):
            return True
    return False


def is_valid_email_candidate(email: str) -> bool:
    normalized = normalize_email_candidate(email)
    if not normalized or _is_blocked_pattern(normalized):
        return False

    split = _split_email(normalized)
    if not split:
        return False
    local, domain = split

    if _is_blocked_email_domain(domain):
        return False

    # Reject emails hosted on subdomains (e.g. info@sub.example.com).
    if normalize_domain(domain) != registrable_domain(domain):
        return False

    # Reject one-letter root domains (e.g. a.com, h.net).
    root_label = registrable_domain(domain).split(".", 1)[0]
    if len(root_label) <= 1:
        return False

    if len(local) > 64 or local.startswith((".", "-")) or local.endswith((".", "-")):
        return False
    if ".." in local or not re.fullmatch(r"[a-z0-9._%+\-]+", local):
        return False

    if len(domain) > 253 or domain.startswith("-") or domain.endswith("-") or ".." in domain:
        return False
    if re.search(r"\d+-\d+", domain):
        return False

    labels = domain.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not re.fullmatch(r"[a-z0-9-]+", label):
            return False

    digit_count = sum(1 for c in domain if c.isdigit())
    if digit_count > 4:
        return False

    tld = labels[-1]
    if not re.fullmatch(r"[a-z]{2,24}", tld):
        return False

    return is_allowed_domain(domain)


def filter_valid_emails(candidates: Iterable[str]) -> Set[str]:
    valid: Set[str] = set()
    for raw in candidates:
        email = normalize_email_candidate(raw)
        if email and is_valid_email_candidate(email):
            valid.add(email)
    return valid


def is_same_business_domain(email: str, business_domain: str) -> bool:
    split = _split_email(normalize_email_candidate(email))
    if not split:
        return False
    _, email_domain = split
    business_host = normalize_domain(business_domain)
    if not business_host:
        return False
    if email_domain == business_host:
        return True
    if email_domain.endswith("." + business_host):
        return True
    return registrable_domain(email_domain) == registrable_domain(business_host)


def _mailbox_priority(email: str) -> int:
    for i, prefix in enumerate(PREFERRED_MAILBOX_ORDER):
        if email.startswith(prefix):
            return i
    return 999


def _business_token(value: str) -> str:
    root = registrable_domain(value)
    if not root:
        return ""
    main = root.split(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", main.lower())


def _business_relevance_score(email: str, business_domain: str) -> int:
    local = email.split("@", 1)[0]
    local_token = re.sub(r"[^a-z0-9]+", "", local.lower())
    biz_token = _business_token(business_domain)
    if not biz_token or not local_token:
        return 2
    if biz_token in local_token or local_token in biz_token:
        return 0
    # allow partial match by chunks (e.g. bakerjones vs baker-jones-heating)
    chunks = [c for c in re.findall(r"[a-z0-9]+", biz_token) if len(c) >= 4]
    if any(chunk in local_token for chunk in chunks):
        return 1
    return 2


def _generic_local_penalty(email: str) -> int:
    local = email.split("@", 1)[0].lower()
    return 1 if local in GENERIC_LOCAL_PARTS else 0


def _domain_relevance_score(email: str, business_domain: str) -> int:
    email_domain = email.split("@", 1)[1]
    biz_token = _business_token(business_domain)
    email_token = _business_token(email_domain)
    if not biz_token or not email_token:
        return 2
    if biz_token == email_token:
        return 0
    if biz_token in email_token or email_token in biz_token:
        return 1
    return 2


def pick_best_business_email(candidates: Iterable[str], business_domain: str, allow_public: bool = True) -> Optional[str]:
    valid = filter_valid_emails(candidates)
    if not valid:
        return None

    business_host = normalize_domain(business_domain)

    def sort_key(email: str) -> Tuple[int, int, int, int]:
        email_domain = email.split("@", 1)[1]
        exact = 0 if email_domain == business_host else 1
        business_relevance = _business_relevance_score(email, business_host)
        local_penalty = _generic_local_penalty(email)
        return (_mailbox_priority(email), business_relevance, local_penalty, exact)

    same_business = [e for e in valid if is_same_business_domain(e, business_host)]
    if same_business:
        same_business.sort(key=sort_key)
        return same_business[0]

    public_emails = [e for e in valid if e.split("@", 1)[1] in PUBLIC_PROVIDERS]
    others = [e for e in valid if e not in public_emails]
    if others:
        others.sort(
            key=lambda e: (
                _domain_relevance_score(e, business_host),
                _business_relevance_score(e, business_host),
                _generic_local_penalty(e),
                _mailbox_priority(e),
                -len(e),
            )
        )
        return others[0]

    if allow_public and public_emails:
        public_emails.sort(
            key=lambda e: (
                _business_relevance_score(e, business_host),
                _generic_local_penalty(e),
                _mailbox_priority(e),
                -len(e),
            )
        )
        return public_emails[0]

    return None
