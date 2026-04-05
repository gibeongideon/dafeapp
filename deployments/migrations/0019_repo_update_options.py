from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0018_alter_deploymentjob_job_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooinstancegitrepo",
            name="auto_upgrade_modules_on_update",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="odooinstancegitrepo",
            name="install_requirements_on_update",
            field=models.BooleanField(default=False),
        ),
    ]
