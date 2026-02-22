"""
python manage.py seed_plans

Seeds (or updates) the three canonical subscription plans:
  STARTER    — free tier, 1 instance, 5 backups/month, no staging/upgrade
  GROWTH     — $49/mo, 3 instances, 30 backups/month, staging + upgrade
  ENTERPRISE — $199/mo, unlimited instances and backups, all features
"""

from django.core.management.base import BaseCommand

from subscriptions.models import Plan

PLANS = [
    dict(
        plan_type=Plan.PlanType.STARTER,
        name="Starter",
        price_monthly="0.00",
        max_instances=1,
        max_backups_per_month=5,
        staging_enabled=False,
        version_upgrade_enabled=False,
    ),
    dict(
        plan_type=Plan.PlanType.GROWTH,
        name="Growth",
        price_monthly="49.00",
        max_instances=3,
        max_backups_per_month=30,
        staging_enabled=True,
        version_upgrade_enabled=True,
    ),
    dict(
        plan_type=Plan.PlanType.ENTERPRISE,
        name="Enterprise",
        price_monthly="199.00",
        max_instances=None,        # unlimited
        max_backups_per_month=None,  # unlimited
        staging_enabled=True,
        version_upgrade_enabled=True,
    ),
]


class Command(BaseCommand):
    help = "Seed the three canonical subscription plans (idempotent)."

    def handle(self, *args, **options):
        for data in PLANS:
            plan_type = data.pop("plan_type")
            obj, created = Plan.objects.update_or_create(
                plan_type=plan_type,
                defaults={"plan_type": plan_type, **data},
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb}: {obj.name} (${obj.price_monthly}/mo)")
            )
        self.stdout.write(self.style.SUCCESS("Done. Plans are ready."))
