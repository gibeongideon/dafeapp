from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0006_phase2_deployment_reliability"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooserver",
            name="deployment_mode",
            field=models.CharField(
                choices=[
                    ("BARE_METAL", "Bare-metal (systemd)"),
                    ("DOCKER", "Docker (Traefik + containers)"),
                ],
                default="BARE_METAL",
                max_length=15,
            ),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="docker_postgres_password",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="odooinstance",
            name="container_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
