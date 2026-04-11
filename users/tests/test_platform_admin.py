from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from core.admin_site import PlatformAdminSite

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
