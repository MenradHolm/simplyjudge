import uuid

from django.db import migrations, models


def populate_judge_invite_tokens(apps, schema_editor):
    Competition = apps.get_model('judging_app', 'Competition')
    for competition in Competition.objects.filter(judge_invite_token__isnull=True):
        token = uuid.uuid4()
        while Competition.objects.filter(judge_invite_token=token).exists():
            token = uuid.uuid4()
        competition.judge_invite_token = token
        competition.save(update_fields=['judge_invite_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('judging_app', '0028_photo_exif_warning_flag_photo_is_raw_verified_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='competition',
            name='judge_invite_token',
            field=models.UUIDField(blank=True, editable=True, null=True),
        ),
        migrations.RunPython(populate_judge_invite_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='competition',
            name='judge_invite_token',
            field=models.UUIDField(default=uuid.uuid4, editable=True, unique=True),
        ),
        migrations.AlterField(
            model_name='competition',
            name='judges',
            field=models.ManyToManyField(blank=True, related_name='judged_competitions', to='auth.user'),
        ),
    ]
