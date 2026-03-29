from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from dns.models import DomainAssignment, DnsProviderAccount, DnsRecord, DnsZone
from organizations.models import Organization, OrganizationMembership

User = get_user_model()


class DnsModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="dns@test.com", password="pass")
        cls.org = Organization.objects.create(name="DNS Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.account = DnsProviderAccount.objects.create(
            organization=cls.org,
            name="Cloudflare Main",
            provider=DnsProviderAccount.Provider.CLOUDFLARE,
            created_by=cls.user,
        )
        cls.zone = DnsZone.objects.create(
            organization=cls.org,
            provider_account=cls.account,
            name="example.com",
            provider_zone_id="zone-123",
        )

    def test_active_record_uniqueness_allows_deleted_reuse(self):
        DnsRecord.objects.create(
            organization=self.org,
            zone=self.zone,
            record_type=DnsRecord.RecordType.A,
            hostname="app",
            value="203.0.113.10",
            status=DnsRecord.Status.ACTIVE,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DnsRecord.objects.create(
                    organization=self.org,
                    zone=self.zone,
                    record_type=DnsRecord.RecordType.A,
                    hostname="app",
                    value="203.0.113.20",
                    status=DnsRecord.Status.ACTIVE,
                )

        DnsRecord.objects.filter(zone=self.zone, hostname="app").update(status=DnsRecord.Status.DELETED)
        reused = DnsRecord.objects.create(
            organization=self.org,
            zone=self.zone,
            record_type=DnsRecord.RecordType.A,
            hostname="app",
            value="203.0.113.30",
            status=DnsRecord.Status.PENDING,
        )
        self.assertEqual(reused.fqdn, "app.example.com")

    def test_active_assignment_uniqueness_allows_deleted_reuse(self):
        first = DomainAssignment.objects.create(
            organization=self.org,
            domain="app.example.com",
            hostname="app",
            status=DomainAssignment.Status.ACTIVE,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DomainAssignment.objects.create(
                    organization=self.org,
                    domain="app.example.com",
                    hostname="app",
                    status=DomainAssignment.Status.PENDING,
                )

        first.status = DomainAssignment.Status.DELETED
        first.save(update_fields=["status", "updated_at"])
        second = DomainAssignment.objects.create(
            organization=self.org,
            domain="app.example.com",
            hostname="app",
            status=DomainAssignment.Status.PENDING,
        )
        self.assertNotEqual(first.id, second.id)


class DnsApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="dns-api@test.com", password="pass")
        cls.org = Organization.objects.create(name="DNS API Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    def test_create_provider_account_encrypts_token(self):
        response = self.client.post(
            reverse("dns:provider-account-list"),
            data={
                "name": "Cloudflare Prod",
                "provider": DnsProviderAccount.Provider.CLOUDFLARE,
                "api_token": "token-123",
            },
        )
        self.assertEqual(response.status_code, 201)
        account = DnsProviderAccount.objects.get(name="Cloudflare Prod")
        self.assertTrue(account.token_configured)
        self.assertNotEqual(account.encrypted_api_token, "token-123")

    @patch("dns.views.get_dns_provider_service")
    def test_verify_provider_account_updates_status(self, mock_factory):
        account = DnsProviderAccount.objects.create(
            organization=self.org,
            name="Verify Me",
            provider=DnsProviderAccount.Provider.CLOUDFLARE,
            created_by=self.user,
        )
        account._raw_api_token = "secret"
        account.save()

        provider = Mock()
        provider.validate_credentials.return_value = True
        mock_factory.return_value = provider

        response = self.client.post(reverse("dns:provider-account-verify", kwargs={"account_id": account.id}))
        self.assertEqual(response.status_code, 200)
        account.refresh_from_db()
        self.assertTrue(account.is_verified)
        provider.validate_credentials.assert_called_once()

    @patch("dns.views.get_dns_provider_service")
    def test_sync_zones_creates_provider_zones(self, mock_factory):
        account = DnsProviderAccount.objects.create(
            organization=self.org,
            name="Zone Sync",
            provider=DnsProviderAccount.Provider.CLOUDFLARE,
            created_by=self.user,
        )
        provider = Mock()
        provider.list_zones.return_value = [
            {"id": "zone-1", "name": "example.com"},
            {"id": "zone-2", "name": "apps.example.com"},
        ]
        mock_factory.return_value = provider

        response = self.client.post(reverse("dns:provider-account-sync-zones", kwargs={"account_id": account.id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DnsZone.objects.filter(organization=self.org).count(), 2)
