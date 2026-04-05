# Generated manually for DNS foundation models.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0003_add_first_name_to_invite"),
    ]

    operations = [
        migrations.CreateModel(
            name="DnsProviderAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("provider", models.CharField(choices=[("CLOUDFLARE", "Cloudflare")], default="CLOUDFLARE", max_length=20)),
                ("encrypted_api_token", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("is_verified", models.BooleanField(default=False)),
                ("verification_error", models.CharField(blank=True, default="", max_length=500)),
                ("last_verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_dns_provider_accounts", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dns_provider_accounts", to="organizations.organization")),
            ],
            options={
                "ordering": ["name", "id"],
                "indexes": [
                    models.Index(fields=["organization", "provider"], name="dns_provider_org_type_idx"),
                    models.Index(fields=["organization", "is_active"], name="dns_provider_org_active_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "name"), name="dns_provider_account_org_name_uniq"),
                ],
            },
        ),
        migrations.CreateModel(
            name="DnsZone",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("provider_zone_id", models.CharField(blank=True, max_length=255, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("default_proxied", models.BooleanField(default=False)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dns_zones", to="organizations.organization")),
                ("provider_account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="zones", to="dns.dnsprovideraccount")),
            ],
            options={
                "ordering": ["name", "id"],
                "indexes": [
                    models.Index(fields=["organization", "name"], name="dns_zone_org_name_idx"),
                    models.Index(fields=["provider_account", "is_active"], name="dns_zone_provider_active_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "name"), name="dns_zone_org_name_uniq"),
                    models.UniqueConstraint(
                        fields=("provider_account", "provider_zone_id"),
                        condition=models.Q(provider_zone_id__isnull=False),
                        name="dns_zone_provider_zone_id_uniq",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="DnsRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("record_type", models.CharField(choices=[("A", "A"), ("AAAA", "AAAA"), ("CNAME", "CNAME"), ("TXT", "TXT")], default="A", max_length=10)),
                ("hostname", models.CharField(default="@", max_length=255)),
                ("value", models.CharField(max_length=255)),
                ("ttl", models.PositiveIntegerField(default=1)),
                ("proxied", models.BooleanField(default=False)),
                ("provider_record_id", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("ACTIVE", "Active"), ("FAILED", "Failed"), ("DELETED", "Deleted")], default="PENDING", max_length=15)),
                ("last_error", models.TextField(blank=True, default="")),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dns_records", to="organizations.organization")),
                ("zone", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="records", to="dns.dnszone")),
            ],
            options={
                "ordering": ["zone__name", "hostname", "record_type", "id"],
                "indexes": [
                    models.Index(fields=["organization", "status"], name="dns_record_org_status_idx"),
                    models.Index(fields=["zone", "hostname"], name="dns_record_zone_host_idx"),
                    models.Index(fields=["zone", "provider_record_id"], name="dns_record_zone_provider_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("zone", "record_type", "hostname"),
                        condition=~models.Q(status="DELETED"),
                        name="dns_active_record_zone_type_host_uniq",
                    ),
                ],
            },
        ),
    ]
