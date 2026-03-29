from django.urls import path

from dns import views

app_name = "dns"

urlpatterns = [
    path("accounts/", views.DnsProviderAccountListCreateAPIView.as_view(), name="provider-account-list"),
    path("accounts/<int:account_id>/", views.DnsProviderAccountDetailAPIView.as_view(), name="provider-account-detail"),
    path("accounts/<int:account_id>/verify/", views.DnsProviderAccountVerifyAPIView.as_view(), name="provider-account-verify"),
    path("accounts/<int:account_id>/sync-zones/", views.DnsProviderAccountSyncZonesAPIView.as_view(), name="provider-account-sync-zones"),
    path("zones/", views.DnsZoneListCreateAPIView.as_view(), name="zone-list"),
    path("zones/<int:zone_id>/", views.DnsZoneDetailAPIView.as_view(), name="zone-detail"),
    path("records/", views.DnsRecordListAPIView.as_view(), name="record-list"),
    path("records/<int:record_id>/", views.DnsRecordDetailAPIView.as_view(), name="record-detail"),
    path("assignments/", views.DomainAssignmentListAPIView.as_view(), name="assignment-list"),
    path("assignments/<int:assignment_id>/", views.DomainAssignmentDetailAPIView.as_view(), name="assignment-detail"),
]
