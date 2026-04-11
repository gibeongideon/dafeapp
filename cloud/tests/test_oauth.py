from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from cloud.models import CloudAccount
from organizations.models import Organization, OrganizationMembership

User = get_user_model()
KEY = "HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g="


@override_settings(
    FIELD_ENCRYPTION_KEY=KEY,
    DIGITALOCEAN_CLIENT_ID="do-client-id",
    DIGITALOCEAN_CLIENT_SECRET="do-client-secret",
)
class DigitalOceanOAuthViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="oauth@test.com", password="pass")
        cls.org = Organization.objects.create(name="OAuth Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
            is_active=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    def test_oauth_start_redirects_to_digitalocean_authorize(self):
        response = self.client.get(reverse("cloud:digitalocean-oauth-start"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("cloud.digitalocean.com/v1/oauth/authorize", response["Location"])
        self.assertIn("client_id=do-client-id", response["Location"])
        self.assertIn("response_type=code", response["Location"])
        self.assertIn("scope=read+write", response["Location"])
        self.assertTrue(self.client.session.get("do_oauth_state"))

    @patch("cloud.views._dispatch")
    @patch("cloud.views.requests.post")
    def test_oauth_callback_creates_oauth_account(self, mock_post, mock_dispatch):
        session = self.client.session
        session["do_oauth_state"] = "state-123"
        session.save()

        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "expires_in": 3600,
        }

        response = self.client.get(
            reverse("cloud:digitalocean-oauth-callback"),
            {"code": "auth-code-123", "state": "state-123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("cloud:dashboard"))

        account = CloudAccount.objects.get()
        self.assertEqual(account.organization, self.org)
        self.assertEqual(account.provider, CloudAccount.Provider.DIGITALOCEAN)
        self.assertEqual(account.do_auth_method, CloudAccount.DOAuthMethod.OAUTH)
        self.assertEqual(account.do_oauth_token, "oauth-access-token")
        self.assertEqual(account.do_oauth_refresh_token, "oauth-refresh-token")
        self.assertEqual(account.api_token, "oauth-access-token")
        mock_dispatch.assert_called_once()

    def test_oauth_callback_rejects_invalid_state(self):
        session = self.client.session
        session["do_oauth_state"] = "expected-state"
        session.save()

        response = self.client.get(
            reverse("cloud:digitalocean-oauth-callback"),
            {"code": "auth-code-123", "state": "wrong-state"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("cloud:add-account"))
        self.assertFalse(CloudAccount.objects.exists())
