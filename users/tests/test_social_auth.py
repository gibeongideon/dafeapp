"""
Tests for social authentication and VCS integration.

Test plan:
1.  test_social_signup_google_creates_org       — new Google user gets org auto-created
2.  test_social_signup_github_creates_org       — new GitHub user gets org auto-created
3.  test_social_login_existing_user_connects    — email-matching user is connected, no extra org
4.  test_vcs_connect_github_encrypts_token      — VCSAccount created with encrypted token via signal
5.  test_vcs_connect_gitlab_encrypts_token      — Same for GitLab
6.  test_vcs_token_not_exposed_in_profile_api   — encrypted_access_token not in /api/users/me/ response
7.  test_vcs_disconnect_deactivates_account     — POST disconnect marks is_active=False
8.  test_vcs_disconnect_removes_social_account  — allauth SocialAccount deleted on disconnect
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from audit.models import AuditLog
from cloud.encryption import FieldEncryptor
from organizations.models import Organization, OrganizationMembership
from users.adapters import SocialAccountAdapter
from users.models import VCSAccount

User = get_user_model()


def _make_sociallogin(provider, email, full_name="Test User", extra_data=None):
    """Build a minimal mock sociallogin object for adapter tests."""
    user = MagicMock()
    user.email = email
    user.get_full_name.return_value = full_name
    user.memberships.filter.return_value.exists.return_value = False

    account = MagicMock()
    account.provider = provider
    account.extra_data = extra_data or {}

    sl = MagicMock()
    sl.user = user
    sl.account = account
    sl.is_existing = False
    return sl


class SocialSignupAdapterTests(TestCase):
    """
    Test SocialAccountAdapter.save_user creates the expected org and membership
    for a brand-new social user.
    """

    def _run_save_user(self, provider, email, extra_data=None):
        """
        Call SocialAccountAdapter.save_user with a mocked sociallogin.
        Returns the real User, Organization, and Membership created by the adapter.
        """
        adapter = SocialAccountAdapter()
        request = RequestFactory().get("/")
        request.session = {}

        # Create a minimal real User so super().save_user has something to return
        db_user = User.objects.create_user(
            email=email,
            password=None,
            first_name="Test",
            last_name="User",
        )

        sociallogin = _make_sociallogin(provider, email, extra_data=extra_data)
        # Ensure memberships queryset returns an empty queryset (no existing orgs)
        db_user._vcs_memberships_checked = False

        with patch.object(adapter.__class__.__bases__[0], "save_user", return_value=db_user):
            result = adapter.save_user(request, sociallogin)

        return result

    def test_social_signup_google_creates_org(self):
        """New Google user gets an Organization + SUPER_ADMIN membership."""
        user = self._run_save_user("google", "google@example.com")

        self.assertEqual(user.auth_provider, "google")
        self.assertTrue(user.is_email_verified)

        org = Organization.objects.filter(owner=user).first()
        self.assertIsNotNone(org)

        membership = OrganizationMembership.objects.filter(user=user, organization=org).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.role, OrganizationMembership.Role.SUPER_ADMIN)

    def test_social_signup_github_creates_org(self):
        """New GitHub user gets an Organization + SUPER_ADMIN membership."""
        user = self._run_save_user("github", "github@example.com")

        self.assertEqual(user.auth_provider, "github")

        org = Organization.objects.filter(owner=user).first()
        self.assertIsNotNone(org)

        membership = OrganizationMembership.objects.filter(user=user, organization=org).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.role, OrganizationMembership.Role.SUPER_ADMIN)

    def test_social_signup_sets_auth_provider(self):
        """auth_provider field is set to the OAuth provider name."""
        user = self._run_save_user("gitlab", "gitlab@example.com")
        user.refresh_from_db()
        self.assertEqual(user.auth_provider, "gitlab")

    def test_social_login_existing_user_no_extra_org(self):
        """
        If the user already has an active membership, save_user must NOT create
        a second Organization.
        """
        # Create a real user with a real org + membership in the DB
        existing_user = User.objects.create_user(
            email="existing@example.com", password="testpass123"
        )
        org = Organization.objects.create(name="Existing Org", owner=existing_user)
        OrganizationMembership.objects.create(
            user=existing_user,
            organization=org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )

        adapter = SocialAccountAdapter()
        request = RequestFactory().get("/")
        request.session = {}

        sociallogin = _make_sociallogin("google", "existing@example.com")

        # Patch only super().save_user so we skip allauth internals;
        # our adapter code checks the real DB for existing memberships.
        with patch.object(
            adapter.__class__.__bases__[0], "save_user", return_value=existing_user
        ):
            adapter.save_user(request, sociallogin)

        # Only 1 org should exist — no duplicate was created
        self.assertEqual(Organization.objects.filter(owner=existing_user).count(), 1)


class SocialAuthSettingsTests(SimpleTestCase):
    def test_github_connect_does_not_require_email_query(self):
        self.assertFalse(settings.SOCIALACCOUNT_QUERY_EMAIL)
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS["github"]["SCOPE"],
            ["read:user", "repo"],
        )


class VCSAccountTests(TestCase):
    """Tests for VCSAccount model and the signal-driven token sync."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="vcsuser@example.com", password="pass12345"
        )
        self.org = Organization.objects.create(name="VCS Org", owner=self.user)
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )

    def _make_sociallogin_with_token(self, provider, token, username="testuser"):
        """Build a mock sociallogin with a SocialToken."""
        token_obj = MagicMock()
        token_obj.token = token

        account = MagicMock()
        account.provider = provider
        account.extra_data = {"login": username} if provider == "github" else {"username": username}

        sl = MagicMock()
        sl.user = self.user
        sl.account = account
        sl.token = token_obj
        return sl

    def test_vcs_connect_github_encrypts_token(self):
        """social_account_added signal creates VCSAccount with encrypted token."""
        from users.signals import _sync_vcs_account

        raw_token = "ghp_TestToken12345"
        sociallogin = self._make_sociallogin_with_token("github", raw_token, username="octocat")

        _sync_vcs_account(sociallogin)

        vcs = VCSAccount.objects.get(user=self.user, provider="github")
        # Stored value must NOT be the raw token
        self.assertNotEqual(vcs.encrypted_access_token, raw_token)
        # But decrypting must return the original
        self.assertEqual(FieldEncryptor.decrypt(vcs.encrypted_access_token), raw_token)
        self.assertEqual(vcs.username, "octocat")
        self.assertTrue(vcs.is_active)

    def test_vcs_connect_gitlab_encrypts_token(self):
        """Works for GitLab provider too."""
        from users.signals import _sync_vcs_account

        raw_token = "glpat-GitLabToken98765"
        sociallogin = self._make_sociallogin_with_token("gitlab", raw_token, username="gitlabber")

        _sync_vcs_account(sociallogin)

        vcs = VCSAccount.objects.get(user=self.user, provider="gitlab")
        self.assertNotEqual(vcs.encrypted_access_token, raw_token)
        self.assertEqual(FieldEncryptor.decrypt(vcs.encrypted_access_token), raw_token)

    def test_vcs_connect_logs_audit(self):
        """VCS_CONNECT audit log entry is created when signal fires."""
        from users.signals import _sync_vcs_account

        sociallogin = self._make_sociallogin_with_token("github", "ghp_AuditToken", "auditor")
        _sync_vcs_account(sociallogin)

        log = AuditLog.objects.filter(
            user=self.user, action=AuditLog.Action.VCS_CONNECT
        ).first()
        self.assertIsNotNone(log)
        self.assertIn("github", log.description)

    def test_vcs_access_token_property(self):
        """VCSAccount.access_token property decrypts transparently."""
        raw = "secret_token_123"
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="testuser",
            encrypted_access_token=FieldEncryptor.encrypt(raw),
        )
        self.assertEqual(vcs.access_token, raw)

    def test_vcs_disconnect_view_deactivates(self):
        """POST to vcs-disconnect sets is_active=False."""
        raw = "token_to_revoke"
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="revoker",
            encrypted_access_token=FieldEncryptor.encrypt(raw),
            is_active=True,
        )

        client = Client()
        client.force_login(self.user)

        url = reverse("users:vcs-disconnect", args=[vcs.pk])
        response = client.post(url)

        # Should redirect to VCS dashboard
        self.assertEqual(response.status_code, 302)

        vcs.refresh_from_db()
        self.assertFalse(vcs.is_active)

    def test_vcs_disconnect_logs_audit(self):
        """Disconnect writes a VCS_DISCONNECT audit log entry."""
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="auditme",
            encrypted_access_token=FieldEncryptor.encrypt("tok"),
            is_active=True,
        )

        client = Client()
        client.force_login(self.user)
        client.post(reverse("users:vcs-disconnect", args=[vcs.pk]))

        log = AuditLog.objects.filter(
            user=self.user, action=AuditLog.Action.VCS_DISCONNECT
        ).first()
        self.assertIsNotNone(log)

    def test_vcs_token_not_in_profile_api(self):
        """encrypted_access_token must NOT appear in /api/users/me/ response."""
        VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="secretuser",
            encrypted_access_token=FieldEncryptor.encrypt("supersecret"),
        )

        from rest_framework.test import APIClient

        # Reload user from DB to avoid any in-memory F() expression on login_count
        fresh_user = User.objects.get(pk=self.user.pk)

        api = APIClient()
        api.force_authenticate(user=fresh_user)
        resp = api.get("/api/users/me/")

        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertNotIn("supersecret", content)
        self.assertNotIn("encrypted_access_token", content)
