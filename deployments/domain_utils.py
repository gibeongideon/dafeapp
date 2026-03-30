from __future__ import annotations

from types import SimpleNamespace

from django.conf import settings
from django.utils.text import slugify

from dns.models import normalize_domain_name
from dns.services.cloudflare import CloudflareDnsProvider


def platform_base_domain() -> str:
    return normalize_domain_name(getattr(settings, "PLATFORM_BASE_DOMAIN", ""))


def platform_domains_enabled() -> bool:
    return bool(platform_base_domain())


def platform_domain_for_label(label: str) -> str:
    base = platform_base_domain()
    normalized = normalize_domain_name(label)
    if not base or not normalized:
        return ""
    return f"{normalized}.{base}"


def build_platform_domain_label(instance_name: str, attempt: int = 0) -> str:
    root = slugify(instance_name or "", allow_unicode=False).strip("-") or "app"
    if attempt <= 0:
        return root[:48]
    suffix = f"-{attempt + 1}"
    return f"{root[: max(1, 48 - len(suffix))]}{suffix}"


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
