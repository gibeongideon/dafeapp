from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dns", "0003_domainassignment_source_primary"),
    ]

    operations = [
        migrations.AddField(
            model_name="domainassignment",
            name="provider_record_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
