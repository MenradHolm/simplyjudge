import os
import zipfile

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judging_app.models import Competition, Photo
from judging_app.views import (
    clean_cell,
    collect_zip_image_index,
    expand_entry_rows,
    find_entry_csv,
    find_matching_image,
    normalize_match_key,
    parse_entry_rows,
    truncate_text,
)


METADATA_FIELDS = [
    'title',
    'photographer_name',
    'photographer_email',
    'category',
    'description',
    'camera_settings',
]


class Command(BaseCommand):
    help = 'Repair Photo metadata in place from an original EntryForm ZIP without touching scores, votes, status, or images.'

    def add_arguments(self, parser):
        parser.add_argument('competition_slug')
        parser.add_argument('zip_path')
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply the metadata updates. Without this flag, the command only reports a dry run.',
        )

    def handle(self, *args, **options):
        competition = Competition.objects.filter(slug=options['competition_slug']).first()
        if competition is None:
            raise CommandError(f'Competition not found: {options["competition_slug"]}')

        zip_path = options['zip_path']
        if not os.path.exists(zip_path):
            raise CommandError(f'ZIP file not found: {zip_path}')

        source_rows, unmatched_source_rows, duplicate_source_keys = self.source_rows_by_image_key(zip_path)
        if duplicate_source_keys:
            raise CommandError(
                'The source ZIP maps multiple rows to the same image key: '
                f'{", ".join(sorted(duplicate_source_keys)[:10])}'
            )

        photos = list(Photo.objects.filter(competition=competition).order_by('id'))
        duplicate_photo_keys = self.duplicate_photo_image_keys(photos)
        if duplicate_photo_keys:
            raise CommandError(
                'Multiple existing photos share the same image key, so automatic repair would be unsafe: '
                f'{", ".join(sorted(duplicate_photo_keys)[:10])}'
            )

        updates = []
        missing_source_rows = []
        for photo in photos:
            image_key = self.photo_image_key(photo)
            if not image_key:
                missing_source_rows.append((photo, 'no stored image filename'))
                continue

            source = source_rows.get(image_key)
            if source is None:
                missing_source_rows.append((photo, image_key))
                continue

            changes = {}
            for field in METADATA_FIELDS:
                current = getattr(photo, field) or ''
                incoming = source[field] or ''
                if current != incoming:
                    changes[field] = (current, incoming)

            if source['entry_code']:
                current_entry_code = photo.entry_code or ''
                if current_entry_code != source['entry_code']:
                    changes['entry_code'] = (current_entry_code, source['entry_code'])

            if changes:
                updates.append((photo, changes, source))

        self.report(competition, updates, missing_source_rows, unmatched_source_rows, options['apply'])

        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run only. Re-run with --apply to save these metadata changes.'))
            return

        with transaction.atomic():
            for photo, changes, _source in updates:
                update_fields = []
                for field, (_old, new) in changes.items():
                    setattr(photo, field, new)
                    update_fields.append(field)
                photo.save(update_fields=update_fields)

        self.stdout.write(self.style.SUCCESS(f'Applied metadata repairs to {len(updates)} photo(s).'))

    def source_rows_by_image_key(self, zip_path):
        source_rows = {}
        unmatched_source_rows = []
        duplicate_source_keys = set()

        with zipfile.ZipFile(zip_path) as package:
            csv_info = find_entry_csv(package)
            rows = expand_entry_rows(parse_entry_rows(package.read(csv_info.filename)))
            images = collect_zip_image_index(package)

            for row_number, row in enumerate(rows, start=2):
                source = self.metadata_from_row(row)
                image_references = [
                    clean_cell(row, 'Image'),
                    clean_cell(row, 'Image File'),
                    clean_cell(row, 'Filename'),
                    clean_cell(row, 'File Name'),
                    clean_cell(row, 'Photo File'),
                    clean_cell(row, 'Photo Filename'),
                    clean_cell(row, 'Asset'),
                ]
                image_info = find_matching_image(
                    images,
                    [*image_references, source['entry_code']],
                    photographer=source['photographer_name'],
                )
                if not image_info:
                    image_info = find_matching_image(images, [source['title']], allow_suffix=False)
                if not image_info:
                    unmatched_source_rows.append((row_number, source['title']))
                    continue

                image_key = normalize_match_key(image_info.filename)
                if image_key in source_rows:
                    duplicate_source_keys.add(image_key)
                    continue
                source_rows[image_key] = source

        return source_rows, unmatched_source_rows, duplicate_source_keys

    def metadata_from_row(self, row):
        return {
            'entry_code': truncate_text(clean_cell(row, 'Code', 'ID', 'Number', 'Entry ID', 'Entry Code', 'id'), 120),
            'title': truncate_text(clean_cell(row, 'Title', 'title', default='Untitled'), 200),
            'photographer_name': truncate_text(
                clean_cell(row, 'Photographer', 'photographer', 'Photographer Name', 'photographer_name', default='Unknown'),
                200,
            ),
            'photographer_email': clean_cell(
                row,
                'Photographer Email',
                'photographer_email',
                'Email',
                'email',
                'Email Address',
                'email_address',
            ),
            'category': truncate_text(clean_cell(row, 'Category', 'category', default='General'), 100),
            'description': clean_cell(row, 'Description', 'description', 'Story', 'story'),
            'camera_settings': clean_cell(row, 'Camera Settings', 'camera settings', 'Settings', 'settings'),
        }

    def duplicate_photo_image_keys(self, photos):
        seen = set()
        duplicates = set()
        for photo in photos:
            image_key = self.photo_image_key(photo)
            if not image_key:
                continue
            if image_key in seen:
                duplicates.add(image_key)
            seen.add(image_key)
        return duplicates

    def photo_image_key(self, photo):
        image_name = getattr(photo.image, 'name', '') or ''
        if not image_name:
            return ''
        return normalize_match_key(image_name)

    def report(self, competition, updates, missing_source_rows, unmatched_source_rows, apply):
        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(f'{mode}: metadata repair for {competition.name}')
        self.stdout.write(f'Photos with metadata changes: {len(updates)}')
        self.stdout.write(f'Existing photos without a source row match: {len(missing_source_rows)}')
        self.stdout.write(f'Source rows without a source image match: {len(unmatched_source_rows)}')

        for photo, changes, _source in updates[:25]:
            fields = ', '.join(changes.keys())
            self.stdout.write(f'  Photo #{photo.id}: update {fields}')
            if 'title' in changes:
                old, new = changes['title']
                self.stdout.write(f'    title: {old!r} -> {new!r}')

        if len(updates) > 25:
            self.stdout.write(f'  ... {len(updates) - 25} more photo update(s)')

        for photo, reason in missing_source_rows[:10]:
            self.stdout.write(f'  No source match for existing Photo #{photo.id}: {reason}')
        if len(missing_source_rows) > 10:
            self.stdout.write(f'  ... {len(missing_source_rows) - 10} more existing photo(s) without source matches')

        for row_number, title in unmatched_source_rows[:10]:
            self.stdout.write(f'  Source row {row_number} has no safe image match: {title}')
        if len(unmatched_source_rows) > 10:
            self.stdout.write(f'  ... {len(unmatched_source_rows) - 10} more source row(s) without image matches')
