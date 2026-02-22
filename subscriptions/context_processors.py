from .models import Subscription


def subscription(request):
    """
    Provides subscription context to every template:
        subscription       — the Subscription object (or None)
        plan               — the Plan object (or None)
        plan_limits        — dict from SubscriptionEnforcer.plan_limits
        subscription_enforcer — the SubscriptionEnforcer attached by middleware
    """
    if not request.user.is_authenticated:
        return {}

    org = getattr(request, "organization", None)
    if org is None:
        return {}

    try:
        sub = org.subscription
        plan = sub.plan
    except Subscription.DoesNotExist:
        return {"subscription": None, "plan": None, "plan_limits": {}}

    enforcer = getattr(request, "subscription_enforcer", None)
    plan_limits = enforcer.plan_limits if enforcer else {}

    return {
        "subscription": sub,
        "plan": plan,
        "plan_limits": plan_limits,
    }
