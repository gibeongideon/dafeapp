import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views import View

from dns.models import DomainAssignment, DnsProviderAccount, DnsRecord, DnsZone
from dns.serializers import (
    DomainAssignmentSerializer,
    DnsProviderAccountSerializer,
    DnsRecordSerializer,
    DnsZoneSerializer,
)
from dns.services.factory import get_dns_provider_service

logger = logging.getLogger(__name__)


def _request_data(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


def _org(request):
    return getattr(request, "organization", None)


def _can_manage_dns(request) -> bool:
    return getattr(request, "org_role", None) in ("SUPER_ADMIN", "ADMIN", "MANAGER")


def _sync_account_zones(account: DnsProviderAccount) -> list[DnsZone]:
    provider = get_dns_provider_service(account)
    payloads = provider.list_zones()
    synced_ids = []
    zones: list[DnsZone] = []
    now = timezone.now()

    for payload in payloads:
        name = (payload.get("name") or "").strip().lower()
        if not name:
            continue
        provider_zone_id = str(payload.get("id") or "").strip() or None
        zone, _ = DnsZone.objects.update_or_create(
            organization=account.organization,
            name=name,
            defaults={
                "provider_account": account,
                "provider_zone_id": provider_zone_id,
                "is_active": True,
                "last_synced_at": now,
            },
        )
        synced_ids.append(zone.id)
        zones.append(zone)

    stale_qs = DnsZone.objects.filter(provider_account=account)
    if synced_ids:
        stale_qs = stale_qs.exclude(id__in=synced_ids)
    stale_qs.update(is_active=False)
    return zones


class DnsProviderAccountListCreateAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        qs = DnsProviderAccount.objects.filter(organization=org).order_by("name", "id")
        return JsonResponse({"results": DnsProviderAccountSerializer(qs, many=True).data})

    def post(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        payload = _request_data(request)
        serializer = DnsProviderAccountSerializer(data=payload)
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)

        account = serializer.save(
            organization=org,
            created_by=request.user,
        )
        return JsonResponse(DnsProviderAccountSerializer(account).data, status=201)


class DnsProviderAccountDetailAPIView(LoginRequiredMixin, View):
    def _account(self, request, account_id):
        org = _org(request)
        if not org:
            return None, JsonResponse({"error": "No active organization."}, status=400)
        account = get_object_or_404(DnsProviderAccount, pk=account_id, organization=org)
        return account, None

    def get(self, request, account_id):
        account, error = self._account(request, account_id)
        if error:
            return error
        return JsonResponse(DnsProviderAccountSerializer(account).data)

    def post(self, request, account_id):
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)
        account, error = self._account(request, account_id)
        if error:
            return error
        serializer = DnsProviderAccountSerializer(account, data=_request_data(request), partial=True)
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)
        serializer.save()
        return JsonResponse(DnsProviderAccountSerializer(account).data)

    def delete(self, request, account_id):
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)
        account, error = self._account(request, account_id)
        if error:
            return error
        account.delete()
        return JsonResponse({"deleted": True})


class DnsProviderAccountVerifyAPIView(LoginRequiredMixin, View):
    def post(self, request, account_id):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        account = get_object_or_404(DnsProviderAccount, pk=account_id, organization=org)
        provider = get_dns_provider_service(account)
        try:
            provider.validate_credentials()
        except Exception as exc:
            account.is_verified = False
            account.verification_error = str(exc)
            account.last_verified_at = timezone.now()
            account.save(update_fields=["is_verified", "verification_error", "last_verified_at", "updated_at"])
            return JsonResponse(
                {
                    "ok": False,
                    "error": account.verification_error,
                    "account": DnsProviderAccountSerializer(account).data,
                },
                status=400,
            )

        account.is_verified = True
        account.verification_error = ""
        account.last_verified_at = timezone.now()
        account.save(update_fields=["is_verified", "verification_error", "last_verified_at", "updated_at"])
        return JsonResponse({"ok": True, "account": DnsProviderAccountSerializer(account).data})


class DnsProviderAccountSyncZonesAPIView(LoginRequiredMixin, View):
    def post(self, request, account_id):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        account = get_object_or_404(DnsProviderAccount, pk=account_id, organization=org)
        try:
            zones = _sync_account_zones(account)
        except Exception as exc:
            logger.warning("DNS zone sync failed for account %s", account.id, exc_info=True)
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "results": DnsZoneSerializer(zones, many=True).data,
            }
        )


class DnsZoneListCreateAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        qs = DnsZone.objects.filter(organization=org).select_related("provider_account").order_by("name", "id")
        return JsonResponse({"results": DnsZoneSerializer(qs, many=True).data})

    def post(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        serializer = DnsZoneSerializer(data=_request_data(request))
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)

        provider_account = serializer.validated_data["provider_account"]
        if provider_account.organization_id != org.id:
            return JsonResponse({"error": "Provider account does not belong to the active organization."}, status=400)

        zone = serializer.save(organization=org)
        return JsonResponse(DnsZoneSerializer(zone).data, status=201)


class DnsZoneDetailAPIView(LoginRequiredMixin, View):
    def _zone(self, request, zone_id):
        org = _org(request)
        if not org:
            return None, JsonResponse({"error": "No active organization."}, status=400)
        zone = get_object_or_404(DnsZone.objects.select_related("provider_account"), pk=zone_id, organization=org)
        return zone, None

    def get(self, request, zone_id):
        zone, error = self._zone(request, zone_id)
        if error:
            return error
        return JsonResponse(DnsZoneSerializer(zone).data)

    def post(self, request, zone_id):
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)
        zone, error = self._zone(request, zone_id)
        if error:
            return error
        serializer = DnsZoneSerializer(zone, data=_request_data(request), partial=True)
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)
        provider_account = serializer.validated_data.get("provider_account")
        if provider_account and provider_account.organization_id != zone.organization_id:
            return JsonResponse({"error": "Provider account does not belong to the active organization."}, status=400)
        serializer.save()
        return JsonResponse(DnsZoneSerializer(zone).data)

    def delete(self, request, zone_id):
        if not _can_manage_dns(request):
            return JsonResponse({"error": "Permission denied."}, status=403)
        zone, error = self._zone(request, zone_id)
        if error:
            return error
        zone.delete()
        return JsonResponse({"deleted": True})


class DnsRecordListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        qs = DnsRecord.objects.filter(organization=org).select_related("zone").order_by("-updated_at", "-id")
        zone_id = (request.GET.get("zone_id") or "").strip()
        if zone_id.isdigit():
            qs = qs.filter(zone_id=zone_id)
        status_value = (request.GET.get("status") or "").strip().upper()
        if status_value:
            qs = qs.filter(status=status_value)
        return JsonResponse({"results": DnsRecordSerializer(qs[:200], many=True).data})


class DnsRecordDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, record_id):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        record = get_object_or_404(DnsRecord.objects.select_related("zone"), pk=record_id, organization=org)
        return JsonResponse(DnsRecordSerializer(record).data)


class DomainAssignmentListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        qs = DomainAssignment.objects.filter(organization=org).select_related("zone", "instance").order_by("-updated_at", "-id")
        instance_id = (request.GET.get("instance_id") or "").strip()
        if instance_id.isdigit():
            qs = qs.filter(instance_id=instance_id)
        status_value = (request.GET.get("status") or "").strip().upper()
        if status_value:
            qs = qs.filter(status=status_value)
        return JsonResponse({"results": DomainAssignmentSerializer(qs[:200], many=True).data})


class DomainAssignmentDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, assignment_id):
        org = _org(request)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        assignment = get_object_or_404(
            DomainAssignment.objects.select_related("zone", "instance", "dns_record"),
            pk=assignment_id,
            organization=org,
        )
        return JsonResponse(DomainAssignmentSerializer(assignment).data)
