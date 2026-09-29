from django.db import migrations, models


def normalize_series_competition_names(apps, schema_editor):
    Competition = apps.get_model('judging_app', 'Competition')
    CompetitionSeries = apps.get_model('judging_app', 'CompetitionSeries')

    for series in CompetitionSeries.objects.all():
        Competition.objects.filter(series_id=series.id).exclude(name=series.name).update(name=series.name)


class Migration(migrations.Migration):

    dependencies = [
        ('judging_app', '0031_competition_series'),
    ]

    operations = [
        migrations.AddField(
            model_name='zipimportjob',
            name='skipped_rows',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(normalize_series_competition_names, migrations.RunPython.noop),
    ]
