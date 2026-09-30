from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judging_app', '0033_clear_synthetic_photo_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='zipimportjob',
            name='diagnostics',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
