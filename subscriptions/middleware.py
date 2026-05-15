from django.shortcuts import redirect

from .models import Subscription
from .services import SubscriptionEnforcer

# Paths that bypass subscription enforcement entirely.
_EXEMPT_PREFIXES = (
    "/admin/",
    "/auth/",
    "/accounts/",
    "/orgs/",
    "/api/token/",
    "/subscriptions/required/",
    "/subscriptions/payment/",
    "/subscriptions/webhook/",
    "/subscriptions/cancel/",
    "/static/",
    "/media/",
)


class SubscriptionMiddleware:
    """
    Attaches a SubscriptionEnforcer to request.subscription_enforcer.

    Hard-redirects to subscriptions:required when the subscription is
    not serviceable AND status is not SUSPENDED (suspended users keep
    dashboard access but are blocked at the service-layer by ensure_active).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not getattr(request.user, "is_platform_admin", False)
            and getattr(request, "organization", None) is not None
            and not self._is_exempt(request.path)
        ):
            try:
                sub = request.organization.subscription
                enforcer = SubscriptionEnforcer(request.organization)
                request.subscription_enforcer = enforcer

                # Hard-block non-serviceable subscriptions except SUSPENDED
                # (SUSPENDED can view the dashboard; provisioning views call ensure_active)
                if not sub.is_serviceable and sub.status != Subscription.Status.SUSPENDED:
                    return redirect("subscriptions:required")

            except Subscription.DoesNotExist:
                # No subscription yet — pass through; signal should have created one
                pass

        return self.get_response(request)

    @staticmethod
    def _is_exempt(path):
        return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)
