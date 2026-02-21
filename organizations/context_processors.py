from .models import OrganizationMembership


def organization(request):
    """
    Provides template context:
      current_org   — Organization | None
      current_role  — str | None
      user_orgs     — list of active memberships (for org switcher)
    """
    ctx = {
        "current_org": None,
        "current_role": None,
        "user_orgs": [],
    }
    if request.user.is_authenticated:
        ctx["current_org"] = getattr(request, "organization", None)
        ctx["current_role"] = getattr(request, "org_role", None)
        ctx["user_orgs"] = list(
            OrganizationMembership.objects
            .select_related("organization")
            .filter(user=request.user, is_active=True)
            .order_by("organization__name")
        )
    return ctx
