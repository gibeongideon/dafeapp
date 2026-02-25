"""
Abstract base class for all cloud providers.
"""

from abc import ABC, abstractmethod


class AbstractCloudProvider(ABC):
    """All cloud provider implementations must subclass this."""

    @abstractmethod
    def validate_credentials(self) -> tuple[bool, str]:
        """
        Verify that the stored API credentials are valid.
        Returns (success: bool, message: str).
        """

    @abstractmethod
    def create_server(self, name: str, region: str, size: str) -> dict:
        """
        Provision a new server.
        Returns a provider-specific dict containing at minimum 'id'.
        """

    @abstractmethod
    def destroy_server(self, provider_server_id: str) -> bool:
        """
        Terminate and delete a server.
        Returns True on success.
        """

    @abstractmethod
    def create_firewall(self, provider_server_id: str) -> dict:
        """
        Apply a firewall rule to a server allowing ports 22, 80, 443.
        Returns a provider-specific response dict.
        """

    @abstractmethod
    def get_server_status(self, provider_server_id: str) -> str:
        """
        Return the current status string for a server
        (e.g. 'new', 'active', 'off', 'archive').
        """

    @abstractmethod
    def get_server_ip(self, provider_server_id: str) -> str:
        """Return the server public IPv4 address (empty string if unavailable)."""

    def list_regions(self) -> list[tuple[str, str]]:
        """Return provider regions as (value, label) tuples."""
        return []

    def list_sizes(self, region: str = "") -> list[tuple[str, str]]:
        """Return provider instance sizes as (value, label) tuples."""
        return []
