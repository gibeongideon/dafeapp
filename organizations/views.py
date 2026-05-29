from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from datetime import timedelta

from audit.models import AuditLog
from core.utils import get_client_ip

from .decorators import organization_required, organization_role_required
from .forms import CreateOrganizationForm, InviteUserForm, MemberRoleForm
from .models import Organization, OrganizationInvite, OrganizationMembership
from .permissions import has_org_permission

User = get_user_model()


@login_required
def select_org(request):
    """Let user pick which org to work in (when they belong to multiple)."""
    memberships = OrganizationMembership.objects.select_related("organization").filter(
        user=request.user, is_active=True
    )
    if memberships.count() == 1:
        m = memberships.first()
        request.session["current_org_id"] = m.organization_id
        return redirect("core:dashboard")
    return render(request, "organizations/select.html", {"memberships": memberships})


@login_required
def switch_org(request, org_id):
    """Switch active organization stored in session."""
    if OrganizationMembership.objects.filter(
        user=request.user, organization_id=org_id, is_active=True
    ).exists():
        request.session["current_org_id"] = org_id
    return redirect(request.META.get("HTTP_REFERER", "core:dashboard"))


@login_required
def create_org(request):
    """Create a new organization and make the current user its SUPER_ADMIN."""
    form = CreateOrganizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            org = form.save(commit=False)
            org.owner = request.user
            org.save()

            OrganizationMembership.objects.create(
                user=request.user,
                organization=org,
                role=OrganizationMembership.Role.SUPER_ADMIN,
                is_active=True,
            )

            # Create a TRIAL subscription so the org is serviceable immediately
            from subscriptions.models import Plan, Subscription
            plan = Plan.objects.filter(plan_type="STARTER").first() or Plan.objects.first()
            if plan:
                Subscription.objects.get_or_create(
                    organization=org,
                    defaults={"plan": plan, "status": "TRIAL"},
                )

            AuditLog.objects.create(
                user=request.user,
                organization=org,
                action=AuditLog.Action.ORG_CREATED,
                ip_address=get_client_ip(request),
                description=f"Created organization '{org.name}'",
            )

        request.session["current_org_id"] = org.id
        messages.success(request, f"Organization '{org.name}' created.")
        return redirect("core:dashboard")

    return render(request, "organizations/create.html", {"form": form})


@organization_role_required(["SUPER_ADMIN", "ADMIN"])
def members(request):
    """List org members + invite form."""
    org = request.organization
    member_list = (
        OrganizationMembership.objects
        .select_related("user", "invited_by")
        .filter(organization=org)
        .order_by("role", "joined_at")
    )
    invite_form = InviteUserForm(current_role=request.org_role)

    if request.method == "POST" and "invite" in request.POST:
        return _handle_invite(request, org, member_list)

    return render(request, "dashboard/users.html", {
        "members": member_list,
        "invite_form": invite_form,
    })


def _handle_invite(request, org, member_list):
    if not has_org_permission(request.user, org, "invite_user"):
        return HttpResponse("Forbidden", status=403)

    form = InviteUserForm(request.POST, current_role=request.org_role)
    if not form.is_valid():
        return render(request, "dashboard/users.html", {
            "members": member_list,
            "invite_form": form,
        })

    email = form.cleaned_data["email"].lower()
    role = form.cleaned_data["role"]

    # Re-use existing invite or create new one
    invite, created = OrganizationInvite.objects.update_or_create(
        email=email,
        organization=org,
        defaults={
            "role": role,
            "is_used": False,
            "created_by": request.user,
            "expires_at": timezone.now() + timedelta(days=7),
        },
    )

    # Send email (console in dev)
    from django.conf import settings
    from django.core.mail import send_mail
    invite_url = f"{settings.SITE_URL}/auth/invite/{invite.token}/"
    send_mail(
        subject=f"You're invited to {org.name} on DafeApp",
        message=(
            f"You've been invited to join {org.name} as {role}.\n\n"
            f"Accept here: {invite_url}\n\nExpires in 7 days."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=True,
    )

    AuditLog.objects.create(
        user=request.user,
        organization=org,
        action=AuditLog.Action.INVITE_SENT,
        ip_address=get_client_ip(request),
        description=f"Invited {email} as {role}",
        metadata={"email": email, "role": role},
    )

    if request.headers.get("HX-Request"):
        return HttpResponse(
            f'<p class="text-green-600 font-medium">✓ Invite sent to {email}</p>'
        )
    messages.success(request, f"Invite sent to {email}.")
    return redirect("core:users")


@organization_role_required(["SUPER_ADMIN"])
def change_member_role(request, membership_id):
    """SUPER_ADMIN only: change a member's role."""
    membership = get_object_or_404(
        OrganizationMembership,
        pk=membership_id,
        organization=request.organization,
    )
    if request.method == "POST":
        form = MemberRoleForm(request.POST, instance=membership)
        if form.is_valid():
            old_role = membership.role
            form.save()
            AuditLog.objects.create(
                user=request.user,
                organization=request.organization,
                action=AuditLog.Action.ROLE_CHANGE,
                ip_address=get_client_ip(request),
                description=f"Role changed for {membership.user.email}: {old_role} → {membership.role}",
            )
            if request.headers.get("HX-Request"):
                return HttpResponse(
                    f'<span class="text-green-600 text-xs font-medium">✓ Role updated</span>'
                )
            messages.success(request, "Role updated.")
    return redirect("core:users")


@organization_role_required(["SUPER_ADMIN", "ADMIN"])
def toggle_member(request, membership_id):
    """Enable or disable a membership (not the user account itself)."""
    membership = get_object_or_404(
        OrganizationMembership,
        pk=membership_id,
        organization=request.organization,
    )
    # ADMINs cannot disable SUPER_ADMINs
    if (
        request.org_role == "ADMIN"
        and membership.role == "SUPER_ADMIN"
    ):
        return HttpResponse("Forbidden", status=403)

    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active"])
    action = "enabled" if membership.is_active else "disabled"
    AuditLog.objects.create(
        user=request.user,
        organization=request.organization,
        action=AuditLog.Action.USER_UPDATE,
        ip_address=get_client_ip(request),
        description=f"Member {membership.user.email} {action}",
    )
    if request.headers.get("HX-Request"):
        label = "Disable" if membership.is_active else "Enable"
        css = "text-yellow-600" if membership.is_active else "text-green-600"
        return HttpResponse(
            f'<span class="text-xs {css} font-medium">✓ {action.title()}</span>'
        )
    messages.success(request, f"Member {action}.")
    return redirect("core:users")


@organization_role_required(["SUPER_ADMIN"])
def remove_member(request, membership_id):
    """SUPER_ADMIN only: permanently remove a member from the org."""
    membership = get_object_or_404(
        OrganizationMembership,
        pk=membership_id,
        organization=request.organization,
    )
    if request.method == "POST":
        email = membership.user.email
        membership.delete()
        AuditLog.objects.create(
            user=request.user,
            organization=request.organization,
            action=AuditLog.Action.USER_DELETE,
            ip_address=get_client_ip(request),
            description=f"Removed {email} from org",
        )
        messages.success(request, f"{email} removed from organization.")
    return redirect("core:users")
