from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from core.utils import get_client_ip, log_audit
from organizations.models import Organization, OrganizationInvite, OrganizationMembership

from .forms import InviteAcceptForm, OrgSignupForm, ProfileUpdateForm
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


class OrgSignupView(View):
    """
    Atomic: creates User + Organization + SUPER_ADMIN membership in one transaction.
    """
    template_name = "auth/register.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return render(request, self.template_name, {"form": OrgSignupForm()})

    def post(self, request):
        form = OrgSignupForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        data = form.cleaned_data
        try:
            with transaction.atomic():
                # 1. Create user
                user = User.objects.create_user(
                    email=data["email"],
                    password=data["password"],
                    first_name=data["first_name"],
                    last_name=data["last_name"],
                )
                # 2. Create organization
                org = Organization.objects.create(
                    name=data["org_name"],
                    owner=user,
                )
                # 3. Create SUPER_ADMIN membership
                OrganizationMembership.objects.create(
                    user=user,
                    organization=org,
                    role=OrganizationMembership.Role.SUPER_ADMIN,
                )
                # 4. Send verification email
                self._send_verification_email(request, user)
                # 5. Audit
                AuditLog.objects.create(
                    user=user,
                    organization=org,
                    action=AuditLog.Action.REGISTER,
                    ip_address=get_client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    description=f"Registered org '{org.name}' as SUPER_ADMIN",
                )
                AuditLog.objects.create(
                    user=user,
                    organization=org,
                    action=AuditLog.Action.ORG_CREATED,
                    ip_address=get_client_ip(request),
                    description=f"Organization '{org.name}' created",
                )
        except Exception as exc:
            messages.error(request, f"Registration failed: {exc}")
            return render(request, self.template_name, {"form": form})

        messages.success(
            request,
            "Account created! Check your console for the verification email.",
        )
        return redirect("users:login")

    @staticmethod
    def _send_verification_email(request, user):
        from django.core.mail import send_mail
        verify_url = (
            f"{settings.SITE_URL}/auth/verify-email/{user.email_verification_token}/"
        )
        send_mail(
            subject="Verify your DafeApp account",
            message=(
                f"Hi {user.get_short_name()},\n\n"
                f"Click to verify your email:\n{verify_url}\n\n"
                "This link is valid for 24 hours."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
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


class AcceptInviteView(View):
    """Handles invite acceptance for both existing and new users."""
    template_name = "auth/invite.html"

    def get(self, request, token):
        invite = get_object_or_404(OrganizationInvite, token=token)
        if not invite.is_valid:
            messages.error(request, "This invite has expired or already been used.")
            return redirect("users:login")
        ctx = {"invite": invite, "form": InviteAcceptForm()}
        # Already logged in and email matches → auto-accept
        if request.user.is_authenticated and request.user.email == invite.email:
            return self._accept(request, invite, request.user)
        return render(request, self.template_name, ctx)

    def post(self, request, token):
        invite = get_object_or_404(OrganizationInvite, token=token)
        if not invite.is_valid:
            messages.error(request, "This invite has expired or already been used.")
            return redirect("users:login")

        # Existing user logging in?
        existing = User.objects.filter(email=invite.email).first()
        if existing:
            return self._accept(request, invite, existing)

        # New user path
        form = InviteAcceptForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"invite": invite, "form": form})

        data = form.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(
                email=invite.email,
                password=data["password"],
                first_name=data["first_name"],
                last_name=data["last_name"],
                is_email_verified=True,  # invited = trusted email
            )
            membership = invite.accept(user)
            AuditLog.objects.create(
                user=user,
                organization=invite.organization,
                action=AuditLog.Action.INVITE_ACCEPTED,
                ip_address=get_client_ip(request),
                description=f"Accepted invite to {invite.organization.name} as {invite.role}",
            )

        login(request, user)
        messages.success(request, f"Welcome to {invite.organization.name}!")
        return redirect("core:dashboard")

    def _accept(self, request, invite, user):
        with transaction.atomic():
            invite.accept(user)
            AuditLog.objects.create(
                user=user,
                organization=invite.organization,
                action=AuditLog.Action.INVITE_ACCEPTED,
                ip_address=get_client_ip(request),
                description=f"Accepted invite to {invite.organization.name} as {invite.role}",
            )
        messages.success(request, f"Joined {invite.organization.name} as {invite.role}.")
        return redirect("core:dashboard")


# ─── REST API Views ──────────────────────────────────────────────────────────

class RegisterAPIView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class ProfileAPIView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class UserListAPIView(generics.ListAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAdminUser]
    queryset = User.objects.all().order_by("-date_joined")


class RoleUpdateAPIView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def patch(self, request, pk):
        from django.shortcuts import get_object_or_404
        user = get_object_or_404(User, pk=pk)
        serializer = RoleUpdateSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
