"""Host decisions are immutable snapshots, independent of registry/model content."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from .canonical import canonical_json, sha256_hex
from .errors import PolicyDenied
from .models import Integration, ToolRule

_AUTHORITY = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#]")
_NUMERIC_LABEL = re.compile(r"(?i)0x[0-9a-f]*|[0-9]+")


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP literal, including legacy IPv4 forms (127.1, 0x7f.0.0.1, 2130706433) resolvers accept."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def fingerprint(integration: Integration) -> str:
    """SHA-256 of the RFC 8785 canonical manifest; identical in the TypeScript SDK."""
    return sha256_hex(canonical_json(integration.model_dump(mode="json")))


class Policy:
    def __init__(
        self,
        approved: Iterable[Integration] = (),
        *,
        capabilities: Iterable[str] = (),
        effects: Iterable[str] = ("read", "draft"),
        accounts: Iterable[str] = ("default",),
        allow_loopback: bool = False,
        allow_local: bool = True,
    ) -> None:
        self._approved = {i.id: fingerprint(i) for i in approved}
        self.capabilities = frozenset(capabilities)
        self.effects = frozenset(effects)
        self.accounts = frozenset(accounts)
        self.allow_loopback = allow_loopback
        self.allow_local = allow_local

    def check(self, integration: Integration, capability: str | None = None, account: str = "default") -> None:
        if self._approved.get(integration.id) != fingerprint(integration):
            raise PolicyDenied("Integration/version/configuration is not approved by the host")
        if integration.support not in {"supported", "experimental"}:
            raise PolicyDenied("Integration has no supported adapter")
        if account not in self.accounts:
            raise PolicyDenied("Account is outside the host policy")
        if capability is not None and (
            capability not in self.capabilities or capability not in integration.capabilities
        ):
            raise PolicyDenied("Capability is outside the host policy")
        if integration.transport == "stdio":
            if not self.allow_local:
                raise PolicyDenied("Local execution is disabled")
            if not (integration.command or integration.package):
                raise PolicyDenied("Local integration needs a configured runtime")
        else:
            self.check_endpoint(integration.endpoint or "")

    def check_endpoint(self, endpoint: str) -> None:
        if not _AUTHORITY.match(endpoint) or any(c == "\\" or c.isspace() or ord(c) < 0x20 for c in endpoint):
            raise PolicyDenied("Endpoint must be an absolute URL without backslashes or whitespace")
        url = urlsplit(endpoint)
        if not url.hostname or url.username or url.password or url.fragment or url.query:
            raise PolicyDenied("Endpoint must not contain credentials, query parameters or fragments")
        host = url.hostname.rstrip(".")
        address = _address(host)
        if address is None and _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
            raise PolicyDenied("Endpoint host is neither a valid address nor a DNS name")
        loopback = host == "localhost" or host.endswith(".localhost") or (address is not None and address.is_loopback)
        if loopback and self.allow_loopback and url.scheme in {"http", "https"}:
            return
        if url.scheme != "https" or loopback or (address is not None and not address.is_global):
            raise PolicyDenied("Remote MCP endpoints require HTTPS and a public address")

    def check_tool(self, integration: Integration, rule: ToolRule, account: str) -> None:
        self.check(integration, rule.capability, account)
        if rule.effect not in self.effects:
            raise PolicyDenied("Tool effect requires a host policy change")
