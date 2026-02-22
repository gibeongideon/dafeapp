"""
Auto-create a 14-day TRIAL subscription on the STARTER plan whenever a
new Organization is created.  Fails silently if no plan has been seeded yet
(first install before `python manage.py seed_plans` is run).
"""

from datetime import timedelta

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender="organizations.Organization")
def auto_create_trial_subscription(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from subscriptions.models import Plan, Subscription

        starter = Plan.objects.filter(plan_type=Plan.PlanType.STARTER, is_active=True).first()
        if starter is None:
            return  # plans not yet seeded — skip silently

        now = timezone.now()
        Subscription.objects.create(
            organization=instance,
            plan=starter,
            status=Subscription.Status.TRIAL,
            current_period_start=now,
            current_period_end=now + timedelta(days=14),
        )
    except Exception:
        pass  # never break org creation
