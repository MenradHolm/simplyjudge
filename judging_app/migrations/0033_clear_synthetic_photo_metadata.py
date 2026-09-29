from django.db import migrations
from django.db.models import F, Q


def clear_synthetic_photo_metadata(apps, schema_editor):
    Photo = apps.get_model('judging_app', 'Photo')
    Photo.objects.filter(
        Q(description='') | Q(description__isnull=True),
        Q(camera_settings='') | Q(camera_settings__isnull=True),
        category='General',
        title=F('entry_code'),
    ).exclude(entry_code='').update(category='')


class Migration(migrations.Migration):

    dependencies = [
        ('judging_app', '0032_zipimportjob_skipped_rows_and_normalize_series_names'),
    ]

    operations = [
        migrations.RunPython(clear_synthetic_photo_metadata, migrations.RunPython.noop),
    ]
