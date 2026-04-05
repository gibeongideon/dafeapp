from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0011_odooinstance_installation_summary"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="odooinstance",
            name="addons_last_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="addons_path_cache",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="addons_root_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="addons_sync_status",
            field=models.CharField(
                choices=[
                    ("NOT_CONFIGURED", "Not configured"),
                    ("PENDING", "Pending"),
                    ("READY", "Ready"),
                    ("ERROR", "Error"),
                ],
                default="NOT_CONFIGURED",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="OdooInstanceGitRepo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("repo_name", models.CharField(max_length=255)),
                ("git_url", models.CharField(max_length=500)),
                ("branch", models.CharField(default="main", max_length=120)),
                (
                    "auth_type",
                    models.CharField(
                        choices=[
                            ("PUBLIC", "Public"),
                            ("GITHUB_OAUTH", "GitHub OAuth"),
                            ("TOKEN", "Personal access token"),
                            ("SSH_KEY", "SSH key"),
                        ],
                        default="PUBLIC",
                        max_length=20,
                    ),
                ),
                ("local_path", models.CharField(blank=True, default="", max_length=500)),
                ("auto_update", models.BooleanField(default=False)),
                ("is_enabled", models.BooleanField(default=True)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("last_pulled_commit", models.CharField(blank=True, default="", max_length=64)),
                ("last_pulled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("CONNECTED", "Connected"),
                            ("CLONING", "Cloning"),
                            ("UPDATING", "Updating"),
                            ("ERROR", "Error"),
                            ("DISCONNECTED", "Disconnected"),
                        ],
                        default="DISCONNECTED",
                        max_length=20,
                    ),
                ),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="created_odoo_instance_git_repos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "instance",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="git_repos",
                        to="deployments.odooinstance",
                    ),
                ),
            ],
            options={
                "ordering": ["display_order", "repo_name", "id"],
                "indexes": [
                    models.Index(fields=["instance", "status"], name="dep_repo_inst_status_idx"),
                    models.Index(fields=["instance", "auto_update"], name="dep_repo_inst_auto_idx"),
                ],
                "unique_together": {("instance", "repo_name")},
            },
        ),
    ]
