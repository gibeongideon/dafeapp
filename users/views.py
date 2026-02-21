from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from core.utils import log_audit

from .forms import RegisterForm
from .serializers import RegisterSerializer, RoleUpdateSerializer, UserSerializer

User = get_user_model()


# ─── Template Auth Views ────────────────────────────────────────────────────

class LoginView(BaseLoginView):
    template_name = "auth/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        return settings.LOGIN_REDIRECT_URL


class LogoutView(BaseLogoutView):
    next_page = settings.LOGOUT_REDIRECT_URL


class RegisterView(View):
    template_name = "auth/register.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return render(request, self.template_name, {"form": RegisterForm()})

    def post(self, request):
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            self._send_verification_email(request, user)
            log_audit(
                user, AuditLog.Action.REGISTER, request,
                description="New user registered",
            )
            messages.success(
                request,
                "Account created! Check your console for the verification email.",
            )
            return redirect("users:login")
        return render(request, self.template_name, {"form": form})

    @staticmethod
    def _send_verification_email(request, user):
        verify_url = (
            f"{settings.SITE_URL}/auth/verify-email/{user.email_verification_token}/"
        )
        send_mail(
            subject="Verify your DafeApp account",
            message=f"Hello {user.get_short_name()},\n\nClick to verify:\n{verify_url}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
        )


class VerifyEmailView(View):
    def get(self, request, token):
        user = get_object_or_404(User, email_verification_token=token)
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])
            log_audit(user, AuditLog.Action.EMAIL_VERIFY, request)
            messages.success(request, "Email verified! You can now log in.")
        else:
            messages.info(request, "Email already verified.")
        return redirect("users:login")


# ─── REST API Views ──────────────────────────────────────────────────────────

class RegisterAPIView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def perform_create(self, serializer):
        user = serializer.save()
        log_audit(user, AuditLog.Action.REGISTER, self.request, "API registration")


class ProfileAPIView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def perform_update(self, serializer):
        serializer.save()
        log_audit(
            self.request.user, AuditLog.Action.PROFILE_UPDATE,
            self.request, "Profile updated via API",
        )


class UserListAPIView(generics.ListAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAdminUser]
    queryset = User.objects.all().order_by("-date_joined")


class RoleUpdateAPIView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = RoleUpdateSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            log_audit(
                request.user, AuditLog.Action.USER_UPDATE, request,
                description=f"Role updated for {user.email}",
                metadata={"target_user": user.email, "new_role": serializer.data["role"]},
            )
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
