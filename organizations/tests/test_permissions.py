from django.contrib.auth import get_user_model
from django.test import TestCase

from organizations.models import Organization, OrganizationMembership
from organizations.permissions import PERMISSION_MATRIX, has_org_permission

User = get_user_model()


class RBACPermissionTests(TestCase):
    """Test the full permission matrix for each role."""

    @classmethod
    def setUpTestData(cls):
        cls.super_admin = User.objects.create_user(email="super@test.com", password="pass123")
        cls.admin = User.objects.create_user(email="admin@test.com", password="pass123")
        cls.manager = User.objects.create_user(email="manager@test.com", password="pass123")
        cls.user = User.objects.create_user(email="user@test.com", password="pass123")

        cls.org = Organization.objects.create(name="Test Org", owner=cls.super_admin)

        OrganizationMembership.objects.create(user=cls.super_admin, organization=cls.org, role="SUPER_ADMIN")
        OrganizationMembership.objects.create(user=cls.admin, organization=cls.org, role="ADMIN")
        OrganizationMembership.objects.create(user=cls.manager, organization=cls.org, role="MANAGER")
        OrganizationMembership.objects.create(user=cls.user, organization=cls.org, role="USER")

    # ── SUPER_ADMIN ─────────────────────────────────────────────────────────

    def test_super_admin_has_all_permissions(self):
        for perm in PERMISSION_MATRIX:
            self.assertTrue(
                has_org_permission(self.super_admin, self.org, perm),
                f"SUPER_ADMIN should have '{perm}'",
            )

    # ── ADMIN ────────────────────────────────────────────────────────────────

    def test_admin_can_create_user(self):
        self.assertTrue(has_org_permission(self.admin, self.org, "create_user"))

    def test_admin_can_invite_user(self):
        self.assertTrue(has_org_permission(self.admin, self.org, "invite_user"))

    def test_admin_cannot_delete_user(self):
        self.assertFalse(has_org_permission(self.admin, self.org, "delete_user"))

    def test_admin_cannot_change_role(self):
        self.assertFalse(has_org_permission(self.admin, self.org, "change_role"))

    def test_admin_cannot_manage_billing(self):
        self.assertFalse(has_org_permission(self.admin, self.org, "manage_billing"))

    # ── MANAGER ─────────────────────────────────────────────────────────────

    def test_manager_can_deploy_odoo(self):
        self.assertTrue(has_org_permission(self.manager, self.org, "deploy_odoo"))

    def test_manager_can_create_instance(self):
        self.assertTrue(has_org_permission(self.manager, self.org, "create_instance"))

    def test_manager_cannot_create_user(self):
        self.assertFalse(has_org_permission(self.manager, self.org, "create_user"))

    def test_manager_cannot_delete_instance(self):
        self.assertFalse(has_org_permission(self.manager, self.org, "delete_instance"))

    # ── USER ─────────────────────────────────────────────────────────────────

    def test_user_can_view_logs(self):
        self.assertTrue(has_org_permission(self.user, self.org, "view_logs"))

    def test_user_cannot_deploy_odoo(self):
        self.assertFalse(has_org_permission(self.user, self.org, "deploy_odoo"))

    def test_user_cannot_create_user(self):
        self.assertFalse(has_org_permission(self.user, self.org, "create_user"))

    def test_user_cannot_manage_billing(self):
        self.assertFalse(has_org_permission(self.user, self.org, "manage_billing"))

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_inactive_membership_has_no_permission(self):
        OrganizationMembership.objects.filter(user=self.user, organization=self.org).update(is_active=False)
        self.assertFalse(has_org_permission(self.user, self.org, "view_logs"))
        # Restore
        OrganizationMembership.objects.filter(user=self.user, organization=self.org).update(is_active=True)

    def test_nonmember_has_no_permission(self):
        outsider = User.objects.create_user(email="outsider@test.com", password="pass123")
        for perm in PERMISSION_MATRIX:
            self.assertFalse(has_org_permission(outsider, self.org, perm))

    def test_user_from_other_org_cannot_view_logs(self):
        other_org = Organization.objects.create(name="Other Org", owner=self.admin)
        self.assertFalse(has_org_permission(self.user, other_org, "view_logs"))

    def test_unknown_permission_returns_false(self):
        self.assertFalse(has_org_permission(self.super_admin, self.org, "fly_to_mars"))
