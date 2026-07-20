import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judging_app.models import Competition, Photo
from judging_app.views import truncate_text


CORRECTION_FIELDS = [
    'entry_code',
    'title',
    'photographer_name',
    'photographer_email',
    'category',
    'description',
    'camera_settings',
]


class Command(BaseCommand):
    help = 'Apply in-place Photo metadata corrections from a CSV keyed by photo_id.'

    def add_arguments(self, parser):
        parser.add_argument('competition_slug')
        parser.add_argument('corrections_csv')
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply the metadata updates. Without this flag, the command only reports a dry run.',
        )

    def handle(self, *args, **options):
        competition = Competition.objects.filter(slug=options['competition_slug']).first()
        if competition is None:
            raise CommandError(f'Competition not found: {options["competition_slug"]}')

        rows = self.read_rows(options['corrections_csv'])
        photo_ids = []
        for row_number, row in rows:
            raw_photo_id = (row.get('photo_id') or '').strip()
            if not raw_photo_id.isdigit():
                raise CommandError(f'Row {row_number}: photo_id is required and must be numeric.')
            photo_ids.append(int(raw_photo_id))

        duplicate_ids = sorted({photo_id for photo_id in photo_ids if photo_ids.count(photo_id) > 1})
        if duplicate_ids:
            raise CommandError(f'Duplicate photo_id value(s) in corrections CSV: {", ".join(map(str, duplicate_ids[:10]))}')

        photos_by_id = {
            photo.id: photo
            for photo in Photo.objects.filter(competition=competition, id__in=photo_ids)
        }
        missing_ids = sorted(set(photo_ids) - set(photos_by_id))
        if missing_ids:
            raise CommandError(f'Photo ID(s) not found in {competition.slug}: {", ".join(map(str, missing_ids[:10]))}')

        updates = []
        for row_number, row in rows:
            photo = photos_by_id[int((row.get('photo_id') or '').strip())]
            changes = {}
            for field in CORRECTION_FIELDS:
                if field not in row:
                    continue
                incoming = self.clean_value(field, row.get(field))
                current = getattr(photo, field) or ''
                if current != incoming:
                    changes[field] = (current, incoming)
            if changes:
                updates.append((photo, changes, row_number))

        self.report(competition, updates, options['apply'])
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run only. Re-run with --apply to save these metadata changes.'))
            return

        with transaction.atomic():
            for photo, changes, _row_number in updates:
                update_fields = []
                for field, (_old, new) in changes.items():
                    setattr(photo, field, new)
                    update_fields.append(field)
                photo.save(update_fields=update_fields)

        self.stdout.write(self.style.SUCCESS(f'Applied CSV corrections to {len(updates)} photo(s).'))

    def read_rows(self, path):
        with open(path, newline='', encoding='utf-8-sig') as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise CommandError('Corrections CSV has no header row.')
            if 'photo_id' not in reader.fieldnames:
                raise CommandError('Corrections CSV must include a photo_id column.')
            return list(enumerate(reader, start=2))

    def clean_value(self, field, value):
        value = str(value or '').strip()
        if field == 'entry_code':
            return truncate_text(value, 120)
        if field == 'title':
            return truncate_text(value or 'Untitled', 200)
        if field == 'photographer_name':
            return truncate_text(value or 'Unknown', 200)
        if field == 'category':
            return truncate_text(value or 'General', 100)
        return value

    def report(self, competition, updates, apply):
        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(f'{mode}: CSV corrections for {competition.name}')
        self.stdout.write(f'Photos with metadata changes: {len(updates)}')
        for photo, changes, row_number in updates[:25]:
            fields = ', '.join(changes.keys())
            self.stdout.write(f'  Row {row_number}, Photo #{photo.id}: update {fields}')
            if 'title' in changes:
                old, new = changes['title']
                self.stdout.write(f'    title: {old!r} -> {new!r}')
        if len(updates) > 25:
            self.stdout.write(f'  ... {len(updates) - 25} more photo update(s)')
