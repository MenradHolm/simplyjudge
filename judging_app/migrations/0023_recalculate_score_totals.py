from django.db import migrations


def recalculate_score_totals(apps, schema_editor):
    Score = apps.get_model('judging_app', 'Score')
    RubricCriterion = apps.get_model('judging_app', 'RubricCriterion')

    criteria_by_competition = {}
    for criterion in RubricCriterion.objects.all():
        criteria_by_competition.setdefault(criterion.competition_id, {})[str(criterion.id)] = criterion

    for score in Score.objects.select_related('photo'):
        if not score.criteria_scores:
            continue

        rubric = criteria_by_competition.get(score.photo.competition_id, {})
        if not rubric:
            continue

        total = 0.0
        for criterion_id, raw_value in score.criteria_scores.items():
            criterion = rubric.get(str(criterion_id))
            if criterion is None:
                continue
            try:
                score_value = float(raw_value)
            except (TypeError, ValueError):
                score_value = 0.0
            score_value = max(0.0, min(score_value, float(criterion.score_out_of)))
            total += score_value * float(criterion.weight)

        score.total_score = total
        score.save(update_fields=['total_score'])


class Migration(migrations.Migration):

    dependencies = [
        ('judging_app', '0022_rubriccriterion_score_out_of'),
    ]

    operations = [
        migrations.RunPython(recalculate_score_totals, migrations.RunPython.noop),
    ]
