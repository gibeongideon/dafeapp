from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0030_git_tab_features"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooserver",
            name="platform_domain",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="platform_domain_record_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
