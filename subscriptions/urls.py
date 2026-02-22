from django.urls import path

from . import views

app_name = "subscriptions"

urlpatterns = [
    path("required/", views.SubscriptionRequiredView.as_view(), name="required"),
    path("billing/", views.BillingView.as_view(), name="billing"),
]
