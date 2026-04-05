from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0016_dns_ssl_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EnterpriseSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("odoo_version", models.CharField(max_length=10)),
                ("package_name", models.CharField(max_length=255)),
                ("archive_filename", models.CharField(max_length=255)),
                ("archive_path", models.CharField(max_length=500)),
                ("extract_path", models.CharField(blank=True, default="", max_length=500)),
                ("addons_source_path", models.CharField(blank=True, default="", max_length=500)),
                ("is_active", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("UPLOADED", "Uploaded"),
                            ("READY", "Ready"),
                            ("FAILED", "Failed"),
                        ],
                        default="UPLOADED",
                        max_length=20,
                    ),
                ),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="uploaded_enterprise_sources",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["odoo_version", "is_active"], name="dep_ent_version_active_idx"),
                ],
            },
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_remote_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="instances",
                to="deployments.enterprisesource",
            ),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_status",
            field=models.CharField(
                choices=[
                    ("NOT_ENABLED", "Not enabled"),
                    ("PENDING", "Pending"),
                    ("ACTIVE", "Active"),
                    ("ERROR", "Error"),
                ],
                default="NOT_ENABLED",
                max_length=20,
            ),
        ),
    ]
