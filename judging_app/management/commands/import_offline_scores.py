import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from judging_app.models import Competition, RubricCriterion, Score
from judging_app.views import judging_photo_queryset


class Command(BaseCommand):
    help = 'Load a scores JSON file exported from an offline judging pack.'

    def add_arguments(self, parser):
        parser.add_argument('scores_json')
        parser.add_argument('--overwrite', action='store_true', help='Replace scores the judge already has online.')
        parser.add_argument('--dry-run', action='store_true', help='Report what would change without saving.')

    def handle(self, *args, **options):
        try:
            with open(options['scores_json'], encoding='utf-8-sig') as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read the scores file ({type(exc).__name__}).')
        if not isinstance(data, dict) or data.get('format') != 'simplyjudge-offline-scores':
            raise CommandError('This is not an offline scores file.')

        competition = Competition.objects.filter(slug=data.get('competition_slug')).first()
        if competition is None:
            raise CommandError(f'Competition not found: {data.get("competition_slug")}')
        judge = get_user_model().objects.filter(username=data.get('judge_username')).first()
        if judge is None:
            raise CommandError(f'User not found: {data.get("judge_username")}')

        rubric = {str(criterion.id): criterion for criterion in RubricCriterion.objects.filter(competition=competition)}
        allowed_photos = {photo.id: photo for photo in judging_photo_queryset(competition, judge)}

        created = updated = skipped_existing = skipped_invalid = 0
        for item in data.get('scores', []):
            photo = allowed_photos.get(item.get('photo_id'))
            if photo is None:
                skipped_invalid += 1
                self.stdout.write(self.style.WARNING(f'  Skipping unknown photo {item.get("photo_id")}'))
                continue

            criteria_scores = {}
            total_score = 0.0
            for criterion_id, raw_value in (item.get('criteria_scores') or {}).items():
                criterion = rubric.get(str(criterion_id))
                if criterion is None:
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                value = max(0.0, min(value, float(criterion.score_out_of)))
                criteria_scores[str(criterion.id)] = value
                total_score += value * criterion.weight

            existing = Score.objects.filter(photo=photo, judge=judge).first()
            if existing and not options['overwrite']:
                skipped_existing += 1
                continue
            if not options['dry_run']:
                Score.objects.update_or_create(
                    photo=photo,
                    judge=judge,
                    defaults={
                        'criteria_scores': criteria_scores,
                        'total_score': total_score,
                        'comment': str(item.get('comment') or ''),
                    },
                )
            if existing:
                updated += 1
            else:
                created += 1

        prefix = '[dry run] ' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{judge.username} on {competition.slug}: {created} created, {updated} replaced, '
            f'{skipped_existing} skipped (already scored; use --overwrite), {skipped_invalid} invalid.'
        ))
