from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from core.admin_site import PlatformAdminSite
from django.contrib.admin.sites import AdminSite

from subscriptions.admin import SubscriptionAdmin
from subscriptions.models import Subscription

User = get_user_model()


class PlatformAdminPermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_platform_admin_gets_global_django_permissions(self):
        user = User.objects.create_user(
            email="platform@example.com",
            password="testpass123",
            is_platform_admin=True,
        )

        self.assertTrue(user.has_perm("auth.view_group"))
        self.assertTrue(user.has_module_perms("auth"))

    def test_platform_admin_site_allows_platform_admin_without_staff(self):
        user = User.objects.create_user(
            email="ops@example.com",
            password="testpass123",
            is_platform_admin=True,
            is_staff=False,
        )
        request = self.factory.get("/admin/")
        request.user = user

        self.assertTrue(PlatformAdminSite().has_permission(request))

    def test_regular_user_does_not_get_platform_admin_access(self):
        user = User.objects.create_user(
            email="member@example.com",
            password="testpass123",
        )
        request = self.factory.get("/admin/")
        request.user = user

        self.assertFalse(user.has_perm("auth.view_group"))
        self.assertFalse(PlatformAdminSite().has_permission(request))

    def test_limited_platform_role_can_enter_admin(self):
        user = User.objects.create_user(
            email="finance@example.com",
            password="testpass123",
            platform_role=User.PlatformRole.FINANCE,
            is_staff=False,
        )
        request = self.factory.get("/admin/")
        request.user = user

        self.assertTrue(user.has_perm("subscriptions.view_subscription"))
        self.assertTrue(PlatformAdminSite().has_permission(request))

    def test_finance_role_gets_billing_admin_but_not_org_write_access(self):
        user = User.objects.create_user(
            email="billing@example.com",
            password="testpass123",
            platform_role=User.PlatformRole.FINANCE,
        )
        request = self.factory.get("/admin/subscriptions/subscription/")
        request.user = user
        admin_obj = SubscriptionAdmin(Subscription, AdminSite())

        self.assertTrue(admin_obj.has_view_permission(request))
        self.assertTrue(admin_obj.has_change_permission(request))
        self.assertFalse(admin_obj.has_delete_permission(request))
