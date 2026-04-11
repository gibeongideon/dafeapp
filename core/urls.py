from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.DashboardHomeView.as_view(), name="dashboard"),
    path("catalog/", views.CatalogView.as_view(), name="catalog"),
    path("connections/", views.ConnectionsView.as_view(), name="connections"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserManagementView.as_view(), name="users"),
    path("audit/", views.AuditLogView.as_view(), name="audit"),
    path("vcs/", views.VCSManagementView.as_view(), name="vcs"),
    path("docs/installation/", views.InstallationDocsView.as_view(), name="docs-installation"),
]
