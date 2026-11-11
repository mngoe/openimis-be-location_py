from django.db import migrations

from core.utils import insert_role_right_for_system, remove_role_right_for_system

UNSCOPED_LOCATION_RIGHT = 121907
IMIS_ADMIN_ROLE = 64


def add_right(apps, schema_editor):
    # the IMIS administrator already sees every location: the right states it
    insert_role_right_for_system(IMIS_ADMIN_ROLE, UNSCOPED_LOCATION_RIGHT, apps)


def remove_right(apps, schema_editor):
    remove_role_right_for_system(IMIS_ADMIN_ROLE, UNSCOPED_LOCATION_RIGHT, apps)


class Migration(migrations.Migration):

    dependencies = [
        ("location", "0025_add_JsonExt_To_TblLocations"),
        ("core", "0029_roleright_uba_userbusinessaccess"),
    ]

    operations = [
        migrations.RunPython(add_right, remove_right),
    ]
