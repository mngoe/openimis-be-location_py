# Generated by Django 3.2.17 on 2023-02-23 11:28

import logging

from django.db import migrations

from core.utils import insert_role_right_for_system

logger = logging.getLogger(__name__)

IMIS_ADMINISTRATOR_ROLE_IS_SYSTEM = 64
ROLE_RIGHT_ID = 121906

def add_rights(apps, schema_editor):
    """
    Add create_region_locations_perms to the IMIS Administrator.
    """
    insert_role_right_for_system(IMIS_ADMINISTRATOR_ROLE_IS_SYSTEM, ROLE_RIGHT_ID)


class Migration(migrations.Migration):
    dependencies = [
        ('location', '0010_auto_20230213_1112'),
    ]

    operations = [
        migrations.RunPython(add_rights, migrations.RunPython.noop),
    ]
