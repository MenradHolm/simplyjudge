import csv

from django.core.management.base import BaseCommand, CommandError

from judging_app.models import Competition, Photo


class Command(BaseCommand):
    help = 'Export a CSV template for in-place Photo metadata corrections keyed by Photo ID.'

    def add_arguments(self, parser):
        parser.add_argument('competition_slug')
        parser.add_argument('output_csv')

    def handle(self, *args, **options):
        competition = Competition.objects.filter(slug=options['competition_slug']).first()
        if competition is None:
            raise CommandError(f'Competition not found: {options["competition_slug"]}')

        fields = [
            'photo_id',
            'image_name',
            'image_url',
            'image_preview_formula',
            'entry_code',
            'title',
            'photographer_name',
            'photographer_email',
            'category',
            'description',
            'camera_settings',
        ]

        photos = Photo.objects.filter(competition=competition).order_by('id')
        with open(options['output_csv'], 'w', newline='', encoding='utf-8-sig') as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for photo in photos:
                try:
                    image_url = photo.image.url if photo.image else ''
                except ValueError:
                    image_url = ''
                writer.writerow({
                    'photo_id': photo.id,
                    'image_name': photo.image.name if photo.image else '',
                    'image_url': image_url,
                    'image_preview_formula': f'=IMAGE("{image_url}")' if image_url else '',
                    'entry_code': photo.entry_code or '',
                    'title': photo.title or '',
                    'photographer_name': photo.photographer_name or '',
                    'photographer_email': photo.photographer_email or '',
                    'category': photo.category or '',
                    'description': photo.description or '',
                    'camera_settings': photo.camera_settings or '',
                })

        self.stdout.write(self.style.SUCCESS(f'Exported correction template for {photos.count()} photo(s) to {options["output_csv"]}'))
