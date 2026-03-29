import json
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import AsyncMock, patch

from cloud.models import CloudAccount, ExternalServer
from deployments.models import (
    DeploymentJob,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooServer,
    TerraformRun,
)
from organizations.models import Organization, OrganizationMembership
from subscriptions.models import Plan, Subscription
from deployments.tasks import delete_odoo_instance, _mark_server_unreachable_from_ansible_log
from users.models import VCSAccount

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
            addons_root_path="/odoo_instances/1/addons",
            addons_path_cache="/odoo/odoo-server/addons,/odoo_instances/1/addons/sales-tools",
            addons_sync_status=OdooInstance.AddonsSyncStatus.READY,
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
        self.assertEqual(server_data["ssh_connection_message"], "Reachability failed.")

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
        mock_create_repo.assert_called_once()

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
