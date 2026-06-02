from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Avg, Count, StdDev
from django.template.loader import render_to_string

from .models import Score


def send_automated_email(
    competition,
    subject,
    template_name,
    context,
    recipient_list,
    from_email=None,
    fail_silently=False,
    html_template_name=None,
):
    if not competition.emails_enabled:
        return False

    email_context = {
        **(context or {}),
        'competition': competition,
    }
    message = render_to_string(template_name, email_context).strip()
    html_message = None
    if html_template_name:
        html_message = render_to_string(html_template_name, email_context)

    return send_mail(
        subject,
        message,
        from_email or getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        recipient_list,
        fail_silently=fail_silently,
        html_message=html_message,
    )


def calculate_judge_calibration(competition_id):
    scores = Score.objects.filter(photo__competition_id=competition_id)
    overall_stats = scores.aggregate(
        average=Avg('total_score'),
        standard_deviation=StdDev('total_score'),
        score_count=Count('id'),
    )
    overall_average = overall_stats['average']
    standard_deviation = overall_stats['standard_deviation'] or 0

    if overall_average is None:
        return {
            'competition_id': competition_id,
            'overall_average': None,
            'standard_deviation': None,
            'threshold': None,
            'judges': [],
            'flagged_judges': [],
        }

    threshold = standard_deviation * 1.5
    judge_rows = (
        scores.values('judge_id', 'judge__username', 'judge__first_name', 'judge__last_name')
        .annotate(average=Avg('total_score'), score_count=Count('id'))
        .order_by('average')
    )

    judges = []
    flagged_judges = []
    for row in judge_rows:
        judge_average = row['average'] or 0
        deviation = judge_average - overall_average
        z_score = deviation / standard_deviation if standard_deviation else 0
        is_flagged = bool(standard_deviation and abs(deviation) > threshold)
        judge_name = ' '.join(
            part for part in [row['judge__first_name'], row['judge__last_name']] if part
        ).strip() or row['judge__username']
        direction = ''
        if is_flagged:
            direction = 'lenient' if deviation > 0 else 'harsh'

        judge_result = {
            'judge_id': row['judge_id'],
            'judge_username': row['judge__username'],
            'judge_name': judge_name,
            'average': judge_average,
            'score_count': row['score_count'],
            'deviation': deviation,
            'z_score': z_score,
            'is_flagged': is_flagged,
            'direction': direction,
        }
        judges.append(judge_result)
        if is_flagged:
            flagged_judges.append(judge_result)

    return {
        'competition_id': competition_id,
        'overall_average': overall_average,
        'standard_deviation': standard_deviation,
        'threshold': threshold,
        'score_count': overall_stats['score_count'],
        'judges': judges,
        'flagged_judges': flagged_judges,
    }
