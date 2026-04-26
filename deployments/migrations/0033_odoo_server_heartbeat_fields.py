import uuid

from django.db import migrations, models


def populate_agent_tokens(apps, schema_editor):
    OdooServer = apps.get_model("deployments", "OdooServer")
    for server in OdooServer.objects.filter(agent_token__isnull=True).iterator():
        server.agent_token = uuid.uuid4()
        server.save(update_fields=["agent_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0032_remove_protect_on_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="odooserver",
            name="agent_token",
            field=models.UUIDField(blank=True, db_index=True, default=uuid.uuid4, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="last_agent_repair_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="odooserver",
            name="last_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_agent_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="odooserver",
            name="agent_token",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
