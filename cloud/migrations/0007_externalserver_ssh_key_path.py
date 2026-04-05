from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cloud", "0006_remove_ssh_key_auth"),
    ]

    operations = [
        migrations.AddField(
            model_name="externalserver",
            name="ssh_key_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
