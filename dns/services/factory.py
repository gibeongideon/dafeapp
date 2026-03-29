from dns.models import DnsProviderAccount
from dns.services.cloudflare import CloudflareDnsProvider


def get_dns_provider_service(account: DnsProviderAccount):
    if account.provider == DnsProviderAccount.Provider.CLOUDFLARE:
        return CloudflareDnsProvider(account)
    raise ValueError(f"Unsupported DNS provider: {account.provider}")
