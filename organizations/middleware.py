from .models import Organization, OrganizationMembership


class OrganizationMiddleware:
    """
    Attaches `request.organization` and `request.org_role` based on the
    session-selected org. Falls back to the user's first active membership.
    Runs after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.org_role = None
        request.platform_view_as_org = False

        if request.user.is_authenticated:
            self._resolve_org(request)

        return self.get_response(request)

    def _resolve_org(self, request):
        org_id = request.session.get("current_org_id")
        if request.user.is_platform_admin:
            view_as_org_id = request.session.get("platform_view_as_org_id")
            if view_as_org_id:
                org = Organization.objects.filter(pk=view_as_org_id).first()
                if org:
                    request.organization = org
                    request.org_role = "SUPER_ADMIN"
                    request.platform_view_as_org = True
                    request.session["current_org_id"] = org.pk
                    return
                request.session.pop("platform_view_as_org_id", None)

        membership = None

        if org_id:
            membership = (
                OrganizationMembership.objects
                .select_related("organization")
                .filter(user=request.user, organization_id=org_id, is_active=True)
                .first()
            )
            if not membership:
                # Stale session — clear it
                request.session.pop("current_org_id", None)

        if not membership:
            # Fall back to first active membership
            membership = (
                OrganizationMembership.objects
                .select_related("organization")
                .filter(user=request.user, is_active=True)
                .first()
            )
            if membership:
                request.session["current_org_id"] = membership.organization_id

        if membership:
            request.organization = membership.organization
            request.org_role = membership.role
