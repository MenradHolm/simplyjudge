import base64
import io
import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageOps

from judging_app.models import Competition, RubricCriterion
from judging_app.views import (
    anonymize_camera_settings,
    anonymize_photo_title,
    is_approved_judge,
    judging_photo_queryset,
)

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / 'offline' / 'pack_template.html'


def resize_for_pack(file_bytes, max_edge, quality):
    with Image.open(io.BytesIO(file_bytes)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)
        output = io.BytesIO()
        # Saved without EXIF so the judge never sees location or camera identity.
        image.save(output, format='JPEG', quality=quality, optimize=True)
    return output.getvalue()


class Command(BaseCommand):
    help = (
        'Build a single self-contained HTML judging panel for one judge that works fully offline. '
        'Scores are exported as JSON and loaded back with import_offline_scores.'
    )

    def add_arguments(self, parser):
        parser.add_argument('competition_slug')
        parser.add_argument('judge_username')
        parser.add_argument('output_html')
        parser.add_argument('--max-edge', type=int, default=2200, help='Longest image edge in pixels (default 2200).')
        parser.add_argument('--quality', type=int, default=82, help='JPEG quality 1-95 (default 82).')
        parser.add_argument('--limit', type=int, default=0, help='Only include the first N photos (for testing).')

    def handle(self, *args, **options):
        competition = Competition.objects.filter(slug=options['competition_slug']).first()
        if competition is None:
            raise CommandError(f'Competition not found: {options["competition_slug"]}')
        judge = get_user_model().objects.filter(username=options['judge_username']).first()
        if judge is None:
            raise CommandError(f'User not found: {options["judge_username"]}')
        if not is_approved_judge(judge, competition):
            raise CommandError(f'{judge.username} is not an approved judge for {competition.slug}.')

        rubric = list(RubricCriterion.objects.filter(competition=competition))
        if not rubric:
            raise CommandError('This competition has no rubric criteria yet.')

        photos = list(judging_photo_queryset(competition, judge))
        if options['limit']:
            photos = photos[:options['limit']]
        if not photos:
            raise CommandError('No photos are available to this judge (full competitions only show shortlisted photos).')

        pack_photos = []
        for number, photo in enumerate(photos, start=1):
            try:
                with photo.image.open('rb') as source:
                    raw = source.read()
                data = resize_for_pack(raw, options['max_edge'], options['quality'])
            except Exception as exc:
                raise CommandError(f'Could not read photo #{photo.id} ({type(exc).__name__}). Nothing was written.')
            title = ''
            if photo.title != photo.entry_code:
                title = anonymize_photo_title(photo.title)
            pack_photos.append({
                'id': photo.id,
                'title': title,
                'category': photo.category or '',
                'description': photo.description or '',
                'settings': anonymize_camera_settings(photo.camera_settings),
                'image': base64.b64encode(data).decode('ascii'),
            })
            self.stdout.write(f'  [{number}/{len(photos)}] #{photo.id} {len(data) // 1024} KB')

        pack = {
            'pack_id': uuid.uuid4().hex,
            'competition_slug': competition.slug,
            'competition_name': competition.name,
            'edition_name': competition.edition_name,
            'judge_username': judge.username,
            'judge_label': judge.get_full_name() or judge.username,
            'rubric': [
                {
                    'id': criterion.id,
                    'name': criterion.name,
                    'description': criterion.description or '',
                    'out_of': criterion.score_out_of,
                    'weight': criterion.weight,
                }
                for criterion in rubric
            ],
            'photos': pack_photos,
        }
        payload = json.dumps(pack, ensure_ascii=False).replace('</', '<\\/')
        html = TEMPLATE_PATH.read_text(encoding='utf-8').replace('__PACK_JSON__', payload)
        output_path = Path(options['output_html'])
        output_path.write_text(html, encoding='utf-8')
        size_mb = output_path.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(pack_photos)} photo(s) for {judge.username} to {output_path} ({size_mb:.1f} MB).'
        ))
