from django.urls import path

from deployments import views

app_name = "deployments"

urlpatterns = [
    path("create/", views.DeploymentCreateView.as_view(), name="create-instance"),
    path("odoo/instances/<int:instance_id>/", views.OdooInstanceConsoleView.as_view(), name="odoo-instance-console"),
    path("options/<int:account_id>/", views.CloudAccountOptionsAPIView.as_view(), name="account-options"),
    path("instances/<int:instance_id>/", views.InstanceDetailAPIView.as_view(), name="instance-detail"),
    path("runs/<int:run_id>/", views.TerraformRunDetailAPIView.as_view(), name="run-detail"),
    path("odoo/servers/", views.OdooServerListAPIView.as_view(), name="odoo-server-list"),
    path("odoo/servers/create/", views.OdooServerCreateAPIView.as_view(), name="odoo-server-create"),
    path("odoo/servers/<int:server_id>/", views.OdooServerDetailAPIView.as_view(), name="odoo-server-detail"),
    path("odoo/servers/<int:server_id>/archive/", views.OdooServerArchiveAPIView.as_view(), name="odoo-server-archive"),
    path("odoo/servers/<int:server_id>/delete/", views.OdooServerDeleteAPIView.as_view(), name="odoo-server-delete"),
    path("odoo/servers/<int:server_id>/check/", views.OdooServerCheckConnectivityView.as_view(), name="odoo-server-check"),
    path("pyos/vps/create/", views.PyosVpsCreateAPIView.as_view(), name="pyos-vps-create"),
    path("odoo/instances/", views.OdooInstanceListAPIView.as_view(), name="odoo-instance-list"),
    path("odoo/instances/<int:instance_id>/runtime-logs/", views.OdooInstanceRuntimeLogsAPIView.as_view(), name="odoo-instance-runtime-logs"),
    path("odoo/instances/<int:instance_id>/repos/", views.OdooInstanceGitRepoListAPIView.as_view(), name="odoo-instance-repo-list"),
    path("odoo/instances/<int:instance_id>/repos/create-github/", views.OdooInstanceGitRepoCreateGitHubAPIView.as_view(), name="odoo-instance-repo-create-github"),
    path("odoo/instances/<int:instance_id>/repos/upload-to-github/", views.OdooInstanceGitRepoUploadToGitHubAPIView.as_view(), name="odoo-instance-repo-upload-github"),
    path("odoo/instances/<int:instance_id>/repos/<int:repo_id>/", views.OdooInstanceGitRepoDetailAPIView.as_view(), name="odoo-instance-repo-detail"),
    path("odoo/instances/<int:instance_id>/repos/<int:repo_id>/status/", views.OdooInstanceGitRepoStatusAPIView.as_view(), name="odoo-instance-repo-status"),
    path("odoo/instances/<int:instance_id>/repos/<int:repo_id>/sync/", views.OdooInstanceGitRepoSyncAPIView.as_view(), name="odoo-instance-repo-sync"),
    path("odoo/instances/<int:instance_id>/repos/<int:repo_id>/rollback/", views.OdooInstanceGitRepoRollbackAPIView.as_view(), name="odoo-instance-repo-rollback"),
    path("odoo/instances/<int:instance_id>/repos/<int:repo_id>/delete/", views.OdooInstanceGitRepoDeleteAPIView.as_view(), name="odoo-instance-repo-delete"),
    path("git-credentials/", views.GitRepositoryCredentialListCreateAPIView.as_view(), name="git-credential-list"),
    path("github/webhook/", views.GitHubWebhookAPIView.as_view(), name="github-webhook"),
    path("enterprise/sources/", views.EnterpriseSourceListCreateAPIView.as_view(), name="enterprise-source-list"),
    path("enterprise/sources/<int:source_id>/activate/", views.EnterpriseSourceActivateAPIView.as_view(), name="enterprise-source-activate"),
    path("github/repos/", views.GitHubRepositoryListAPIView.as_view(), name="github-repo-list"),
    path("github/branches/", views.GitHubBranchListAPIView.as_view(), name="github-branch-list"),
    path("odoo/instances/create/", views.OdooInstanceCreateAPIView.as_view(), name="odoo-instance-create"),
    path("odoo/instances/<int:instance_id>/enterprise/activate/", views.OdooInstanceEnterpriseActivateAPIView.as_view(), name="odoo-instance-enterprise-activate"),
    path("odoo/instances/<int:instance_id>/domain/attach/", views.OdooInstanceDomainAttachAPIView.as_view(), name="odoo-instance-domain-attach"),
    path("odoo/instances/<int:instance_id>/domain/detach/", views.OdooInstanceDomainDetachAPIView.as_view(), name="odoo-instance-domain-detach"),
    path("odoo/instances/<int:instance_id>/domain/retry/", views.OdooInstanceDomainRetryAPIView.as_view(), name="odoo-instance-domain-retry"),
    path("odoo/instances/<int:instance_id>/delete/", views.OdooInstanceDeleteAPIView.as_view(), name="odoo-instance-delete"),
    path("infrastructure/", views.InfrastructureListAPIView.as_view(), name="infrastructure-list"),
    path("infrastructure/create/", views.InfrastructureCreateAPIView.as_view(), name="infrastructure-create"),
    path("infrastructure/<int:infrastructure_id>/delete/", views.InfrastructureDeleteAPIView.as_view(), name="infrastructure-delete"),
    # Phase 2: jobs, history, health, rollback
    path("jobs/", views.DeploymentJobListAPIView.as_view(), name="job-list"),
    path("jobs/<int:job_id>/cancel/", views.DeploymentJobCancelAPIView.as_view(), name="job-cancel"),
    path("odoo/servers/<int:server_id>/history/", views.OdooServerHistoryAPIView.as_view(), name="odoo-server-history"),
    path("odoo/instances/<int:instance_id>/history/", views.OdooInstanceHistoryAPIView.as_view(), name="odoo-instance-history"),
    path("odoo/instances/<int:instance_id>/rollback/", views.OdooInstanceRollbackAPIView.as_view(), name="odoo-instance-rollback"),
    path("odoo/instances/<int:instance_id>/health/", views.OdooInstanceHealthCheckView.as_view(), name="odoo-instance-health"),
    # SSH keys
    path("odoo/servers/<int:server_id>/ssh-keys/", views.ServerSSHKeyListCreateAPIView.as_view(), name="server-ssh-keys"),
    path("odoo/servers/<int:server_id>/ssh-keys/<int:key_id>/delete/", views.ServerSSHKeyDeleteAPIView.as_view(), name="server-ssh-key-delete"),
    # Instance maintenance
    path("odoo/instances/<int:instance_id>/commands/", views.OdooInstanceCommandsAPIView.as_view(), name="odoo-instance-commands"),
    path("odoo/instances/<int:instance_id>/maintenance/update-modules/", views.OdooInstanceUpdateModulesAPIView.as_view(), name="odoo-instance-update-modules"),
    path("odoo/instances/<int:instance_id>/maintenance/restart/", views.OdooInstanceRestartAPIView.as_view(), name="odoo-instance-restart"),
]
