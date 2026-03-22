from django.urls import path

from cloud import views

app_name = "cloud"

urlpatterns = [
    # Dashboard
    path("", views.CloudDashboardView.as_view(), name="dashboard"),
    path("ssh-settings/", views.PyOSSSHSettingsView.as_view(), name="ssh-settings"),

    # PYOS servers
    path("servers/add/", views.AddExternalServerView.as_view(), name="add-server"),
    path("servers/<int:pk>/", views.ServerDetailView.as_view(), name="server-detail"),
    path("servers/<int:pk>/verify/", views.VerifyServerView.as_view(), name="verify-server"),
    path("servers/<int:pk>/prepare/", views.PrepareServerView.as_view(), name="prepare-server"),

    # Cloud accounts (DO)
    path("accounts/add/", views.AddCloudAccountView.as_view(), name="add-account"),
    path("accounts/<int:pk>/verify/", views.VerifyAccountView.as_view(), name="verify-account"),
    path("accounts/<int:pk>/options/", views.CloudAccountOptionsView.as_view(), name="account-options"),

    # Droplets
    path("droplets/provision/", views.ProvisionDropletView.as_view(), name="provision-droplet"),
    path("droplets/<int:pk>/destroy/", views.DestroyDropletView.as_view(), name="destroy-droplet"),

    # DafeApp SSH public key (for DAFEAPP_KEY auth)
    path("ssh-key/", views.DafeAppPublicKeyView.as_view(), name="dafeapp-ssh-key"),
]
