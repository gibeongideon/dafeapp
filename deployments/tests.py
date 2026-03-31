import json
import io
import tarfile
import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import AsyncMock, patch

from cloud.models import CloudAccount, ExternalServer
from dns.models import DomainAssignment, DnsProviderAccount, DnsZone
from deployments.models import (
    DeploymentJob,
    EnterpriseSource,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooServer,
    TerraformRun,
)
from deployments.serializers import OdooInstanceSerializer
from organizations.models import Organization, OrganizationMembership
from subscriptions.models import Plan, Subscription
from deployments.tasks import delete_odoo_instance, _mark_server_unreachable_from_ansible_log
from users.models import VCSAccount

User = get_user_model()


def _build_enterprise_archive(
    filename="odoo_19.0+e.20260327.tar.gz",
    nested_root="ads/odoo_19.0+e.20260327/odoo-19.0+e.20260327",
):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        files = {
            f"{nested_root}/account/__manifest__.py": b"{'name': 'Accounting'}",
            f"{nested_root}/account/__init__.py": b"",
            f"{nested_root}/hr/__manifest__.py": b"{'name': 'HR'}",
            f"{nested_root}/hr/__init__.py": b"",
        }
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    buffer.seek(0)
    return SimpleUploadedFile(filename, buffer.getvalue(), content_type="application/gzip")


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
            addons_root_path="/odoo_instances/1/addons",
            addons_path_cache="/odoo/odoo-server/addons,/odoo_instances/1/addons/sales-tools",
            addons_sync_status=OdooInstance.AddonsSyncStatus.READY,
        )
        EnterpriseSource.objects.create(
            odoo_version="19",
            package_name="odoo_19.0+e.20260327",
            archive_filename="odoo_19.0+e.20260327.tar.gz",
            archive_path="/tmp/odoo_19.0+e.20260327.tar.gz",
            extract_path="/tmp/enterprise/19",
            addons_source_path="/tmp/enterprise/19/odoo-19.0+e.20260327",
            is_active=True,
            status=EnterpriseSource.Status.READY,
            uploaded_by=self.user,
        )
        OdooInstanceGitRepo.objects.create(
            instance=instance,
            repo_name="sales-tools",
            git_url="https://github.com/acme/sales-tools.git",
            branch="main",
            local_path="/odoo_instances/1/addons/sales-tools",
            status=OdooInstanceGitRepo.Status.CONNECTED,
            auto_update=True,
            created_by=self.user,
        )
        resp = self.client.get(
            reverse("deployments_ui:odoo-instance-console", kwargs={"instance_id": instance.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Addons")
        self.assertContains(resp, "Git Addon Sources")
        self.assertContains(resp, "sales-tools")
        self.assertContains(resp, "/odoo_instances/1/addons")
        self.assertContains(resp, "Production")
        self.assertContains(resp, "Staging")
        self.assertContains(resp, "Development")
        self.assertContains(resp, "GitHistory")
        self.assertContains(resp, "Setting")
        self.assertContains(resp, "Installation Summary")
        self.assertContains(resp, "Server IP")
        self.assertContains(resp, "Slide to activate")
        self.assertContains(resp, 'role="switch"')
        self.assertContains(resp, "odoo_19.0+e.20260327")

    def test_all_instances_view_hides_instance_summary_and_extra_header_copy(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-list",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.30",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="inventory",
            db_name="inventory_db",
            http_port=8070,
            status=OdooInstance.Status.RUNNING,
            installation_summary_text="Server IP     : 203.0.113.30\nAccess       : http://203.0.113.30:8070",
            created_by=self.user,
        )

        resp = self.client.get(reverse("deployments_ui:create-instance"), {"section": "instances"})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "All instances")
        self.assertContains(resp, "1 total")
        self.assertNotContains(resp, "Organization-wide instance view")
        self.assertNotContains(resp, "Pick a server from the sidebar to filter this page down to one server.")
        self.assertNotContains(resp, "Back to Servers")
        self.assertNotContains(resp, "Installation Summary")
        self.assertNotContains(resp, "Server IP     : 203.0.113.30")

    def test_server_list_reports_pyos_server_as_disconnected_after_latest_failed_check(self):
        external_server = ExternalServer.objects.create(
            organization=self.org,
            name="ssh-box",
            host="203.0.113.40",
            port=22,
            username="root",
            auth_type=ExternalServer.AuthType.DAFEAPP_KEY,
            is_verified=True,
            last_verified_at=timezone.now() - timedelta(hours=1),
            verification_error="",
        )
        pyos_infra = Infrastructure.objects.create(
            organization=self.org,
            name="ssh-box",
            infra_type=Infrastructure.InfraType.PYOS,
            external_server=external_server,
            is_connected=True,
            created_by=self.user,
        )
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=pyos_infra,
            name="odoo19-pyos",
            odoo_version="19",
            region="manual",
            size="manual",
            ip_address="203.0.113.40",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=False,
            last_checked_at=timezone.now(),
            created_by=self.user,
        )

        resp = self.client.get(reverse("deployments:odoo-server-list"))

        self.assertEqual(resp.status_code, 200)
        server_data = next(row for row in resp.json()["results"] if row["id"] == server.id)
        self.assertEqual(server_data["ssh_connection_status"], "disconnected")
        self.assertEqual(server_data["ssh_connection_message"], "Disconnected.")

    def test_instance_list_relays_disconnected_parent_server_state(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-down",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.41",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=False,
            last_checked_at=timezone.now(),
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="crm",
            db_name="crm_db",
            http_port=8071,
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        resp = self.client.get(reverse("deployments:odoo-instance-list"))

        self.assertEqual(resp.status_code, 200)
        instance_data = next(row for row in resp.json()["results"] if row["id"] == instance.id)
        self.assertEqual(instance_data["status"], OdooInstance.Status.RUNNING)
        self.assertEqual(instance_data["server"]["ssh_connection_status"], "disconnected")

    @patch("deployments.tasks._probe_server_ssh")
    def test_manual_connectivity_check_marks_server_disconnected_when_ssh_validation_fails(self, mock_probe):
        mock_probe.return_value = (False, "SSH validation failed for 203.0.113.42:22: timed out")
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-check",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.42",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=True,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-server-check", kwargs={"server_id": server.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["connectivity_status"], "disconnected")
        self.assertEqual(resp.json()["message"], "SSH validation failed for 203.0.113.42:22: timed out")
        server.refresh_from_db()
        self.assertFalse(server.is_reachable)
        self.assertIsNotNone(server.last_checked_at)
        mock_probe.assert_called_once()

    @patch("deployments.tasks._probe_server_ssh")
    def test_manual_connectivity_check_marks_server_connected_when_ssh_validation_recovers(self, mock_probe):
        mock_probe.return_value = (True, "Connected.")
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-recover",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.142",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=False,
            last_checked_at=timezone.now() - timedelta(minutes=5),
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-server-check", kwargs={"server_id": server.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["connectivity_status"], "connected")
        self.assertEqual(resp.json()["message"], "Connected.")
        server.refresh_from_db()
        self.assertTrue(server.is_reachable)
        self.assertIsNotNone(server.last_checked_at)
        mock_probe.assert_called_once()

    @patch("deployments.tasks._probe_server_ssh")
    def test_manual_connectivity_check_updates_pyos_external_server_state(self, mock_probe):
        mock_probe.return_value = (False, "Host unreachable for root@203.0.113.43:22: timed out")
        external_server = ExternalServer.objects.create(
            organization=self.org,
            name="pyos-check",
            host="203.0.113.43",
            port=22,
            username="root",
            auth_type=ExternalServer.AuthType.DAFEAPP_KEY,
            is_verified=True,
        )
        pyos_infra = Infrastructure.objects.create(
            organization=self.org,
            name="pyos-check",
            infra_type=Infrastructure.InfraType.PYOS,
            external_server=external_server,
            is_connected=True,
            created_by=self.user,
        )
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=pyos_infra,
            name="odoo19-pyos-check",
            odoo_version="19",
            region="manual",
            size="manual",
            ip_address="203.0.113.43",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=True,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-server-check", kwargs={"server_id": server.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 200)
        server.refresh_from_db()
        external_server.refresh_from_db()
        self.assertFalse(server.is_reachable)
        self.assertFalse(external_server.is_reachable)
        self.assertFalse(external_server.is_verified)
        self.assertEqual(
            external_server.verification_error,
            "Host unreachable for root@203.0.113.43:22: timed out",
        )

    def test_selected_server_instances_page_includes_disconnected_retry_loop(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-retry-ui",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.45",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=False,
            last_checked_at=timezone.now(),
            created_by=self.user,
        )

        resp = self.client.get(
            reverse("deployments_ui:create-instance"),
            {"server_id": server.id, "section": "instances"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "function retryDisconnectedVisibleServers()")
        self.assertContains(resp, "setInterval(retryDisconnectedVisibleServers, DISCONNECTED_SERVER_RETRY_INTERVAL_MS)")
        self.assertNotContains(resp, "Reachability")

    def test_ansible_unreachable_log_marks_server_and_external_server_disconnected(self):
        external_server = ExternalServer.objects.create(
            organization=self.org,
            name="pyos-ansible",
            host="203.0.113.44",
            port=22,
            username="root",
            auth_type=ExternalServer.AuthType.DAFEAPP_KEY,
            is_verified=True,
            is_reachable=True,
        )
        pyos_infra = Infrastructure.objects.create(
            organization=self.org,
            name="pyos-ansible",
            infra_type=Infrastructure.InfraType.PYOS,
            external_server=external_server,
            is_connected=True,
            created_by=self.user,
        )
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=pyos_infra,
            name="odoo19-pyos-ansible",
            odoo_version="19",
            region="manual",
            size="manual",
            ip_address="203.0.113.44",
            status=OdooServer.Status.PROVISIONED,
            is_reachable=True,
            created_by=self.user,
        )

        changed = _mark_server_unreachable_from_ansible_log(
            server,
            'fatal: [203.0.113.44]: UNREACHABLE! => {"changed": false, "msg": "Failed to connect to the host via ssh: ssh: connect to host 203.0.113.44 port 22: Connection timed out", "unreachable": true}',
        )

        self.assertTrue(changed)
        server.refresh_from_db()
        external_server.refresh_from_db()
        self.assertFalse(server.is_reachable)
        self.assertFalse(external_server.is_reachable)
        self.assertFalse(external_server.is_verified)
        self.assertIn("UNREACHABLE!", external_server.verification_error)

    def test_instance_repo_list_api_returns_instance_repos(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-repos",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
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
        repo = OdooInstanceGitRepo.objects.create(
            instance=instance,
            repo_name="stock-custom",
            git_url="git@github.com:acme/stock-custom.git",
            branch="18.0",
            auth_type=OdooInstanceGitRepo.AuthType.SSH_KEY,
            local_path="/odoo_instances/44/addons/stock-custom",
            status=OdooInstanceGitRepo.Status.DISCONNECTED,
            created_by=self.user,
        )

        resp = self.client.get(
            reverse("deployments:odoo-instance-repo-list", kwargs={"instance_id": instance.id})
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["id"], repo.id)
        self.assertEqual(payload["results"][0]["repo_name"], "stock-custom")
        self.assertEqual(payload["results"][0]["branch"], "18.0")

    @patch("deployments.views._dispatch")
    def test_instance_delete_is_blocked_while_instance_is_configuring(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-configuring",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="inventory",
            db_name="inventory_db",
            status=OdooInstance.Status.CONFIGURING,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-delete", kwargs={"instance_id": instance.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("still in progress", resp.json()["error"])
        mock_dispatch.assert_not_called()

    def test_domain_attach_is_blocked_while_domain_provisioning_is_pending(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-domain-pending",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="sales",
            db_name="sales_db",
            status=OdooInstance.Status.RUNNING,
            domain_status=OdooInstance.DomainStatus.PENDING,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-domain-attach", kwargs={"instance_id": instance.id}),
            data={"domain": "shop.example.com"},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("Domain provisioning", resp.json()["error"])

    @patch("deployments.views._dispatch")
    def test_repo_sync_is_blocked_while_another_instance_job_is_running(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-repo-lock",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="crm",
            db_name="crm_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        repo = OdooInstanceGitRepo.objects.create(
            instance=instance,
            repo_name="crm-tools",
            git_url="https://github.com/acme/crm-tools.git",
            branch="main",
            local_path="/odoo_instances/88/addons/crm-tools",
            status=OdooInstanceGitRepo.Status.CONNECTED,
            created_by=self.user,
        )
        DeploymentJob.objects.create(
            organization=self.org,
            job_type=DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
            status=DeploymentJob.Status.RUNNING,
            odoo_instance=instance,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse(
                "deployments:odoo-instance-repo-sync",
                kwargs={"instance_id": instance.id, "repo_id": repo.id},
            ),
            data={},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("deployment job", resp.json()["error"])
        mock_dispatch.assert_not_called()

    def test_server_archive_is_blocked_while_server_is_provisioning(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-server-lock",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONING,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-server-archive", kwargs={"server_id": server.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("Server provisioning", resp.json()["error"])
        server.refresh_from_db()
        self.assertTrue(server.is_active)
        self.assertEqual(server.status, OdooServer.Status.PROVISIONING)

    def test_platform_admin_can_upload_enterprise_source(self):
        self.user.is_platform_admin = True
        self.user.save(update_fields=["is_platform_admin"])

        with tempfile.TemporaryDirectory() as archive_root, tempfile.TemporaryDirectory() as extract_root:
            with override_settings(
                ODOO_ENTERPRISE_ARCHIVE_ROOT=archive_root,
                ODOO_ENTERPRISE_EXTRACT_ROOT=extract_root,
            ):
                resp = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={
                        "archive": _build_enterprise_archive(),
                    },
                )
                self.assertEqual(resp.status_code, 201)
                source = EnterpriseSource.objects.get(odoo_version="19")
                self.assertEqual(source.status, EnterpriseSource.Status.READY)
                self.assertTrue(source.is_active)
                self.assertTrue(source.archive_path.startswith(archive_root))
                self.assertTrue(source.extract_path.startswith(extract_root))
                self.assertRegex(source.package_name, r"^\d{14}-odoo_19\.0\+e\.20260327\.tar$")
                self.assertIn("odoo-19.0+e.20260327", source.addons_source_path)

    def test_uploading_same_release_date_replaces_previous_enterprise_source(self):
        self.user.is_platform_admin = True
        self.user.save(update_fields=["is_platform_admin"])

        with tempfile.TemporaryDirectory() as archive_root, tempfile.TemporaryDirectory() as extract_root:
            with override_settings(
                ODOO_ENTERPRISE_ARCHIVE_ROOT=archive_root,
                ODOO_ENTERPRISE_EXTRACT_ROOT=extract_root,
            ):
                first_resp = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive()},
                )
                self.assertEqual(first_resp.status_code, 201)
                first_source = EnterpriseSource.objects.get(odoo_version="19")
                first_archive_path = first_source.archive_path
                first_extract_path = first_source.extract_path

                second_resp = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive()},
                )
                self.assertEqual(second_resp.status_code, 201)
                self.assertEqual(EnterpriseSource.objects.filter(odoo_version="19").count(), 1)
                second_source = EnterpriseSource.objects.get(odoo_version="19")
                self.assertEqual(first_source.id, second_source.id)
                self.assertFalse(Path(first_archive_path).exists())
                self.assertFalse(Path(first_extract_path).exists())
                self.assertTrue(Path(second_source.archive_path).exists())
                self.assertTrue(Path(second_source.extract_path).exists())

    def test_uploading_older_release_is_rejected_when_newer_one_exists(self):
        self.user.is_platform_admin = True
        self.user.save(update_fields=["is_platform_admin"])

        with tempfile.TemporaryDirectory() as archive_root, tempfile.TemporaryDirectory() as extract_root:
            with override_settings(
                ODOO_ENTERPRISE_ARCHIVE_ROOT=archive_root,
                ODOO_ENTERPRISE_EXTRACT_ROOT=extract_root,
            ):
                newer_resp = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive(filename="odoo_19.0+e.20260327.tar.gz")},
                )
                self.assertEqual(newer_resp.status_code, 201)

                older_resp = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive(filename="odoo_19.0+e.20260301.tar.gz")},
                )
                self.assertEqual(older_resp.status_code, 400)
                self.assertIn("newer Enterprise source already exists", older_resp.json()["error"])
                self.assertEqual(EnterpriseSource.objects.filter(odoo_version="19").count(), 1)

    def test_enterprise_uploads_are_kept_in_separate_version_folders(self):
        self.user.is_platform_admin = True
        self.user.save(update_fields=["is_platform_admin"])

        with tempfile.TemporaryDirectory() as archive_root, tempfile.TemporaryDirectory() as extract_root:
            with override_settings(
                ODOO_ENTERPRISE_ARCHIVE_ROOT=archive_root,
                ODOO_ENTERPRISE_EXTRACT_ROOT=extract_root,
            ):
                resp_19 = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive(filename="odoo_19.0+e.20260327.tar.gz")},
                )
                resp_18 = self.client.post(
                    reverse("deployments:enterprise-source-list"),
                    data={"archive": _build_enterprise_archive(filename="odoo_18.0+e.20260320.tar.gz")},
                )

                self.assertEqual(resp_19.status_code, 201)
                self.assertEqual(resp_18.status_code, 201)

                source_19 = EnterpriseSource.objects.get(odoo_version="19")
                source_18 = EnterpriseSource.objects.get(odoo_version="18")

                self.assertIn(f"{Path(archive_root) / '19'}", source_19.archive_path)
                self.assertIn(f"{Path(extract_root) / '19'}", source_19.extract_path)
                self.assertIn(f"{Path(archive_root) / '18'}", source_18.archive_path)
                self.assertIn(f"{Path(extract_root) / '18'}", source_18.extract_path)

    def test_platform_admin_can_switch_active_enterprise_source(self):
        self.user.is_platform_admin = True
        self.user.save(update_fields=["is_platform_admin"])
        current = EnterpriseSource.objects.create(
            odoo_version="19",
            package_name="odoo_19.0+e.20260301",
            archive_filename="odoo_19.0+e.20260301.tar.gz",
            archive_path="/tmp/odoo_19.0+e.20260301.tar.gz",
            extract_path="/tmp/enterprise/current",
            addons_source_path="/tmp/enterprise/current/odoo-19.0+e.20260301",
            is_active=True,
            status=EnterpriseSource.Status.READY,
            uploaded_by=self.user,
        )
        next_source = EnterpriseSource.objects.create(
            odoo_version="19",
            package_name="odoo_19.0+e.20260327",
            archive_filename="odoo_19.0+e.20260327.tar.gz",
            archive_path="/tmp/odoo_19.0+e.20260327.tar.gz",
            extract_path="/tmp/enterprise/next",
            addons_source_path="/tmp/enterprise/next/odoo-19.0+e.20260327",
            is_active=False,
            status=EnterpriseSource.Status.READY,
            uploaded_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:enterprise-source-activate", kwargs={"source_id": next_source.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 200)
        current.refresh_from_db()
        next_source.refresh_from_db()
        self.assertFalse(current.is_active)
        self.assertTrue(next_source.is_active)

    @patch("deployments.views._dispatch")
    def test_instance_enterprise_activate_api_queues_job(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-enterprise",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.55",
            status=OdooServer.Status.PROVISIONED,
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
        source = EnterpriseSource.objects.create(
            odoo_version="19",
            package_name="odoo_19.0+e.20260327",
            archive_filename="odoo_19.0+e.20260327.tar.gz",
            archive_path="/tmp/odoo_19.0+e.20260327.tar.gz",
            extract_path="/tmp/enterprise/19",
            addons_source_path="/tmp/enterprise/19/odoo-19.0+e.20260327",
            is_active=True,
            status=EnterpriseSource.Status.READY,
            uploaded_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-enterprise-activate", kwargs={"instance_id": instance.id}),
            data={},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["enterprise_source_name"], source.package_name)
        self.assertEqual(payload["enterprise_status"], OdooInstance.EnterpriseStatus.PENDING)
        job = DeploymentJob.objects.get(
            odoo_instance=instance,
            job_type=DeploymentJob.JobType.ACTIVATE_ENTERPRISE,
        )
        mock_dispatch.assert_called_once()
        self.assertEqual(mock_dispatch.call_args[0][1:], (instance.id, source.id, job.id))

    @patch("deployments.views._dispatch")
    def test_instance_repo_create_api_creates_repo_and_clone_job(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-repo-create",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.21",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="inventory",
            db_name="inventory_db",
            status=OdooInstance.Status.RUNNING,
            addons_root_path="/odoo/instances/inventory_db/addons/custom",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-list", kwargs={"instance_id": instance.id}),
            data=json.dumps({
                "repo_name": "sales-tools",
                "git_url": "https://github.com/acme/sales-tools.git",
                "branch": "18.0",
                "auth_type": "TOKEN",
                "credential_name": "sales-pat",
                "git_username": "oauth2",
                "access_token": "ghp_secret_123",
                "auto_update": "true",
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201)
        repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="sales-tools")
        self.assertEqual(repo.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
        self.assertTrue(repo.auto_update)
        self.assertTrue(repo.local_path.endswith("/sales-tools"))
        self.assertTrue(repo.credential_id)
        self.assertEqual(repo.credential.name, "sales-pat")
        self.assertNotEqual(repo.credential.encrypted_access_token, "ghp_secret_123")
        job = DeploymentJob.objects.get(odoo_instance=instance, job_type=DeploymentJob.JobType.CLONE_INSTANCE_REPO)
        mock_dispatch.assert_called_once()
        self.assertEqual(job.status, DeploymentJob.Status.QUEUED)

    @patch("deployments.views._dispatch")
    def test_instance_repo_update_branch_queues_branch_job(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-repo-branch",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.22",
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
        repo = OdooInstanceGitRepo.objects.create(
            instance=instance,
            repo_name="stock-tools",
            git_url="https://github.com/acme/stock-tools.git",
            branch="18.0",
            auth_type=OdooInstanceGitRepo.AuthType.PUBLIC,
            local_path="/odoo/instances/inventory_db/addons/stock-tools",
            status=OdooInstanceGitRepo.Status.CONNECTED,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-detail", kwargs={"instance_id": instance.id, "repo_id": repo.id}),
            data=json.dumps({"branch": "19.0"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        repo.refresh_from_db()
        self.assertEqual(repo.branch, "19.0")
        job = DeploymentJob.objects.get(
            odoo_instance=instance,
            job_type=DeploymentJob.JobType.CHECKOUT_INSTANCE_REPO_BRANCH,
        )
        self.assertEqual(job.status, DeploymentJob.Status.QUEUED)
        mock_dispatch.assert_called_once()

    @patch("deployments.views._dispatch")
    def test_github_oauth_repo_create_uses_vcs_account_as_token_source(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-repo-github",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.23",
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
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-list", kwargs={"instance_id": instance.id}),
            data=json.dumps({
                "repo_name": "octo-tools",
                "git_url": "https://github.com/octocat/octo-tools.git",
                "branch": "main",
                "auth_type": "GITHUB_OAUTH",
                "credential_name": "octocat-github",
                "github_account_id": vcs.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201)
        repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="octo-tools")
        self.assertEqual(repo.auth_type, OdooInstanceGitRepo.AuthType.GITHUB_OAUTH)
        self.assertTrue(repo.credential_id)
        self.assertEqual(repo.credential.github_account_id, vcs.id)
        self.assertEqual(repo.credential.git_username, "octocat")
        self.assertEqual(repo.credential.encrypted_access_token, "")
        self.assertEqual(repo.credential.access_token, vcs.access_token)
        mock_dispatch.assert_called_once()

    def test_git_credentials_endpoint_lists_github_accounts(self):
        VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )
        GitRepositoryCredential.objects.create(
            organization=self.org,
            name="public-readonly",
            auth_type=GitRepositoryCredential.AuthType.PUBLIC,
            created_by=self.user,
        )

        resp = self.client.get(reverse("deployments:git-credential-list"))

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["name"], "public-readonly")
        self.assertEqual(len(payload["github_accounts"]), 1)
        self.assertEqual(payload["github_accounts"][0]["username"], "octocat")

    @patch("deployments.views.requests.get")
    def test_github_repo_list_defaults_to_connected_account(self, mock_get):
        VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )
        mock_get.return_value.json.return_value = [
            {
                "id": 7,
                "full_name": "octocat/octo-tools",
                "name": "octo-tools",
                "default_branch": "main",
                "private": True,
                "clone_url": "https://github.com/octocat/octo-tools.git",
                "ssh_url": "git@github.com:octocat/octo-tools.git",
            }
        ]
        mock_get.return_value.raise_for_status.return_value = None

        resp = self.client.get(reverse("deployments:github-repo-list"))

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["results"][0]["full_name"], "octocat/octo-tools")
        self.assertTrue(mock_get.called)

    @patch("deployments.views._create_github_repository")
    def test_create_github_repo_returns_repo_data(self, mock_create_repo):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-create-upload",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.24",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="website",
            db_name="website_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )
        mock_create_repo.return_value = {
            "name": "addon-bundle",
            "full_name": "octocat/addon-bundle",
            "clone_url": "https://github.com/octocat/addon-bundle.git",
            "default_branch": "main",
            "html_url": "https://github.com/octocat/addon-bundle",
            "private": True,
        }

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-create-github", kwargs={"instance_id": instance.id}),
            data=json.dumps({"repo_name": "addon-bundle"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["github_repo"]["full_name"], "octocat/addon-bundle")
        self.assertEqual(payload["linked_repo"]["repo_name"], "addon-bundle")
        self.assertEqual(payload["linked_repo"]["status"], OdooInstanceGitRepo.Status.DISCONNECTED)
        linked_repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="addon-bundle")
        self.assertEqual(payload["linked_repo"]["id"], linked_repo.id)
        mock_create_repo.assert_called_once()

    @patch("deployments.views._create_github_repository")
    def test_create_github_repo_accepts_personal_access_token_auth(self, mock_create_repo):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-create-upload-pat",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.28",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="website",
            db_name="website_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        mock_create_repo.return_value = {
            "name": "addon-bundle",
            "full_name": "octocat/addon-bundle",
            "clone_url": "https://github.com/octocat/addon-bundle.git",
            "default_branch": "main",
            "html_url": "https://github.com/octocat/addon-bundle",
            "private": True,
        }

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-create-github", kwargs={"instance_id": instance.id}),
            data=json.dumps(
                {
                    "repo_name": "addon-bundle",
                    "auth_type": "TOKEN",
                    "git_username": "octocat",
                    "access_token": "github_pat_secret_123",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["github_repo"]["full_name"], "octocat/addon-bundle")
        self.assertEqual(payload["linked_repo"]["repo_name"], "addon-bundle")
        self.assertEqual(payload["linked_repo"]["auth_type"], OdooInstanceGitRepo.AuthType.TOKEN)
        mock_create_repo.assert_called_once()

    @patch("deployments.views._create_github_repository")
    def test_create_github_repo_returns_reconnect_hint_on_permission_error(self, mock_create_repo):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-create-upload-error",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.29",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="website",
            db_name="website_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )
        mock_create_repo.side_effect = RuntimeError(
            "GitHub denied repository creation for this connection. Disconnect and reconnect GitHub from Connections so DafeApp gets repository write access, then try again."
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-create-github", kwargs={"instance_id": instance.id}),
            data=json.dumps({"repo_name": "addon-bundle"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        payload = resp.json()
        self.assertIn("Disconnect and reconnect GitHub", payload["error"])
        self.assertEqual(payload["reconnect_url"], reverse("socialaccount_connections"))

    @patch("deployments.views._create_github_repository")
    def test_create_github_repo_with_connected_account_falls_back_to_saved_pat(self, mock_create_repo):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-create-upload-fallback",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.30",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="website",
            db_name="website_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-oauth-token",
            is_active=True,
        )
        saved_pat = GitRepositoryCredential.objects.create(
            organization=self.org,
            name="octocat-pat",
            auth_type=GitRepositoryCredential.AuthType.TOKEN,
            git_username="octocat",
            created_by=self.user,
        )
        saved_pat._raw_access_token = "github_pat_saved_123"
        saved_pat.save()

        def create_repo_side_effect(*, actor, repo_name, private=True):
            self.assertEqual(repo_name, "addon-bundle")
            if actor.auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
                self.assertEqual(actor.username, vcs.username)
                raise RuntimeError(
                    "GitHub denied repository creation for this connection. Disconnect and reconnect GitHub from Connections so DafeApp gets repository write access, then try again."
                )
            self.assertEqual(actor.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
            self.assertEqual(actor.username, "octocat")
            return {
                "name": "addon-bundle",
                "full_name": "octocat/addon-bundle",
                "clone_url": "https://github.com/octocat/addon-bundle.git",
                "default_branch": "main",
                "html_url": "https://github.com/octocat/addon-bundle",
                "private": private,
            }

        mock_create_repo.side_effect = create_repo_side_effect

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-create-github", kwargs={"instance_id": instance.id}),
            data=json.dumps(
                {
                    "repo_name": "addon-bundle",
                    "auth_type": "GITHUB_OAUTH",
                    "github_account_id": vcs.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["github_repo"]["full_name"], "octocat/addon-bundle")
        self.assertEqual(payload["linked_repo"]["auth_type"], OdooInstanceGitRepo.AuthType.TOKEN)
        self.assertEqual(mock_create_repo.call_count, 2)

    @patch("deployments.views._push_zip_to_github_repo")
    @patch("deployments.views._dispatch")
    def test_upload_to_github_creates_repo_and_instance_link(self, mock_dispatch, mock_publish):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-upload",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.25",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="marketing",
            db_name="marketing_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-upload-github", kwargs={"instance_id": instance.id}),
            data={
                "repo_name": "addon-bundle",
                "full_name": "octocat/addon-bundle",
                "clone_url": "https://github.com/octocat/addon-bundle.git",
                "zip_file": SimpleUploadedFile("addon-bundle.zip", b"PK\x03\x04fake-zip"),
            },
        )

        self.assertEqual(resp.status_code, 201)
        repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="addon-bundle")
        self.assertEqual(repo.auth_type, OdooInstanceGitRepo.AuthType.GITHUB_OAUTH)
        self.assertEqual(repo.credential.github_account_id, vcs.id)
        self.assertEqual(repo.git_url, "https://github.com/octocat/addon-bundle.git")
        mock_publish.assert_called_once()
        mock_dispatch.assert_called_once()

    @patch("deployments.views._push_zip_to_github_repo")
    @patch("deployments.views._dispatch")
    def test_upload_to_github_uses_existing_linked_repo_when_repo_id_is_provided(self, mock_dispatch, mock_publish):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-upload-linked",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.25",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="marketing",
            db_name="marketing_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-token",
            is_active=True,
        )
        credential = GitRepositoryCredential.objects.create(
            organization=self.org,
            name="github-octocat",
            auth_type=GitRepositoryCredential.AuthType.GITHUB_OAUTH,
            github_account=vcs,
            git_username="octocat",
            created_by=self.user,
        )
        linked_repo = OdooInstanceGitRepo.objects.create(
            instance=instance,
            credential=credential,
            repo_name="addon-bundle",
            git_url="https://github.com/octocat/addon-bundle.git",
            branch="main",
            default_branch="main",
            auth_type=OdooInstanceGitRepo.AuthType.GITHUB_OAUTH,
            local_path="/odoo/instances/marketing_db/addons/addon-bundle",
            status=OdooInstanceGitRepo.Status.DISCONNECTED,
            last_error="Repository created on GitHub. Upload a zip or sync content to finish linking it to this instance.",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-upload-github", kwargs={"instance_id": instance.id}),
            data={
                "repo_id": linked_repo.id,
                "zip_file": SimpleUploadedFile("addon-bundle.zip", b"PK\x03\x04fake-zip"),
            },
        )

        self.assertEqual(resp.status_code, 201)
        linked_repo.refresh_from_db()
        self.assertEqual(OdooInstanceGitRepo.objects.filter(instance=instance, repo_name="addon-bundle").count(), 1)
        self.assertEqual(linked_repo.auth_type, OdooInstanceGitRepo.AuthType.GITHUB_OAUTH)
        mock_publish.assert_called_once()
        mock_dispatch.assert_called_once()

    @patch("deployments.views._push_zip_to_github_repo")
    @patch("deployments.views._dispatch")
    def test_upload_to_github_uses_existing_linked_token_credential_when_repo_id_is_provided(self, mock_dispatch, mock_publish):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-upload-linked-token",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.35",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="warehouse",
            db_name="warehouse_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        credential = GitRepositoryCredential.objects.create(
            organization=self.org,
            name="octocat-pat",
            auth_type=GitRepositoryCredential.AuthType.TOKEN,
            git_username="octocat",
            created_by=self.user,
        )
        credential._raw_access_token = "github_pat_saved_333"
        credential.save()
        linked_repo = OdooInstanceGitRepo.objects.create(
            instance=instance,
            credential=credential,
            repo_name="addon-bundle",
            git_url="https://github.com/octocat/addon-bundle.git",
            branch="main",
            default_branch="main",
            auth_type=OdooInstanceGitRepo.AuthType.TOKEN,
            local_path="/odoo/instances/warehouse_db/addons/addon-bundle",
            status=OdooInstanceGitRepo.Status.DISCONNECTED,
            last_error="Repository created on GitHub. Upload a zip or sync content to finish linking it to this instance.",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-upload-github", kwargs={"instance_id": instance.id}),
            data={
                "repo_id": linked_repo.id,
                "zip_file": SimpleUploadedFile("addon-bundle.zip", b"PK\x03\x04fake-zip"),
            },
        )

        self.assertEqual(resp.status_code, 201)
        linked_repo.refresh_from_db()
        self.assertEqual(linked_repo.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
        self.assertEqual(linked_repo.credential_id, credential.id)
        mock_publish.assert_called_once()
        mock_dispatch.assert_called_once()

    @patch("deployments.views._push_zip_to_github_repo")
    @patch("deployments.views._dispatch")
    def test_upload_to_github_with_personal_access_token_creates_token_credential(self, mock_dispatch, mock_publish):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-upload-pat",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.26",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="marketing",
            db_name="marketing_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-upload-github", kwargs={"instance_id": instance.id}),
            data={
                "auth_type": "TOKEN",
                "repo_name": "addon-bundle",
                "full_name": "octocat/addon-bundle",
                "clone_url": "https://github.com/octocat/addon-bundle.git",
                "git_username": "octocat",
                "access_token": "github_pat_secret_456",
                "credential_name": "octocat-pat",
                "zip_file": SimpleUploadedFile("addon-bundle.zip", b"PK\\x03\\x04fake-zip"),
            },
        )

        self.assertEqual(resp.status_code, 201)
        repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="addon-bundle")
        self.assertEqual(repo.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
        self.assertEqual(repo.credential.name, "octocat-pat")
        self.assertEqual(repo.credential.git_username, "octocat")
        self.assertNotEqual(repo.credential.encrypted_access_token, "github_pat_secret_456")
        mock_publish.assert_called_once()
        mock_dispatch.assert_called_once()

    @patch("deployments.views._push_zip_to_github_repo")
    @patch("deployments.views._dispatch")
    def test_upload_to_github_with_connected_account_falls_back_to_saved_pat_credential(self, mock_dispatch, mock_publish):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-upload-fallback",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address="203.0.113.31",
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="marketing",
            db_name="marketing_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )
        vcs = VCSAccount.objects.create(
            user=self.user,
            provider=VCSAccount.Provider.GITHUB,
            username="octocat",
            encrypted_access_token="encrypted-oauth-token",
            is_active=True,
        )
        saved_pat = GitRepositoryCredential.objects.create(
            organization=self.org,
            name="octocat-pat",
            auth_type=GitRepositoryCredential.AuthType.TOKEN,
            git_username="octocat",
            created_by=self.user,
        )
        saved_pat._raw_access_token = "github_pat_saved_789"
        saved_pat.save()

        def publish_side_effect(*, actor, user, full_name, zip_file, branch="main"):
            self.assertEqual(full_name, "octocat/addon-bundle")
            if actor.auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
                self.assertEqual(actor.username, vcs.username)
                raise RuntimeError(
                    "GitHub denied repository creation for this connection. Disconnect and reconnect GitHub from Connections so DafeApp gets repository write access, then try again."
                )
            self.assertEqual(actor.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
            self.assertEqual(actor.username, "octocat")
            return None

        mock_publish.side_effect = publish_side_effect

        resp = self.client.post(
            reverse("deployments:odoo-instance-repo-upload-github", kwargs={"instance_id": instance.id}),
            data={
                "auth_type": "GITHUB_OAUTH",
                "github_account_id": vcs.id,
                "repo_name": "addon-bundle",
                "full_name": "octocat/addon-bundle",
                "clone_url": "https://github.com/octocat/addon-bundle.git",
                "zip_file": SimpleUploadedFile("addon-bundle.zip", b"PK\\x03\\x04fake-zip"),
            },
        )

        self.assertEqual(resp.status_code, 201)
        repo = OdooInstanceGitRepo.objects.get(instance=instance, repo_name="addon-bundle")
        self.assertEqual(repo.auth_type, OdooInstanceGitRepo.AuthType.TOKEN)
        self.assertEqual(repo.credential_id, saved_pat.id)
        self.assertEqual(mock_publish.call_count, 2)
        mock_dispatch.assert_called_once()

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

    def test_archive_hides_server_and_delete_removes_it(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-archive",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            is_active=True,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="archive-check",
            db_name="archive_check_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        archive_resp = self.client.post(
            reverse("deployments:odoo-server-archive", kwargs={"server_id": server.id}),
            data={},
        )
        self.assertEqual(archive_resp.status_code, 200)
        server.refresh_from_db()
        self.assertFalse(server.is_active)
        self.assertEqual(server.status, OdooServer.Status.ARCHIVED)
        self.assertTrue(OdooInstance.objects.filter(pk=instance.pk).exists())

        list_resp = self.client.get(reverse("deployments:odoo-server-list"))
        self.assertNotContains(list_resp, "odoo19-archive")

        delete_resp = self.client.post(
            reverse("deployments:odoo-server-delete", kwargs={"server_id": server.id}),
            data={},
        )
        self.assertEqual(delete_resp.status_code, 200)
        self.assertFalse(OdooServer.objects.filter(pk=server.pk).exists())
        self.assertFalse(OdooInstance.objects.filter(pk=instance.pk).exists())

    def test_delete_odoo_instance_hard_removes_record(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-instance-delete",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            ip_address=None,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="sales",
            db_name="sales_db",
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        delete_odoo_instance(instance.id)

        self.assertFalse(OdooInstance.objects.filter(pk=instance.pk).exists())

    @patch("deployments.signals.get_channel_layer")
    def test_server_delete_posts_removed_event(self, mock_get_channel_layer):
        mock_channel_layer = mock_get_channel_layer.return_value
        mock_channel_layer.group_send = AsyncMock()
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            name="odoo19-signal-delete",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            is_active=True,
        )

        server_id = server.id
        server.delete()

        mock_channel_layer.group_send.assert_awaited_once_with(
            f"odoo.server.{server_id}",
            {"type": "server.update", "payload": {"type": "removed", "server_id": server_id, "reason": "deleted"}},
        )


class DnsSslDeploymentFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="dns-ssl@test.com", password="pass")
        cls.org = Organization.objects.create(name="DNS SSL Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.plan = Plan.objects.create(
            name="Pro",
            plan_type=Plan.PlanType.GROWTH,
            price_monthly="99.00",
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
            name="Managed Infra",
            infra_type=Infrastructure.InfraType.MANAGED,
            cloud_account=cls.account,
            is_connected=True,
        )
        cls.dns_account = DnsProviderAccount.objects.create(
            organization=cls.org,
            name="Cloudflare",
            provider=DnsProviderAccount.Provider.CLOUDFLARE,
            created_by=cls.user,
        )
        cls.zone = DnsZone.objects.create(
            organization=cls.org,
            provider_account=cls.dns_account,
            name="example.com",
            provider_zone_id="zone-123",
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    @override_settings(PLATFORM_BASE_DOMAIN="dafeapp.com")
    @patch("deployments.views._dispatch")
    def test_create_server_uses_global_platform_domain_settings(self, mock_dispatch):
        response = self.client.post(
            reverse("deployments:odoo-server-create"),
            data={
                "name": "dns-enabled",
                "infrastructure_id": self.infrastructure.id,
                "odoo_version": "19",
                "region": "nyc3",
                "size": "s-2vcpu-4gb",
            },
        )
        self.assertEqual(response.status_code, 201)
        server = OdooServer.objects.get(name="dns-enabled")
        self.assertFalse(server.managed_dns_enabled)
        self.assertTrue(server.domain_routing_enabled)
        self.assertIsNone(server.managed_dns_zone_id)
        self.assertEqual(server.dns_domain, "dafeapp.com")
        mock_dispatch.assert_called_once()

    @override_settings(PLATFORM_BASE_DOMAIN="dafeapp.com")
    @patch("deployments.views._dispatch")
    def test_create_instance_reserves_domain_assignment(self, mock_dispatch):
        platform_label = "nexora4821"
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            managed_dns_enabled=True,
            managed_dns_zone=self.zone,
            domain_routing_enabled=True,
            tls_mode=OdooServer.TLSMode.LETS_ENCRYPT,
            name="routing-host",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.50",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )

        response = self.client.post(
            reverse("deployments:odoo-instance-create"),
            data={
                "server_id": server.id,
                "name": "crm",
                "db_name": "crm_db",
                "platform_domain_label": platform_label,
                "custom_domain": "crm.example.com",
                "http_port": 8072,
            },
        )
        self.assertEqual(response.status_code, 201)
        instance = OdooInstance.objects.get(server=server, db_name="crm_db")
        primary = DomainAssignment.objects.get(instance=instance, is_primary=True, status=DomainAssignment.Status.PENDING)
        custom = DomainAssignment.objects.get(instance=instance, domain="crm.example.com", status=DomainAssignment.Status.PENDING)
        self.assertEqual(instance.domain_status, OdooInstance.DomainStatus.PENDING)
        self.assertEqual(instance.ssl_status, OdooInstance.SSLStatus.PENDING)
        self.assertEqual(instance.domain, f"{platform_label}.dafeapp.com")
        self.assertEqual(primary.domain, f"{platform_label}.dafeapp.com")
        self.assertEqual(primary.source, DomainAssignment.Source.PLATFORM)
        self.assertEqual(custom.zone_id, self.zone.id)
        mock_dispatch.assert_called_once()

    @override_settings(PLATFORM_BASE_DOMAIN="dafeapp.com")
    @patch("deployments.views._dispatch")
    def test_create_instance_rejects_reused_platform_domain_label(self, mock_dispatch):
        existing_server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            domain_routing_enabled=True,
            tls_mode=OdooServer.TLSMode.LETS_ENCRYPT,
            name="existing-host",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.55",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        OdooInstance.objects.create(
            organization=self.org,
            server=existing_server,
            name="existing-app",
            db_name="existing_db",
            domain="nexora4821.dafeapp.com",
            http_port=8071,
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        target_server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            domain_routing_enabled=True,
            tls_mode=OdooServer.TLSMode.LETS_ENCRYPT,
            name="target-host",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.56",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )

        response = self.client.post(
            reverse("deployments:odoo-instance-create"),
            data={
                "server_id": target_server.id,
                "name": "new-app",
                "db_name": "new_app_db",
                "platform_domain_label": "nexora4821",
                "http_port": 8072,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "That DafeApp domain prefix is already used. Choose another one or regenerate.",
        )
        mock_dispatch.assert_not_called()

    @override_settings(PLATFORM_BASE_DOMAIN="dafeapp.com")
    @patch("deployments.views._dispatch")
    def test_domain_attach_and_detach_endpoints_queue_tasks(self, mock_dispatch):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            domain_routing_enabled=True,
            tls_mode=OdooServer.TLSMode.LETS_ENCRYPT,
            name="attach-host",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.60",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="inventory",
            db_name="inventory_db",
            http_port=8075,
            status=OdooInstance.Status.RUNNING,
            created_by=self.user,
        )

        attach_response = self.client.post(
            reverse("deployments:odoo-instance-domain-attach", kwargs={"instance_id": instance.id}),
            data={"domain": "inventory.example.com"},
        )
        self.assertEqual(attach_response.status_code, 200)
        instance.refresh_from_db()
        self.assertEqual(instance.domain, "")
        self.assertTrue(DomainAssignment.objects.filter(instance=instance, domain="inventory.example.com").exists())

        detach_response = self.client.post(
            reverse("deployments:odoo-instance-domain-detach", kwargs={"instance_id": instance.id}),
            data={"domain": "inventory.example.com"},
        )
        self.assertEqual(detach_response.status_code, 200)
        self.assertEqual(mock_dispatch.call_count, 2)

    @override_settings(PLATFORM_BASE_DOMAIN="dafeapp.com")
    def test_instance_serializer_exposes_preferred_domain_url(self):
        server = OdooServer.objects.create(
            organization=self.org,
            infrastructure=self.infrastructure,
            cloud_account=self.account,
            domain_routing_enabled=True,
            tls_mode=OdooServer.TLSMode.LETS_ENCRYPT,
            name="serializer-host",
            odoo_version="19",
            region="nyc3",
            size="s-2vcpu-4gb",
            ip_address="203.0.113.70",
            status=OdooServer.Status.PROVISIONED,
            created_by=self.user,
        )
        instance = OdooInstance.objects.create(
            organization=self.org,
            server=server,
            name="website",
            db_name="website_db",
            domain="website.dafeapp.com",
            http_port=8078,
            status=OdooInstance.Status.RUNNING,
            domain_status=OdooInstance.DomainStatus.ACTIVE,
            ssl_status=OdooInstance.SSLStatus.ACTIVE,
            ssl_enabled=True,
            created_by=self.user,
        )
        DomainAssignment.objects.create(
            organization=self.org,
            instance=instance,
            domain="website.dafeapp.com",
            source=DomainAssignment.Source.PLATFORM,
            is_primary=True,
            status=DomainAssignment.Status.ACTIVE,
            is_managed=True,
        )
        DomainAssignment.objects.create(
            organization=self.org,
            instance=instance,
            domain="erp.customer.com",
            source=DomainAssignment.Source.CUSTOM,
            is_primary=False,
            status=DomainAssignment.Status.ACTIVE,
            is_managed=False,
        )

        data = OdooInstanceSerializer(instance).data
        self.assertEqual(data["direct_access_url"], "http://203.0.113.70:8078")
        self.assertEqual(data["domain_access_url"], "https://website.dafeapp.com")
        self.assertEqual(data["preferred_access_url"], "https://website.dafeapp.com")
        self.assertEqual(data["access_url"], "https://website.dafeapp.com")
        self.assertEqual(len(data["domain_assignments"]), 2)
