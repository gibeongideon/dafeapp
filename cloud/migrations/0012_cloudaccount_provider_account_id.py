from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cloud", "0011_platform_cloud_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="cloudaccount",
            name="provider_account_id",
            field=models.CharField(blank=True, db_index=True, max_length=200),
        ),
    ]
