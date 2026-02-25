from django.urls import path

from deployments import views

app_name = "deployments"

urlpatterns = [
    path("create/", views.DeploymentCreateView.as_view(), name="create-instance"),
    path("options/<int:account_id>/", views.CloudAccountOptionsAPIView.as_view(), name="account-options"),
    path("instances/<int:instance_id>/", views.InstanceDetailAPIView.as_view(), name="instance-detail"),
    path("runs/<int:run_id>/", views.TerraformRunDetailAPIView.as_view(), name="run-detail"),
    path("odoo/servers/", views.OdooServerListAPIView.as_view(), name="odoo-server-list"),
    path("odoo/servers/create/", views.OdooServerCreateAPIView.as_view(), name="odoo-server-create"),
    path("odoo/instances/", views.OdooInstanceListAPIView.as_view(), name="odoo-instance-list"),
    path("odoo/instances/create/", views.OdooInstanceCreateAPIView.as_view(), name="odoo-instance-create"),
]
