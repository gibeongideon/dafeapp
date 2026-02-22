from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from users.urls import auth_urlpatterns

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),

    # Template auth
    path("auth/", include((auth_urlpatterns, "users"))),

    # Dashboard UI
    path("dashboard/", include("core.urls", namespace="core")),

    # Organization management
    path("orgs/", include("organizations.urls", namespace="organizations")),

    # Subscription UI (required page + billing dashboard)
    path("subscriptions/", include("subscriptions.urls", namespace="subscriptions")),

    # JWT
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/token/verify/", TokenVerifyView.as_view(), name="token_verify"),

    # REST API
    path("api/users/", include("users.urls")),
    # subscriptions REST API endpoints will be added in a future phase
    path("api/tenants/", include("tenants.urls", namespace="tenants")),
    path("cloud/", include("cloud.urls", namespace="cloud")),
    path("api/deployments/", include("deployments.urls", namespace="deployments")),
    path("api/dns/", include("dns.urls", namespace="dns")),
    path("api/backups/", include("backups.urls", namespace="backups")),
    path("api/monitoring/", include("monitoring.urls", namespace="monitoring")),
    path("api/audit/", include("audit.urls", namespace="audit")),
]
