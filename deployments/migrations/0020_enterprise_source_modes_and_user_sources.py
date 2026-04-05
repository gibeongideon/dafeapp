from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0019_repo_update_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="enterprisesource",
            name="owner",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="enterprise_sources", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="enterprisesource",
            name="release_code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="enterprisesource",
            name="source_scope",
            field=models.CharField(choices=[("PLATFORM", "Platform"), ("USER", "User")], default="PLATFORM", max_length=20),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_auto_sync",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_available_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_source_mode",
            field=models.CharField(choices=[("PLATFORM", "Platform"), ("USER", "User")], default="PLATFORM", max_length=20),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="enterprise_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddIndex(
            model_name="enterprisesource",
            index=models.Index(fields=["source_scope", "owner", "odoo_version"], name="dep_ent_scope_owner_version_idx"),
        ),
    ]
