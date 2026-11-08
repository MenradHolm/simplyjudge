import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from PIL import Image

from .models import Competition, Photo, RubricCriterion, Score


def jpeg_bytes(size=(3000, 2000)):
    output = io.BytesIO()
    Image.new('RGB', size, (120, 80, 40)).save(output, format='JPEG')
    return output.getvalue()


class OfflinePackTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.judge = User.objects.create_user('hannes', password='x-test-only-1')
        self.competition = Competition.objects.create(
            name='Shutter Society', slug='shutter', workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        self.competition.judges.add(self.judge)
        self.rubric = RubricCriterion.objects.create(
            competition=self.competition, name='Composition', score_out_of=10, weight=2.0,
        )
        self.photo = Photo.objects.create(
            competition=self.competition, entry_code='SS001', title='SS001',
            photographer_name='Secret Name', category='Open',
        )
        self.photo.image.save('a.jpg', ContentFile(jpeg_bytes()))

    def build_pack(self):
        path = Path(self.tmp.name) / 'pack.html'
        call_command('export_offline_judging_pack', 'shutter', 'hannes', str(path), '--max-edge', '800', verbosity=0)
        return path.read_text(encoding='utf-8')

    def test_pack_is_self_contained_and_anonymous(self):
        html = self.build_pack()
        self.assertNotIn('Secret Name', html)
        self.assertNotIn('http://', html.split('<script id="pack-data"')[0].replace('http://www.w3.org', ''))
        payload = html.split('<script id="pack-data" type="application/json">')[1].split('</script>')[0]
        pack = json.loads(payload.replace('<\\/', '</'))
        self.assertEqual(pack['judge_username'], 'hannes')
        self.assertEqual(len(pack['photos']), 1)
        self.assertEqual(pack['rubric'][0]['out_of'], 10)

    def test_non_judge_is_refused(self):
        User.objects.create_user('stranger', password='x-test-only-1')
        with self.assertRaises(CommandError):
            call_command('export_offline_judging_pack', 'shutter', 'stranger', str(Path(self.tmp.name) / 'p.html'), verbosity=0)

    def write_scores(self, value):
        path = Path(self.tmp.name) / 'scores.json'
        path.write_text(json.dumps({
            'format': 'simplyjudge-offline-scores', 'version': 1,
            'competition_slug': 'shutter', 'judge_username': 'hannes',
            'scores': [
                {'photo_id': self.photo.id, 'criteria_scores': {str(self.rubric.id): value}, 'comment': 'Nice'},
                {'photo_id': 99999, 'criteria_scores': {}, 'comment': ''},
            ],
        }), encoding='utf-8')
        return path

    def test_import_clamps_and_does_not_overwrite_by_default(self):
        call_command('import_offline_scores', str(self.write_scores(50)), verbosity=0)
        score = Score.objects.get(photo=self.photo, judge=self.judge)
        self.assertEqual(score.criteria_scores[str(self.rubric.id)], 10.0)
        self.assertEqual(score.total_score, 20.0)
        self.assertEqual(score.comment, 'Nice')

        call_command('import_offline_scores', str(self.write_scores(3)), verbosity=0)
        score.refresh_from_db()
        self.assertEqual(score.criteria_scores[str(self.rubric.id)], 10.0)

        call_command('import_offline_scores', str(self.write_scores(3)), '--overwrite', verbosity=0)
        score.refresh_from_db()
        self.assertEqual(score.total_score, 6.0)

    def test_dry_run_saves_nothing(self):
        call_command('import_offline_scores', str(self.write_scores(5)), '--dry-run', verbosity=0)
        self.assertFalse(Score.objects.exists())
