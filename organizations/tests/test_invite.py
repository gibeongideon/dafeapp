from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from organizations.models import Organization, OrganizationInvite, OrganizationMembership

User = get_user_model()


class InviteFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="owner@test.com", password="pass")
        cls.org = Organization.objects.create(name="Invite Org", owner=cls.owner)
        OrganizationMembership.objects.create(user=cls.owner, organization=cls.org, role="SUPER_ADMIN")

    def _make_invite(self, email="new@test.com", role="USER", days=7, used=False):
        return OrganizationInvite.objects.create(
            email=email,
            organization=self.org,
            role=role,
            created_by=self.owner,
            is_used=used,
            expires_at=timezone.now() + timedelta(days=days),
        )

    def test_valid_invite_is_valid(self):
        invite = self._make_invite()
        self.assertTrue(invite.is_valid)
        self.assertFalse(invite.is_expired)

    def test_expired_invite_is_invalid(self):
        invite = self._make_invite(days=-1)
        self.assertFalse(invite.is_valid)
        self.assertTrue(invite.is_expired)

    def test_used_invite_is_invalid(self):
        invite = self._make_invite(used=True)
        self.assertFalse(invite.is_valid)

    def test_accept_creates_membership(self):
        invite = self._make_invite(email="joiner@test.com", role="ADMIN")
        new_user = User.objects.create_user(email="joiner@test.com", password="pass")
        membership = invite.accept(new_user)

        self.assertEqual(membership.user, new_user)
        self.assertEqual(membership.organization, self.org)
        self.assertEqual(membership.role, "ADMIN")
        self.assertTrue(membership.is_active)

    def test_accept_marks_invite_used(self):
        invite = self._make_invite(email="once@test.com")
        new_user = User.objects.create_user(email="once@test.com", password="pass")
        invite.accept(new_user)
        invite.refresh_from_db()
        self.assertTrue(invite.is_used)

    def test_accept_reactivates_existing_membership(self):
        """If member was disabled, accepting invite re-activates them."""
        existing = User.objects.create_user(email="returning@test.com", password="pass")
        OrganizationMembership.objects.create(
            user=existing, organization=self.org, role="USER", is_active=False
        )
        invite = self._make_invite(email="returning@test.com", role="MANAGER")
        invite.accept(existing)

        membership = OrganizationMembership.objects.get(user=existing, organization=self.org)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.role, "MANAGER")

    def test_org_slug_auto_generated(self):
        org = Organization.objects.create(name="My Awesome Corp", owner=self.owner)
        self.assertEqual(org.slug, "my-awesome-corp")

    def test_org_slug_unique_on_collision(self):
        Organization.objects.create(name="Collision Org", owner=self.owner)
        org2 = Organization.objects.create(name="Collision Org", owner=self.owner)
        self.assertNotEqual(org2.slug, "collision-org")
        self.assertTrue(org2.slug.startswith("collision-org-"))
