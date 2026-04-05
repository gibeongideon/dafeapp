from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cloud", "0007_externalserver_ssh_key_path"),
    ]

    operations = [
        migrations.CreateModel(
            name="PyOSSSHSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("default_ssh_key_path", models.CharField(blank=True, default="", max_length=500)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "PYOS SSH Settings",
                "verbose_name_plural": "PYOS SSH Settings",
            },
        ),
    ]
