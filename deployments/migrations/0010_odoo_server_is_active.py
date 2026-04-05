from django.db import migrations, models


def forwards(apps, schema_editor):
    OdooServer = apps.get_model("deployments", "OdooServer")
    OdooServer.objects.filter(status__in=["ARCHIVED", "DELETED"]).update(is_active=False)


def backwards(apps, schema_editor):
    OdooServer = apps.get_model("deployments", "OdooServer")
    OdooServer.objects.filter(is_active=False).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0009_odooserver_installation_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooserver",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(forwards, backwards),
    ]
