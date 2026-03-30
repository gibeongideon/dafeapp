from __future__ import annotations

import re
import secrets
from types import SimpleNamespace

from django.conf import settings

from dns.models import normalize_domain_name
from dns.services.cloudflare import CloudflareDnsProvider


PLATFORM_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{4,61}[a-z0-9])?$")
_PLATFORM_SYLLABLES = (
    "da", "fe", "lo", "ra", "mi", "ko", "zen", "tri", "vel", "nor",
    "qua", "sol", "tal", "ver", "nex", "luna", "cora", "vexa", "mero", "sira",
)


def platform_base_domain() -> str:
    return normalize_domain_name(getattr(settings, "PLATFORM_BASE_DOMAIN", ""))


def platform_domains_enabled() -> bool:
    return bool(platform_base_domain())


def platform_domain_for_label(label: str) -> str:
    base = platform_base_domain()
    normalized = normalize_platform_domain_label(label)
    if not base or not normalized:
        return ""
    return f"{normalized}.{base}"


def normalize_platform_domain_label(label: str) -> str:
    candidate = normalize_domain_name(label)
    candidate = re.sub(r"[^a-z0-9-]", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    return candidate[:63]


def is_platform_domain_label_valid(label: str) -> bool:
    normalized = normalize_platform_domain_label(label)
    return bool(normalized and PLATFORM_LABEL_RE.match(normalized))


def build_platform_domain_label(_instance_name: str = "", attempt: int = 0) -> str:
    suffix = "".join(secrets.choice("0123456789") for _ in range(4))
    body = "".join(secrets.choice(_PLATFORM_SYLLABLES) for _ in range(3))
    label = f"{body}{suffix}"
    if attempt > 0:
        extra = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(2))
        label = f"{body}{extra}{suffix}"
    return normalize_platform_domain_label(label)


def platform_dns_is_configured() -> bool:
    return bool(
        getattr(settings, "PLATFORM_DNS_PROVIDER", "").strip()
        and getattr(settings, "PLATFORM_DNS_API_TOKEN", "").strip()
        and getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip()
        and platform_base_domain()
    )


def platform_dns_default_proxied() -> bool:
    return bool(getattr(settings, "PLATFORM_DNS_PROXIED", False))


def platform_dns_provider_service():
    provider = getattr(settings, "PLATFORM_DNS_PROVIDER", "").strip().upper()
    if provider != "CLOUDFLARE":
        raise RuntimeError("PLATFORM_DNS_PROVIDER must be set to CLOUDFLARE for platform-managed DNS.")

    account = SimpleNamespace(
        api_token=getattr(settings, "PLATFORM_DNS_API_TOKEN", "").strip(),
    )
    return CloudflareDnsProvider(account)
