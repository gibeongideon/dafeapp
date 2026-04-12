"""Add retention_days to OdooInstanceBackupSchedule."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backups", "0003_schedule_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooinstancebackupschedule",
            name="retention_days",
            field=models.IntegerField(
                choices=[(1, "1 Day"), (7, "1 Week"), (30, "1 Month"), (0, "Keep Forever")],
                default=0,
                help_text="Delete backups older than this. 0 = keep forever.",
            ),
        ),
    ]
