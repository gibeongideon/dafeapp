"""
Subscription enforcement test suite — 12 cases covering all plan limits,
status transitions, grace period, and upgrade/downgrade safety.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from deployments.models import Instance
from organizations.models import Organization, OrganizationMembership
from subscriptions.exceptions import SubscriptionError, SubscriptionLimitError
from subscriptions.models import Plan, Subscription, UsageRecord
from subscriptions.services import SubscriptionEnforcer

User = get_user_model()


def make_plan(**kwargs):
    defaults = dict(
        name="Test Starter",
        plan_type=Plan.PlanType.STARTER,
        price_monthly="0.00",
        max_instances=1,
        max_backups_per_month=5,
        staging_enabled=False,
        version_upgrade_enabled=False,
    )
    defaults.update(kwargs)
    return Plan.objects.create(**defaults)


def make_subscription(org, plan, status=Subscription.Status.ACTIVE, days_ahead=30):
    now = timezone.now()
    return Subscription.objects.create(
        organization=org,
        plan=plan,
        status=status,
        current_period_start=now,
        current_period_end=now + timedelta(days=days_ahead),
    )


class EnforcementTestCase(TestCase):
    """Base class: creates one org + super-admin user per test."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="owner@test.com", password="pass")
        cls.org = Organization.objects.create(name="Test Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user, organization=cls.org, role="SUPER_ADMIN"
        )
        cls.starter = make_plan(name="Starter", plan_type=Plan.PlanType.STARTER)
        cls.growth = make_plan(
            name="Growth",
            plan_type=Plan.PlanType.GROWTH,
            price_monthly="49.00",
            max_instances=3,
            max_backups_per_month=30,
            staging_enabled=True,
            version_upgrade_enabled=True,
        )
        cls.enterprise = make_plan(
            name="Enterprise",
            plan_type=Plan.PlanType.ENTERPRISE,
            price_monthly="199.00",
            max_instances=None,
            max_backups_per_month=None,
            staging_enabled=True,
            version_upgrade_enabled=True,
        )

    def _enforcer(self, plan=None, status=Subscription.Status.ACTIVE, days_ahead=30):
        """Helper: create/replace subscription and return a fresh enforcer."""
        Subscription.objects.filter(organization=self.org).delete()
        make_subscription(self.org, plan or self.starter, status=status, days_ahead=days_ahead)
        # Reload org to clear cached relation
        org = Organization.objects.get(pk=self.org.pk)
        return SubscriptionEnforcer(org)


# ── 1. Instance limit ────────────────────────────────────────────────────────

class InstanceLimitTests(EnforcementTestCase):

    def test_cannot_create_instance_beyond_plan_limit(self):
        """Starter allows 1 instance; a second one must be blocked."""
        enforcer = self._enforcer(self.starter)
        Instance.objects.create(
            organization=self.org, name="odoo-1", status=Instance.Status.RUNNING
        )
        with self.assertRaises(SubscriptionLimitError):
            enforcer.check_instance_limit()

    def test_can_create_instance_within_limit(self):
        """No instances yet → limit check must pass."""
        enforcer = self._enforcer(self.starter)
        Instance.objects.filter(organization=self.org).delete()
        enforcer.check_instance_limit()  # must not raise

    def test_deleted_instances_do_not_count_toward_limit(self):
        """DELETED instances are excluded from the live count."""
        enforcer = self._enforcer(self.starter)
        Instance.objects.create(
            organization=self.org, name="old-odoo", status=Instance.Status.DELETED
        )
        enforcer.check_instance_limit()  # must not raise (deleted doesn't count)

    def test_enterprise_unlimited_plan_never_blocked(self):
        """Enterprise has null max_instances → never raises SubscriptionLimitError."""
        enforcer = self._enforcer(self.enterprise)
        for i in range(50):
            Instance.objects.create(
                organization=self.org, name=f"odoo-{i}", status=Instance.Status.RUNNING
            )
        enforcer.check_instance_limit()  # must not raise


# ── 2. Backup limit ──────────────────────────────────────────────────────────

class BackupLimitTests(EnforcementTestCase):

    def test_cannot_backup_beyond_monthly_limit(self):
        """Starter allows 5 backups/month; 6th must be blocked."""
        enforcer = self._enforcer(self.starter)
        now = timezone.now()
        UsageRecord.objects.bulk_create([
            UsageRecord(
                organization=self.org,
                usage_type=UsageRecord.UsageType.BACKUP,
                timestamp=now,
            )
            for _ in range(5)
        ])
        with self.assertRaises(SubscriptionLimitError):
            enforcer.check_backup_limit()

    def test_can_backup_within_limit(self):
        """4 backups on Starter (limit 5) must pass."""
        enforcer = self._enforcer(self.starter)
        UsageRecord.objects.filter(organization=self.org).delete()
        UsageRecord.objects.bulk_create([
            UsageRecord(
                organization=self.org,
                usage_type=UsageRecord.UsageType.BACKUP,
                timestamp=timezone.now(),
            )
            for _ in range(4)
        ])
        enforcer.check_backup_limit()  # must not raise


# ── 3. Feature flags ─────────────────────────────────────────────────────────

class FeatureFlagTests(EnforcementTestCase):

    def test_cannot_create_staging_on_starter(self):
        """Staging is disabled on Starter → raises SubscriptionLimitError."""
        enforcer = self._enforcer(self.starter)
        with self.assertRaises(SubscriptionLimitError):
            enforcer.check_staging_allowed()

    def test_can_create_staging_on_growth(self):
        """Staging is enabled on Growth → must not raise."""
        enforcer = self._enforcer(self.growth)
        enforcer.check_staging_allowed()  # must not raise

    def test_cannot_upgrade_on_starter(self):
        """Version upgrades are disabled on Starter → raises SubscriptionLimitError."""
        enforcer = self._enforcer(self.starter)
        with self.assertRaises(SubscriptionLimitError):
            enforcer.check_upgrade_allowed()

    def test_can_upgrade_on_enterprise(self):
        """Version upgrades are enabled on Enterprise → must not raise."""
        enforcer = self._enforcer(self.enterprise)
        enforcer.check_upgrade_allowed()  # must not raise


# ── 4. Subscription status ───────────────────────────────────────────────────

class SubscriptionStatusTests(EnforcementTestCase):

    def test_cancelled_subscription_blocks_ensure_active(self):
        enforcer = self._enforcer(self.starter, status=Subscription.Status.CANCELLED)
        with self.assertRaises(SubscriptionError):
            enforcer.ensure_active()

    def test_suspended_subscription_blocks_ensure_active(self):
        enforcer = self._enforcer(self.starter, status=Subscription.Status.SUSPENDED)
        with self.assertRaises(SubscriptionError):
            enforcer.ensure_active()

    def test_expired_trial_blocks_ensure_active(self):
        """A TRIAL whose period ended yesterday must be blocked."""
        enforcer = self._enforcer(self.starter, status=Subscription.Status.TRIAL, days_ahead=-1)
        with self.assertRaises(SubscriptionError):
            enforcer.ensure_active()

    def test_active_trial_allows_ensure_active(self):
        """A TRIAL that has not expired must pass."""
        enforcer = self._enforcer(self.starter, status=Subscription.Status.TRIAL, days_ahead=7)
        enforcer.ensure_active()  # must not raise

    def test_past_due_within_grace_allows_ensure_active(self):
        """PAST_DUE within 3-day grace window must still be serviceable."""
        Subscription.objects.filter(organization=self.org).delete()
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.starter,
            status=Subscription.Status.PAST_DUE,
            current_period_start=now - timedelta(days=31),
            current_period_end=now - timedelta(hours=12),  # expired 12 h ago → in grace
        )
        org = Organization.objects.get(pk=self.org.pk)
        enforcer = SubscriptionEnforcer(org)
        enforcer.ensure_active()  # must not raise (within 3-day grace)

    def test_past_due_beyond_grace_blocks_ensure_active(self):
        """PAST_DUE beyond the 3-day grace window must be blocked."""
        Subscription.objects.filter(organization=self.org).delete()
        now = timezone.now()
        Subscription.objects.create(
            organization=self.org,
            plan=self.starter,
            status=Subscription.Status.PAST_DUE,
            current_period_start=now - timedelta(days=35),
            current_period_end=now - timedelta(days=4),  # expired 4 days ago → beyond grace
        )
        org = Organization.objects.get(pk=self.org.pk)
        enforcer = SubscriptionEnforcer(org)
        with self.assertRaises(SubscriptionError):
            enforcer.ensure_active()


# ── 5. Plan upgrade / downgrade safety ───────────────────────────────────────

class PlanUpgradeTests(EnforcementTestCase):

    def test_after_plan_upgrade_can_create_more_instances(self):
        """
        User has 1 instance on Starter (at limit). After upgrading to Growth
        (limit 3) the check must pass.
        """
        # Start on Starter with 1 instance
        Subscription.objects.filter(organization=self.org).delete()
        make_subscription(self.org, self.starter)
        Instance.objects.filter(organization=self.org).delete()
        Instance.objects.create(
            organization=self.org, name="odoo-1", status=Instance.Status.RUNNING
        )

        # Upgrade to Growth
        Subscription.objects.filter(organization=self.org).update(plan=self.growth)
        org = Organization.objects.get(pk=self.org.pk)
        enforcer = SubscriptionEnforcer(org)
        enforcer.check_instance_limit()  # 1 of 3 → must not raise

    def test_downgrade_does_not_delete_existing_instances(self):
        """
        Downgrading from Growth → Starter leaves existing instances intact
        but blocks NEW instance creation.
        """
        Subscription.objects.filter(organization=self.org).delete()
        make_subscription(self.org, self.growth)
        Instance.objects.filter(organization=self.org).delete()
        # Create 2 instances (within Growth limit)
        Instance.objects.create(
            organization=self.org, name="odoo-1", status=Instance.Status.RUNNING
        )
        Instance.objects.create(
            organization=self.org, name="odoo-2", status=Instance.Status.RUNNING
        )
        # Downgrade to Starter
        Subscription.objects.filter(organization=self.org).update(plan=self.starter)
        org = Organization.objects.get(pk=self.org.pk)
        enforcer = SubscriptionEnforcer(org)

        # Both old instances still exist
        self.assertEqual(enforcer.current_instance_count(), 2)

        # Creating a third is blocked
        with self.assertRaises(SubscriptionLimitError):
            enforcer.check_instance_limit()
