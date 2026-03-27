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
    path("odoo/instances/<int:instance_id>/repos/", views.OdooInstanceGitRepoListAPIView.as_view(), name="odoo-instance-repo-list"),
    path("odoo/instances/create/", views.OdooInstanceCreateAPIView.as_view(), name="odoo-instance-create"),
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
]
