"""
Change OdooInstanceBackupSchedule.instance from OneToOneField → ForeignKey
so an instance can have multiple independent backup schedules.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backups", "0002_odooinstancebackupschedule"),
        ("deployments", "__first__"),
    ]

    operations = [
        migrations.AlterField(
            model_name="odooinstancebackupschedule",
            name="instance",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="backup_schedules",
                to="deployments.odooinstance",
            ),
        ),
    ]
