from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "users_api"

# Template auth routes (included at /auth/)
auth_urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("register/", views.OrgSignupView.as_view(), name="register"),
    path("verify-email/<uuid:token>/", views.VerifyEmailView.as_view(), name="verify-email"),
    path("invite/<uuid:token>/", views.AcceptInviteView.as_view(), name="accept-invite"),
    # VCS account management
    path("vcs/<int:pk>/disconnect/", views.VCSDisconnectView.as_view(), name="vcs-disconnect"),
    # Password reset
    path("password-reset/", views.CustomPasswordResetView.as_view(), name="password-reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(template_name="auth/password_reset_done.html"), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", views.CustomPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(template_name="auth/password_reset_complete.html"), name="password_reset_complete"),
]

# API routes (included at /api/users/)
urlpatterns = [
    path("register/", views.RegisterAPIView.as_view(), name="api-register"),
    path("me/", views.ProfileAPIView.as_view(), name="api-profile"),
    path("", views.UserListAPIView.as_view(), name="api-user-list"),
    path("<int:pk>/role/", views.RoleUpdateAPIView.as_view(), name="api-role-update"),
]
