"""
Provider factory — returns the correct AbstractCloudProvider for a CloudAccount.
"""

from cloud.base import AbstractCloudProvider


def get_provider(cloud_account) -> AbstractCloudProvider:
    """
    Return a configured provider instance for *cloud_account*.
    Raises ValueError if the provider is unknown.
    """
    from cloud.digitalocean import DigitalOceanProvider

    provider_map = {
        "DIGITALOCEAN": DigitalOceanProvider,
    }
    cls = provider_map.get(cloud_account.provider)
    if cls is None:
        raise ValueError(f"Unknown provider: {cloud_account.provider!r}")
    return cls(cloud_account)
