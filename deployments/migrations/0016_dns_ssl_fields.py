# Generated manually for DNS and SSL rollout fields.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0015_merge_20260327_2316"),
        ("dns", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooinstance",
            name="domain_last_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="domain_status",
            field=models.CharField(
                choices=[
                    ("NOT_CONFIGURED", "Not configured"),
                    ("PENDING", "Pending"),
                    ("ACTIVE", "Active"),
                    ("FAILED", "Failed"),
                    ("DELETED", "Deleted"),
                ],
                default="NOT_CONFIGURED",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="ssl_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="ssl_status",
            field=models.CharField(
                choices=[
                    ("NOT_CONFIGURED", "Not configured"),
                    ("PENDING", "Pending"),
                    ("ACTIVE", "Active"),
                    ("FAILED", "Failed"),
                ],
                default="NOT_CONFIGURED",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="domain_routing_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="managed_dns_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="managed_dns_zone",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="odoo_servers", to="dns.dnszone"),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="tls_mode",
            field=models.CharField(
                choices=[("DISABLED", "Disabled"), ("LETS_ENCRYPT", "Let's Encrypt")],
                default="LETS_ENCRYPT",
                max_length=20,
            ),
        ),
    ]
