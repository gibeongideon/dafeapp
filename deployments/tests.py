from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch

from cloud.models import CloudAccount
from deployments.models import Infrastructure, Instance, OdooInstance, OdooServer, TerraformRun
from organizations.models import Organization, OrganizationMembership
from subscriptions.models import Plan, Subscription

User = get_user_model()


class DeploymentCreateFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="deploy@test.com", password="pass")
        cls.org = Organization.objects.create(name="Deploy Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.plan = Plan.objects.create(
            name="Starter",
            plan_type=Plan.PlanType.STARTER,
            price_monthly="0.00",
            max_instances=3,
            max_backups_per_month=5,
            staging_enabled=False,
            version_upgrade_enabled=False,
            is_active=True,
        )
        Subscription.objects.update_or_create(
            organization=cls.org,
            defaults={
                "plan": cls.plan,
                "status": Subscription.Status.ACTIVE,
                "current_period_start": timezone.now(),
                "current_period_end": timezone.now() + timedelta(days=365),
            },
        )
        cls.account = CloudAccount.objects.create(
            organization=cls.org,
            provider=CloudAccount.Provider.DIGITALOCEAN,
            name="DO Account",
            encrypted_api_token="dummy",
            is_verified=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    @patch("deployments.views.terraform_apply_instance.delay")
    def test_create_instance_queues_terraform_run(self, mock_delay):
        resp = self.client.post(
            reverse("deployments_ui:create-instance"),
            data={
                "name": "phase4-app",
                "cloud_account": self.account.id,
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        instance = Instance.objects.get(name="phase4-app")
        run = TerraformRun.objects.get(instance=instance)
        mock_delay.assert_called_once_with(run.id)

    @patch("deployments.views.get_provider")
    def test_options_api_returns_regions_and_sizes(self, mock_get_provider):
        provider = mock_get_provider.return_value
        provider.list_regions.return_value = [("r1", "Region 1")]
        provider.list_sizes.return_value = [("s1", "Small 1")]
        resp = self.client.get(
            reverse("deployments:account-options", kwargs={"account_id": self.account.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertJSONEqual(
            resp.content,
            {"regions": [["r1", "Region 1"]], "sizes": [["s1", "Small 1"]]},
        )


class OdooVersionedFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="odoo@test.com", password="pass")
        cls.org = Organization.objects.create(name="Odoo Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.plan = Plan.objects.create(
            name="Growth",
            plan_type=Plan.PlanType.GROWTH,
            price_monthly="49.00",
            max_instances=10,
            max_backups_per_month=30,
            staging_enabled=True,
            version_upgrade_enabled=True,
            is_active=True,
        )
        Subscription.objects.update_or_create(
            organization=cls.org,
            defaults={
                "plan": cls.plan,
                "status": Subscription.Status.ACTIVE,
                "current_period_start": timezone.now(),
                "current_period_end": timezone.now() + timedelta(days=365),
            },
        )
        cls.account = CloudAccount.objects.create(
            organization=cls.org,
            provider=CloudAccount.Provider.DIGITALOCEAN,
            name="DO Account",
            encrypted_api_token="dummy",
            is_verified=True,
        )
        cls.infrastructure = Infrastructure.objects.create(
            organization=cls.org,
            name="Managed DO Infra",
            infra_type=Infrastructure.InfraType.MANAGED,
            cloud_account=cls.account,
            is_connected=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    @patch("deployments.views._dispatch")
    def test_create_odoo_server_v19(self, mock_dispatch):
        resp = self.client.post(
            reverse("deployments:odoo-server-create"),
            data={
                "name": "odoo19-prod",
                "infrastructure_id": self.infrastructure.id,
                "odoo_version": "19",
                "region": "nyc3",
                "size": "s-2vcpu-4gb",
                "dns_domain": "odoo19.example.com",
                "deployment_mode": "DOCKER",
            },
        )
        self.assertEqual(resp.status_code, 201)
        server = OdooServer.objects.get(name="odoo19-prod")
        self.assertEqual(server.odoo_version, "19")
        self.assertEqual(server.deployment_mode, OdooServer.DeploymentMode.DOCKER)
        mock_dispatch.assert_called_once()

    @patch("deployments.views._dispatch")
    def test_create_odoo_instance_on_ready_server(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo18-prod",
            odoo_version="18",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.10",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        resp = self.client.post(
            reverse("deployments:odoo-instance-create"),
            data={
                "server_id": server.id,
                "name": "sales",
                "db_name": "sales_db",
                "domain": "sales.example.com",
                "http_port": 8071,
            },
        )
        self.assertEqual(resp.status_code, 201)
        obj = OdooInstance.objects.get(server=server, db_name="sales_db")
        self.assertEqual(obj.http_port, 8071)
        mock_dispatch.assert_called_once()

    def test_open_odoo_instance_console_ui(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-console",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.20",
            status=OdooServer.Status.PROVISIONED,
            installation_summary_text="Odoo 19 installation complete!\n  Server IP     : 203.0.113.20",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="inventory",
            db_name="inventory_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        resp = self.client.get(
            reverse("deployments_ui:odoo-instance-console", kwargs={"instance_id": instance.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Production")
        self.assertContains(resp, "Staging")
        self.assertContains(resp, "Development")
        self.assertContains(resp, "GitHistory")
        self.assertContains(resp, "Setting")
        self.assertContains(resp, "Installation Summary")
        self.assertContains(resp, "Server IP")

    def test_infrastructure_delete_requires_force_if_servers_exist(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-del",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
        )
        resp = self.client.post(
            reverse("deployments:infrastructure-delete", kwargs={"infrastructure_id": self.infrastructure.id}),
            data={},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(OdooServer.objects.filter(pk=server.pk).exists())
