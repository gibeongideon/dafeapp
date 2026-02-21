from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from organizations.middleware import OrganizationMiddleware
from organizations.models import Organization, OrganizationMembership

User = get_user_model()


class OrgScopeTests(TestCase):
    """Ensure cross-org data isolation works correctly."""

    @classmethod
    def setUpTestData(cls):
        cls.user_a = User.objects.create_user(email="a@test.com", password="pass")
        cls.user_b = User.objects.create_user(email="b@test.com", password="pass")

        cls.org_a = Organization.objects.create(name="Org A", owner=cls.user_a)
        cls.org_b = Organization.objects.create(name="Org B", owner=cls.user_b)

        OrganizationMembership.objects.create(user=cls.user_a, organization=cls.org_a, role="SUPER_ADMIN")
        OrganizationMembership.objects.create(user=cls.user_b, organization=cls.org_b, role="SUPER_ADMIN")

    def test_user_cannot_see_other_org_members(self):
        """Filtering by org must exclude members from other orgs."""
        members_a = OrganizationMembership.objects.filter(organization=self.org_a)
        self.assertNotIn(
            self.user_b,
            [m.user for m in members_a],
            "user_b should NOT appear in org_a's member list",
        )

    def test_org_a_members_only_from_org_a(self):
        members = OrganizationMembership.objects.filter(organization=self.org_a)
        for m in members:
            self.assertEqual(m.organization, self.org_a)

    def test_middleware_sets_current_org(self):
        """Middleware attaches request.organization from session."""
        factory = RequestFactory()
        request = factory.get("/dashboard/")
        request.user = self.user_a
        request.session = {"current_org_id": self.org_a.id}

        middleware = OrganizationMiddleware(get_response=lambda r: r)
        middleware(request)

        self.assertEqual(request.organization, self.org_a)
        self.assertEqual(request.org_role, "SUPER_ADMIN")

    def test_middleware_clears_stale_session(self):
        """Stale org_id in session is cleared and replaced with a valid fallback."""
        factory = RequestFactory()
        request = factory.get("/dashboard/")
        request.user = self.user_a
        request.session = {"current_org_id": 99999}  # non-existent

        middleware = OrganizationMiddleware(get_response=lambda r: r)
        middleware(request)

        # Middleware falls back to the first real membership and sets a valid key
        self.assertIsNotNone(request.organization)
        self.assertEqual(request.organization, self.org_a)
        self.assertNotEqual(request.session.get("current_org_id"), 99999)
        self.assertEqual(request.session.get("current_org_id"), self.org_a.id)

    def test_multi_org_user_sees_only_own_orgs(self):
        """A user in two orgs only gets back their own memberships."""
        shared_user = User.objects.create_user(email="shared@test.com", password="pass")
        OrganizationMembership.objects.create(user=shared_user, organization=self.org_a, role="USER")
        OrganizationMembership.objects.create(user=shared_user, organization=self.org_b, role="ADMIN")

        my_orgs = OrganizationMembership.objects.filter(user=shared_user, is_active=True)
        org_ids = set(m.organization_id for m in my_orgs)
        self.assertIn(self.org_a.id, org_ids)
        self.assertIn(self.org_b.id, org_ids)
        self.assertEqual(my_orgs.count(), 2)
