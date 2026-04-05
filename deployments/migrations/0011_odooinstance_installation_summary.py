from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0010_odoo_server_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooinstance",
            name="installation_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="installation_summary_text",
            field=models.TextField(blank=True),
        ),
    ]
