"""
DigitalOcean provider tests — 5 cases, requests.Session is fully mocked.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from cloud.encryption import FieldEncryptor
from cloud.models import CloudAccount
from organizations.models import Organization

User = get_user_model()

KEY = "HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g="


def _make_account(org):
    account = CloudAccount(
        organization=org,
        provider=CloudAccount.Provider.DIGITALOCEAN,
        name="Test DO Account",
    )
    account.encrypted_api_token = FieldEncryptor.encrypt("dop_v1_test_token")
    account.pk = 1
    return account


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class DigitalOceanProviderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="do@test.com", password="pass")
        cls.org = Organization.objects.create(name="DO Org", owner=cls.user)

    def _provider(self):
        from cloud.digitalocean import DigitalOceanProvider
        account = _make_account(self.org)
        return DigitalOceanProvider(account)

    @patch("requests.Session.get")
    def test_validate_credentials_success(self, mock_get):
        """GET /v2/account → 200 → validate_credentials returns (True, …)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        provider = self._provider()
        success, msg = provider.validate_credentials()

        self.assertTrue(success)
        self.assertIn("valid", msg.lower())

    @patch("requests.Session.get")
    def test_validate_credentials_bad_token(self, mock_get):
        """GET /v2/account → 401 → validate_credentials returns (False, …)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        provider = self._provider()
        success, msg = provider.validate_credentials()

        self.assertFalse(success)
        self.assertIn("401", msg)

    @patch("requests.Session.post")
    def test_create_server_returns_dict_with_id(self, mock_post):
        """POST /v2/droplets → returns dict containing 'id'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {"droplet": {"id": 12345, "name": "test-droplet"}}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        provider = self._provider()
        droplet = provider.create_server("test-droplet", "nyc3", "s-1vcpu-1gb")

        self.assertIn("id", droplet)
        self.assertEqual(droplet["id"], 12345)

    @patch("requests.Session.get")
    def test_get_server_status_returns_string(self, mock_get):
        """GET /v2/droplets/{id} → returns status string."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"droplet": {"id": 12345, "status": "active"}}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        provider = self._provider()
        status = provider.get_server_status("12345")

        self.assertEqual(status, "active")
        self.assertIsInstance(status, str)

    @patch("requests.Session.delete")
    def test_destroy_server_returns_true_on_204(self, mock_delete):
        """DELETE /v2/droplets/{id} → 204 → True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_delete.return_value = mock_resp

        provider = self._provider()
        result = provider.destroy_server("12345")

        self.assertTrue(result)
