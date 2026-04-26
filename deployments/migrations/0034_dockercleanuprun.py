from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0003_add_first_name_to_invite"),
        ("deployments", "0033_odoo_server_heartbeat_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DockerCleanupRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("RUNNING", "Running"), ("DONE", "Done"), ("FAILED", "Failed")], default="RUNNING", max_length=15)),
                ("cleanup_types", models.JSONField(blank=True, default=list)),
                ("age_threshold_days", models.PositiveIntegerField(default=7)),
                ("items_deleted", models.PositiveIntegerField(default=0)),
                ("space_freed_bytes", models.BigIntegerField(default=0)),
                ("duration_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True)),
                ("command_log", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="docker_cleanup_runs", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="docker_cleanup_runs", to="organizations.organization")),
                ("server", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="docker_cleanup_runs", to="deployments.odooserver")),
            ],
            options={
                "ordering": ["-started_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="dockercleanuprun",
            index=models.Index(fields=["organization", "server", "-started_at"], name="dep_docker_cleanup_server_idx"),
        ),
        migrations.AddIndex(
            model_name="dockercleanuprun",
            index=models.Index(fields=["organization", "status"], name="dep_docker_cleanup_status_idx"),
        ),
    ]
