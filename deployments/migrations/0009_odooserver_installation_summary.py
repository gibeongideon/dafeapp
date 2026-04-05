# Generated manually to add persisted installation summary fields.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0008_server_ssh_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooserver",
            name="installation_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="installation_summary_text",
            field=models.TextField(blank=True),
        ),
    ]
