from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cloud", "0005_system_ssh_key"),
    ]

    operations = [
        # Drop the encrypted_private_key column
        migrations.RemoveField(
            model_name="externalserver",
            name="encrypted_private_key",
        ),
        # Update auth_type choices and default
        migrations.AlterField(
            model_name="externalserver",
            name="auth_type",
            field=models.CharField(
                choices=[
                    ("PASSWORD", "Password"),
                    ("DAFEAPP_KEY", "DafeApp SSH Key (public key auth)"),
                ],
                default="DAFEAPP_KEY",
                max_length=15,
            ),
        ),
    ]
