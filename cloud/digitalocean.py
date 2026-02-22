"""
DigitalOcean provider — uses the DO v2 REST API via `requests`.
"""

import logging

import requests

from cloud.base import AbstractCloudProvider
from cloud.encryption import FieldEncryptor

logger = logging.getLogger(__name__)

API_BASE = "https://api.digitalocean.com/v2"

# Available regions (slug → display name)
DO_REGIONS = [
    ("nyc3", "New York 3"),
    ("sfo3", "San Francisco 3"),
    ("ams3", "Amsterdam 3"),
    ("sgp1", "Singapore 1"),
    ("lon1", "London 1"),
    ("fra1", "Frankfurt 1"),
    ("tor1", "Toronto 1"),
    ("blr1", "Bangalore 1"),
    ("syd1", "Sydney 1"),
]

# Available sizes (slug → display name)
DO_SIZES = [
    ("s-1vcpu-1gb", "1 vCPU / 1 GB RAM ($6/mo)"),
    ("s-1vcpu-2gb", "1 vCPU / 2 GB RAM ($12/mo)"),
    ("s-2vcpu-2gb", "2 vCPU / 2 GB RAM ($18/mo)"),
    ("s-2vcpu-4gb", "2 vCPU / 4 GB RAM ($24/mo)"),
    ("s-4vcpu-8gb", "4 vCPU / 8 GB RAM ($48/mo)"),
]


class DigitalOceanProvider(AbstractCloudProvider):
    """Interact with DigitalOcean via the v2 REST API."""

    def __init__(self, cloud_account):
        self.account = cloud_account
        self._token = FieldEncryptor.decrypt(cloud_account.encrypted_api_token)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # AbstractCloudProvider implementation
    # ------------------------------------------------------------------

    def validate_credentials(self) -> tuple[bool, str]:
        """GET /v2/account → 200 means valid token."""
        try:
            resp = self._session.get(f"{API_BASE}/account", timeout=10)
            if resp.status_code == 200:
                return True, "Credentials valid."
            if resp.status_code == 401:
                return False, "Invalid API token (401 Unauthorized)."
            return False, f"Unexpected response: {resp.status_code}"
        except requests.RequestException as exc:
            return False, f"Network error: {exc}"

    def create_server(self, name: str, region: str, size: str) -> dict:
        """POST /v2/droplets — ubuntu-22-04-x64, returns droplet dict."""
        payload = {
            "name": name,
            "region": region,
            "size": size,
            "image": "ubuntu-22-04-x64",
            "backups": False,
            "ipv6": False,
            "monitoring": True,
        }
        try:
            resp = self._session.post(f"{API_BASE}/droplets", json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json().get("droplet", {})
        except requests.RequestException as exc:
            logger.error("DO create_server failed: %s", exc)
            raise

    def create_firewall(self, provider_server_id: str) -> dict:
        """POST /v2/firewalls — allow 22/80/443 TCP inbound for the droplet."""
        payload = {
            "name": f"dafeapp-fw-{provider_server_id}",
            "inbound_rules": [
                {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
                {"protocol": "tcp", "ports": "80", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
                {"protocol": "tcp", "ports": "443", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
            ],
            "outbound_rules": [
                {"protocol": "tcp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
                {"protocol": "udp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
            ],
            "droplet_ids": [int(provider_server_id)],
        }
        try:
            resp = self._session.post(f"{API_BASE}/firewalls", json=payload, timeout=15)
            resp.raise_for_status()
            return resp.json().get("firewall", {})
        except requests.RequestException as exc:
            logger.error("DO create_firewall failed: %s", exc)
            raise

    def get_server_status(self, provider_server_id: str) -> str:
        """GET /v2/droplets/{id} → droplet.status string."""
        try:
            resp = self._session.get(f"{API_BASE}/droplets/{provider_server_id}", timeout=10)
            resp.raise_for_status()
            return resp.json().get("droplet", {}).get("status", "unknown")
        except requests.RequestException as exc:
            logger.error("DO get_server_status failed: %s", exc)
            return "unknown"

    def destroy_server(self, provider_server_id: str) -> bool:
        """DELETE /v2/droplets/{id} → True on 204."""
        try:
            resp = self._session.delete(f"{API_BASE}/droplets/{provider_server_id}", timeout=15)
            return resp.status_code == 204
        except requests.RequestException as exc:
            logger.error("DO destroy_server failed: %s", exc)
            return False
