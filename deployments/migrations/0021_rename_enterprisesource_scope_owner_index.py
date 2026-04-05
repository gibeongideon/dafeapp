from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0020_enterprise_source_modes_and_user_sources"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="enterprisesource",
            old_name="dep_ent_scope_owner_version_idx",
            new_name="dep_ent_scope_owner_ver_idx",
        ),
    ]
