from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .models import Subscription
from .services import SubscriptionEnforcer


class SubscriptionRequiredView(TemplateView):
    """Shown when a subscription is inactive / expired / cancelled."""
    template_name = "subscriptions/required.html"

    def dispatch(self, request, *args, **kwargs):
        # Already has an active subscription → send back to dashboard
        if request.user.is_authenticated:
            org = getattr(request, "organization", None)
            if org:
                try:
                    sub = org.subscription
                    if sub.is_serviceable or sub.status == Subscription.Status.SUSPENDED:
                        return redirect("core:dashboard")
                except Subscription.DoesNotExist:
                    pass
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = getattr(self.request, "organization", None)
        if org:
            try:
                ctx["subscription"] = org.subscription
            except Subscription.DoesNotExist:
                ctx["subscription"] = None
        return ctx


class BillingView(LoginRequiredMixin, TemplateView):
    """Dashboard billing & plan overview page."""
    template_name = "dashboard/billing.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not getattr(request, "organization", None):
            return redirect("organizations:select")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        enforcer = getattr(self.request, "subscription_enforcer", None)
        if enforcer is None:
            enforcer = SubscriptionEnforcer(org)

        ctx["enforcer"] = enforcer
        ctx["plan_limits"] = enforcer.plan_limits
        ctx["all_plans"] = __import__("subscriptions.models", fromlist=["Plan"]).Plan.objects.filter(is_active=True).order_by("price_monthly")

        try:
            ctx["subscription"] = org.subscription
            ctx["plan"] = org.subscription.plan
        except Exception:
            ctx["subscription"] = None
            ctx["plan"] = None
        return ctx
