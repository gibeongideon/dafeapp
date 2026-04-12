from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "__first__"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("backups", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OdooInstanceBackupSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=False)),
                ("frequency", models.CharField(choices=[("DAILY", "Daily"), ("WEEKLY", "Weekly")], default="DAILY", max_length=10)),
                ("weekday", models.CharField(choices=[("1", "Monday"), ("2", "Tuesday"), ("3", "Wednesday"), ("4", "Thursday"), ("5", "Friday"), ("6", "Saturday"), ("0", "Sunday")], default="0", max_length=1)),
                ("hour_utc", models.PositiveSmallIntegerField(default=2)),
                ("minute_utc", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_instance_backup_schedules", to=settings.AUTH_USER_MODEL)),
                ("instance", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="backup_schedule", to="deployments.odooinstance")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="instance_backup_schedules", to="organizations.organization")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_instance_backup_schedules", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["instance_id"],
            },
        ),
        migrations.AddIndex(
            model_name="odooinstancebackupschedule",
            index=models.Index(fields=["organization", "enabled"], name="backups_od_organiz_7fe6ff_idx"),
        ),
    ]
