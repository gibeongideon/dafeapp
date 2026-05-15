from django.urls import path

from . import views

app_name = "subscriptions"

urlpatterns = [
    path("required/", views.SubscriptionRequiredView.as_view(), name="required"),
    path("billing/", views.BillingView.as_view(), name="billing"),

    # Payment flow
    path("payment/initiate/<int:plan_id>/", views.InitiatePaymentView.as_view(), name="initiate_payment"),
    path("payment/callback/", views.PaymentCallbackView.as_view(), name="payment_callback"),
    path("payment/success/", views.PaymentSuccessView.as_view(), name="payment_success"),
    path("payment/failed/", views.PaymentFailedView.as_view(), name="payment_failed"),
    path("cancel/", views.CancelSubscriptionView.as_view(), name="cancel"),

    # Paystack webhook (CSRF exempt, no auth required)
    path("webhook/paystack/", views.PaystackWebhookView.as_view(), name="paystack_webhook"),
]
