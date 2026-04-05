# Generated manually for multi-domain instance support.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dns", "0002_domainassignment"),
    ]

    operations = [
        migrations.AddField(
            model_name="domainassignment",
            name="is_primary",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="domainassignment",
            name="source",
            field=models.CharField(
                choices=[("PLATFORM", "Platform"), ("CUSTOM", "Custom")],
                default="CUSTOM",
                max_length=20,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="domainassignment",
            name="dns_active_assignment_instance_uniq",
        ),
        migrations.AddConstraint(
            model_name="domainassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True), ~models.Q(("status", "DELETED"))),
                fields=("instance",),
                name="dns_active_primary_assignment_instance_uniq",
            ),
        ),
    ]
