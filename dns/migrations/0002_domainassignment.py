# Generated manually for DNS domain assignments.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0015_merge_20260327_2316"),
        ("dns", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DomainAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=255)),
                ("hostname", models.CharField(default="@", max_length=255)),
                ("proxied", models.BooleanField(default=False)),
                ("is_managed", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("ACTIVE", "Active"), ("FAILED", "Failed"), ("DELETED", "Deleted")], default="PENDING", max_length=15)),
                ("last_error", models.TextField(blank=True, default="")),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("dns_record", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="domain_assignments", to="dns.dnsrecord")),
                ("instance", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="domain_assignments", to="deployments.odooinstance")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="domain_assignments", to="organizations.organization")),
                ("zone", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assignments", to="dns.dnszone")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(fields=["organization", "domain"], name="dns_assign_org_domain_idx"),
                    models.Index(fields=["zone", "hostname"], name="dns_assign_zone_host_idx"),
                    models.Index(fields=["organization", "status"], name="dns_assign_org_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("domain",),
                        condition=~models.Q(status="DELETED"),
                        name="dns_active_assignment_domain_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("instance",),
                        condition=~models.Q(status="DELETED"),
                        name="dns_active_assignment_instance_uniq",
                    ),
                ],
            },
        ),
    ]
