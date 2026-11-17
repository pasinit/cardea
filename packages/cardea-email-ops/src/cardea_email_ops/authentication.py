"""Sender authentication of an inbound reply, failing closed (ADR-039).

A reply is accepted only when its From domain is authenticated. Otherwise anyone could
forge the From header. The verdict is `True` (authenticated), `False` (a mechanism
failed, or none passed) or `None` (unknown). The operation accepts only `True`, unless the
host owner sets `accept_unauthenticated`.

Authenticated means DMARC pass, or failing that an SPF or DKIM pass for a domain aligned
with the From domain. Alignment is relaxed (RFC 7489 section 3.1): the domains are equal,
or one is a subdomain of the other. This approximates the organizational domain without
the public suffix list. It is stricter, never looser: two subdomains of one organization
that share neither name do not align here.

IMAP: the verdict comes only from an RFC 8601 `Authentication-Results` header that the
configured receiving server added. That is the topmost `Authentication-Results` header,
and its authserv-id must equal the configured one. A receiving server prepends its own
header, so anything a sender planted sits below it. If the topmost header is absent or
names another authserv-id, the verdict is unknown.

AgentMail: the verdict comes from the message's `authentication_results` field and its
`unauthenticated` label (https://docs.agentmail.to/api-reference/inboxes/messages/get,
read 2026-11-17). The docs say:
- the label is AgentMail's acceptance decision, and it is looser than alignment: a message
  whose only pass is an unaligned envelope SPF is delivered without the label;
- `spf` is reported only when the envelope domain is aligned with the From domain;
- `dkim_domain` is the signing domain and must be compared with the From domain;
- `dmarc` is the verdict for the From domain, named in `dmarc_domain`;
- the field is absent on messages received before 2026-09-09, and a verdict that could
  not be computed is absent.

The absence of the label therefore does not count as authentication. The message must
also carry an aligned pass in `authentication_results`.
"""

import re
from collections.abc import Mapping
from email.message import Message
from email.utils import parseaddr
from typing import Any

_COMMENT = re.compile(r"\([^()]*\)")
_TOKEN = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*([A-Za-z0-9_.-]+)")
_PROPERTY = re.compile(r"([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\s*=\s*(\"[^\"]*\"|[^\s;]+)")


def domain_of(address: str) -> str:
    """The domain of an address or a From value, lower-case; empty if there is none."""
    value = parseaddr(address)[1] or address
    return value.rsplit("@", 1)[1].strip().strip(">").lower() if "@" in value else ""


def aligned(domain: str | None, from_domain: str) -> bool:
    if not domain or not from_domain:
        return False
    domain = domain.strip().lstrip("@").lower()
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    return (
        domain == from_domain
        or domain.endswith("." + from_domain)
        or from_domain.endswith("." + domain)
    )


def _verdict(
    dmarc: str | None,
    dmarc_domain: str | None,
    spf: str | None,
    spf_domain: str | None,
    dkim: str | None,
    dkim_domain: str | None,
    from_domain: str,
) -> bool | None:
    if dmarc == "pass" and (dmarc_domain is None or aligned(dmarc_domain, from_domain)):
        return True
    if spf == "pass" and aligned(spf_domain, from_domain):
        return True
    if dkim == "pass" and aligned(dkim_domain, from_domain):
        return True
    if any(v is not None for v in (dmarc, spf, dkim)):
        return False
    return None


def from_header(message: Message, authserv_id: str | None) -> bool | None:
    """The verdict of the topmost `Authentication-Results` header, if the configured
    receiving server added it; None when it did not or no server is configured."""
    if not authserv_id:
        return None
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return None
    text = _COMMENT.sub(" ", " ".join(str(headers[0]).split()))
    parts = [p.strip() for p in text.split(";")]
    server = parts[0].split()[0].lower() if parts and parts[0].split() else ""
    if server != authserv_id.lower():
        return None
    found: dict[str, tuple[str, dict[str, str]]] = {}
    for part in parts[1:]:
        match = _TOKEN.match(part)
        if match is None:
            continue
        method, result = match.group(1).lower(), match.group(2).lower()
        props = {k.lower(): v.strip('"') for k, v in _PROPERTY.findall(part[match.end() :])}
        # The first result per method counts; a server writes one.
        found.setdefault(method, (result, props))
    if not found:
        return None
    from_domain = domain_of(str(message.get("From", "")))

    def outcome(method: str) -> str | None:
        return found[method][0] if method in found else None

    def prop(method: str, *names: str) -> str | None:
        props = found.get(method, ("", {}))[1]
        for name in names:
            if props.get(name):
                return domain_of(props[name]) if "@" in props[name] else props[name]
        return None

    return _verdict(
        outcome("dmarc"),
        prop("dmarc", "header.from"),
        outcome("spf"),
        prop("spf", "smtp.mailfrom", "smtp.helo"),
        outcome("dkim"),
        prop("dkim", "header.d", "header.i"),
        from_domain,
    )


def from_agentmail(item: Mapping[str, Any]) -> bool | None:
    """AgentMail's verdict for a received message (see the module docstring)."""
    labels = set(item.get("labels") or [])
    if "unauthenticated" in labels:
        return False
    results = item.get("authentication_results")
    if not isinstance(results, Mapping) or not results:
        return None
    return _verdict(
        results.get("dmarc"),
        results.get("dmarc_domain"),
        results.get("spf"),
        results.get("spf_domain"),
        results.get("dkim"),
        results.get("dkim_domain"),
        domain_of(str(item.get("from") or "")),
    )
