import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from .models import Plan, PaystackPayment, Subscription
from .paystack import PaystackClient, PaystackError, generate_reference, verify_webhook_signature
from .services import SubscriptionEnforcer

logger = logging.getLogger(__name__)


class SubscriptionRequiredView(TemplateView):
    """Shown when a subscription is inactive / expired / cancelled."""
    template_name = "subscriptions/required.html"

    def dispatch(self, request, *args, **kwargs):
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
        ctx["all_plans"] = Plan.objects.filter(is_active=True).order_by("price_monthly")
        ctx["paystack_public_key"] = getattr(settings, "PAYSTACK_PUBLIC_KEY", "")

        try:
            ctx["subscription"] = org.subscription
            ctx["plan"] = org.subscription.plan
        except Exception:
            ctx["subscription"] = None
            ctx["plan"] = None

        # Recent payment history (last 5)
        ctx["recent_payments"] = org.paystack_payments.select_related("plan").order_by("-created_at")[:5]
        return ctx


# ── Payment flow ──────────────────────────────────────────────────────────────

class InitiatePaymentView(LoginRequiredMixin, View):
    """
    POST /subscriptions/payment/initiate/<plan_id>/
    Creates a Paystack transaction and redirects the user to the hosted checkout.
    """

    def post(self, request, plan_id):
        org = getattr(request, "organization", None)
        if not org:
            messages.error(request, "No active organization found.")
            return redirect("organizations:select")

        plan = get_object_or_404(Plan, pk=plan_id, is_active=True)

        if plan.price_monthly == 0:
            messages.error(request, "This plan is free — no payment required.")
            return redirect("subscriptions:billing")

        reference = generate_reference()
        callback_url = f"{settings.SITE_URL.rstrip('/')}/subscriptions/payment/callback/"
        currency = getattr(settings, "PAYSTACK_CURRENCY", "NGN")

        # Determine payment type (upgrade vs initial)
        try:
            existing = org.subscription
            payment_type = (
                PaystackPayment.PaymentType.UPGRADE
                if existing.plan != plan
                else PaystackPayment.PaymentType.INITIAL
            )
        except Subscription.DoesNotExist:
            payment_type = PaystackPayment.PaymentType.INITIAL

        # Record a pending payment before redirecting
        PaystackPayment.objects.create(
            organization=org,
            plan=plan,
            reference=reference,
            amount=plan.price_monthly,
            currency=currency,
            status=PaystackPayment.Status.PENDING,
            payment_type=payment_type,
            metadata={"user_id": request.user.pk, "plan_id": plan.pk},
        )

        try:
            client = PaystackClient()
            result = client.initialize_transaction(
                email=request.user.email,
                amount_kobo=plan.price_in_kobo,
                reference=reference,
                callback_url=callback_url,
                plan_code=plan.paystack_plan_code or None,
                metadata={
                    "org_id": org.pk,
                    "org_name": org.name,
                    "plan_id": plan.pk,
                    "plan_name": plan.name,
                    "user_id": request.user.pk,
                },
            )
        except PaystackError as exc:
            logger.error("Paystack init failed for org=%s plan=%s: %s", org.pk, plan.pk, exc)
            PaystackPayment.objects.filter(reference=reference).update(
                status=PaystackPayment.Status.FAILED,
                gateway_response=str(exc),
            )
            messages.error(request, f"Payment initialization failed: {exc}")
            return redirect("subscriptions:billing")

        return redirect(result["authorization_url"])


class PaymentCallbackView(LoginRequiredMixin, View):
    """
    GET /subscriptions/payment/callback/?reference=DAFE-XXXX
    Verifies the Paystack transaction and activates/updates the subscription.
    """

    def get(self, request):
        reference = request.GET.get("reference", "").strip()
        if not reference:
            messages.error(request, "Missing payment reference.")
            return redirect("subscriptions:billing")

        try:
            payment = PaystackPayment.objects.select_related("organization", "plan").get(
                reference=reference
            )
        except PaystackPayment.DoesNotExist:
            messages.error(request, "Unknown payment reference.")
            return redirect("subscriptions:billing")

        # Guard: this payment must belong to the current org
        org = getattr(request, "organization", None)
        if not org or payment.organization_id != org.pk:
            messages.error(request, "Payment reference does not match your organization.")
            return redirect("subscriptions:billing")

        if payment.status == PaystackPayment.Status.SUCCESS:
            messages.info(request, "This payment has already been processed.")
            return redirect("subscriptions:billing")

        try:
            client = PaystackClient()
            tx = client.verify_transaction(reference)
        except PaystackError as exc:
            logger.error("Paystack verify failed ref=%s: %s", reference, exc)
            messages.error(request, f"Could not verify payment: {exc}")
            return redirect("subscriptions:billing")

        if tx.get("status") != "success":
            payment.status = PaystackPayment.Status.FAILED
            payment.gateway_response = tx.get("gateway_response", "")
            payment.save(update_fields=["status", "gateway_response"])
            messages.error(request, "Payment was not successful. Please try again.")
            return redirect("subscriptions:payment_failed")

        # --- Payment successful ---
        payment.status = PaystackPayment.Status.SUCCESS
        payment.paystack_id = str(tx.get("id", ""))
        payment.gateway_response = tx.get("gateway_response", "")
        payment.paid_at = timezone.now()
        payment.save(update_fields=["status", "paystack_id", "gateway_response", "paid_at"])

        _activate_subscription(org, payment.plan, tx)

        messages.success(request, f"Payment successful! You are now on the {payment.plan.name} plan.")
        return redirect("subscriptions:payment_success")


class CancelSubscriptionView(LoginRequiredMixin, View):
    """
    POST /subscriptions/cancel/
    Cancels the active Paystack subscription and marks it as CANCELLED.
    """

    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return redirect("organizations:select")

        try:
            sub = org.subscription
        except Subscription.DoesNotExist:
            messages.error(request, "No subscription found.")
            return redirect("subscriptions:billing")

        if sub.status not in (Subscription.Status.ACTIVE, Subscription.Status.PAST_DUE):
            messages.warning(request, "Your subscription is not currently active.")
            return redirect("subscriptions:billing")

        if sub.paystack_subscription_code and sub.paystack_email_token:
            try:
                client = PaystackClient()
                client.disable_subscription(sub.paystack_subscription_code, sub.paystack_email_token)
            except PaystackError as exc:
                logger.error("Paystack cancel failed org=%s: %s", org.pk, exc)
                messages.error(request, f"Could not cancel via Paystack: {exc}")
                return redirect("subscriptions:billing")

        sub.status = Subscription.Status.CANCELLED
        sub.auto_renew = False
        sub.save(update_fields=["status", "auto_renew"])

        messages.success(request, "Your subscription has been cancelled.")
        return redirect("subscriptions:billing")


# ── Webhook ───────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(View):
    """
    POST /subscriptions/webhook/paystack/
    Handles Paystack webhook events. Must return HTTP 200 quickly.
    """

    def post(self, request):
        payload_bytes = request.body
        signature = request.headers.get("X-Paystack-Signature", "")

        if not verify_webhook_signature(payload_bytes, signature):
            logger.warning("Paystack webhook: invalid signature")
            return HttpResponse(status=400)

        try:
            event = json.loads(payload_bytes)
        except json.JSONDecodeError:
            return HttpResponse(status=400)

        event_type = event.get("event", "")
        data = event.get("data", {})

        try:
            self._dispatch(event_type, data)
        except Exception as exc:
            # Log but always return 200 so Paystack doesn't retry forever
            logger.exception("Paystack webhook handler error event=%s: %s", event_type, exc)

        return HttpResponse(status=200)

    def _dispatch(self, event_type, data):
        if event_type == "charge.success":
            self._on_charge_success(data)
        elif event_type == "subscription.create":
            self._on_subscription_create(data)
        elif event_type == "subscription.disable":
            self._on_subscription_disable(data)
        elif event_type == "invoice.payment_failed":
            self._on_invoice_payment_failed(data)
        elif event_type in ("invoice.update", "invoice.create"):
            pass  # informational; charge.success is the authoritative event
        else:
            logger.debug("Paystack webhook: unhandled event %s", event_type)

    def _on_charge_success(self, data):
        reference = data.get("reference", "")
        try:
            payment = PaystackPayment.objects.select_related("organization", "plan").get(
                reference=reference
            )
        except PaystackPayment.DoesNotExist:
            # Renewal charge created by Paystack — find org via customer code
            customer_code = data.get("customer", {}).get("customer_code", "")
            if customer_code:
                try:
                    sub = Subscription.objects.select_related("plan", "organization").get(
                        paystack_customer_code=customer_code
                    )
                    _renew_subscription(sub, data)
                except Subscription.DoesNotExist:
                    logger.warning("charge.success: unknown customer_code=%s", customer_code)
            return

        if payment.status == PaystackPayment.Status.SUCCESS:
            return  # already processed via callback

        payment.status = PaystackPayment.Status.SUCCESS
        payment.paystack_id = str(data.get("id", ""))
        payment.gateway_response = data.get("gateway_response", "")
        payment.paid_at = timezone.now()
        payment.save(update_fields=["status", "paystack_id", "gateway_response", "paid_at"])

        _activate_subscription(payment.organization, payment.plan, data)

    def _on_subscription_create(self, data):
        """Store the Paystack subscription code against our Subscription."""
        customer_code = data.get("customer", {}).get("customer_code", "")
        sub_code = data.get("subscription_code", "")
        email_token = data.get("email_token", "")
        if not customer_code:
            return
        try:
            sub = Subscription.objects.get(paystack_customer_code=customer_code)
            update_fields = []
            if sub_code and not sub.paystack_subscription_code:
                sub.paystack_subscription_code = sub_code
                update_fields.append("paystack_subscription_code")
            if email_token and not sub.paystack_email_token:
                sub.paystack_email_token = email_token
                update_fields.append("paystack_email_token")
            if update_fields:
                sub.save(update_fields=update_fields)
        except Subscription.DoesNotExist:
            logger.warning("subscription.create: unknown customer_code=%s", customer_code)

    def _on_subscription_disable(self, data):
        sub_code = data.get("subscription_code", "")
        if not sub_code:
            return
        try:
            sub = Subscription.objects.get(paystack_subscription_code=sub_code)
            sub.status = Subscription.Status.CANCELLED
            sub.auto_renew = False
            sub.save(update_fields=["status", "auto_renew"])
            logger.info("Subscription %s cancelled via webhook", sub.pk)
        except Subscription.DoesNotExist:
            logger.warning("subscription.disable: unknown sub_code=%s", sub_code)

    def _on_invoice_payment_failed(self, data):
        sub_code = data.get("subscription", {}).get("subscription_code", "") if isinstance(
            data.get("subscription"), dict
        ) else ""
        if not sub_code:
            return
        try:
            sub = Subscription.objects.get(paystack_subscription_code=sub_code)
            if sub.status == Subscription.Status.ACTIVE:
                sub.status = Subscription.Status.PAST_DUE
                sub.save(update_fields=["status"])
                logger.info("Subscription %s marked PAST_DUE via webhook", sub.pk)
        except Subscription.DoesNotExist:
            logger.warning("invoice.payment_failed: unknown sub_code=%s", sub_code)


# ── Result pages ──────────────────────────────────────────────────────────────

class PaymentSuccessView(LoginRequiredMixin, TemplateView):
    template_name = "subscriptions/payment_success.html"


class PaymentFailedView(LoginRequiredMixin, TemplateView):
    template_name = "subscriptions/payment_failed.html"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _activate_subscription(org, plan, tx_data):
    """Activate or update an org's subscription after a successful payment."""
    now = timezone.now()
    # Subscription period: now → +30 days (Paystack webhooks renew automatically)
    period_end = now + timezone.timedelta(days=30)

    customer = tx_data.get("customer", {}) if isinstance(tx_data.get("customer"), dict) else {}
    customer_code = customer.get("customer_code", "")

    try:
        sub = org.subscription
        sub.plan = plan
        sub.status = Subscription.Status.ACTIVE
        sub.current_period_start = now
        sub.current_period_end = period_end
        sub.auto_renew = True
        if customer_code and not sub.paystack_customer_code:
            sub.paystack_customer_code = customer_code
        sub.save(update_fields=[
            "plan", "status", "current_period_start", "current_period_end",
            "auto_renew", "paystack_customer_code",
        ])
    except Subscription.DoesNotExist:
        Subscription.objects.create(
            organization=org,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            current_period_start=now,
            current_period_end=period_end,
            auto_renew=True,
            paystack_customer_code=customer_code,
        )

    logger.info("Subscription activated org=%s plan=%s", org.pk, plan.pk)


def _renew_subscription(sub, tx_data):
    """Extend subscription period on a successful renewal charge."""
    now = timezone.now()
    sub.status = Subscription.Status.ACTIVE
    sub.current_period_start = now
    sub.current_period_end = now + timezone.timedelta(days=30)
    sub.save(update_fields=["status", "current_period_start", "current_period_end"])
    logger.info("Subscription renewed sub=%s", sub.pk)
