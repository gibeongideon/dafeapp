from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

urlpatterns = [
    path("admin/", admin.site.urls),

    # JWT auth
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/token/verify/", TokenVerifyView.as_view(), name="token_verify"),

    # App routes
    path("api/users/", include("users.urls", namespace="users")),
    path("api/subscriptions/", include("subscriptions.urls", namespace="subscriptions")),
    path("api/tenants/", include("tenants.urls", namespace="tenants")),
    path("api/cloud/", include("cloud.urls", namespace="cloud")),
    path("api/deployments/", include("deployments.urls", namespace="deployments")),
    path("api/dns/", include("dns.urls", namespace="dns")),
    path("api/backups/", include("backups.urls", namespace="backups")),
    path("api/monitoring/", include("monitoring.urls", namespace="monitoring")),
    path("api/audit/", include("audit.urls", namespace="audit")),
]
