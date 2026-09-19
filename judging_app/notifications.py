import logging

from django.conf import settings
from django.db.models import Q
from django.urls import reverse

from .models import (
    Competition,
    CompetitionMembership,
    JudgeProgressNotification,
    Photo,
    PhotoStatusVote,
    RoundOneScore,
    Score,
)
from .utils import send_automated_email

logger = logging.getLogger(__name__)


def organizer_email_recipients(competition):
    return list(
        CompetitionMembership.objects.filter(
            competition=competition,
            role=CompetitionMembership.Role.ORGANIZER,
            is_active=True,
            user__is_active=True,
        )
        .exclude(user__email='')
        .values_list('user__email', flat=True)
        .distinct()
    )


def judge_stage_progress(competition, judge, stage):
    photos = Photo.objects.filter(competition=competition)

    if stage == JudgeProgressNotification.Stage.TRIAGE:
        eligible_photos = photos.exclude(rule_flags__icontains='No matching image file found')
        total = eligible_photos.count()
        completed = PhotoStatusVote.objects.filter(
            photo__in=eligible_photos,
            voter=judge,
        ).count()
    elif stage == JudgeProgressNotification.Stage.ROUND_1:
        eligible_photos = photos.filter(
            Q(status__in=[Photo.Status.ROUND_1, Photo.Status.SHORTLISTED])
            | Q(round_1_scores__isnull=False)
        ).distinct()
        total = eligible_photos.count()
        completed = RoundOneScore.objects.filter(
            photo__in=eligible_photos,
            judge=judge,
        ).count()
    else:
        if competition.workflow == Competition.Workflow.FEEDBACK_PORTAL:
            eligible_photos = photos
        else:
            eligible_photos = photos.filter(status=Photo.Status.SHORTLISTED)
        total = eligible_photos.count()
        completed = Score.objects.filter(
            photo__in=eligible_photos,
            judge=judge,
        ).count()

    return completed, total


def send_judge_progress_notifications(competition, judge, stage):
    if not competition.emails_enabled:
        return []

    recipients = organizer_email_recipients(competition)
    if not recipients:
        return []

    completed, total = judge_stage_progress(competition, judge, stage)
    if not total or not completed:
        return []

    if completed >= total:
        milestones = [JudgeProgressNotification.Milestone.FINISHED]
    elif completed == 1:
        milestones = [JudgeProgressNotification.Milestone.STARTED]
    else:
        milestones = []

    sent_milestones = []
    for milestone in milestones:
        notification, created = JudgeProgressNotification.objects.get_or_create(
            competition=competition,
            judge=judge,
            stage=stage,
            milestone=milestone,
        )
        if not created:
            continue

        stage_label = notification.get_stage_display()
        milestone_label = notification.get_milestone_display().lower()
        judge_name = judge.get_full_name() or judge.username
        progress_path = reverse('competition_progress', args=[competition.slug])
        progress_url = f"{getattr(settings, 'SITE_URL', '').rstrip('/')}{progress_path}"
        try:
            result = send_automated_email(
                competition=competition,
                subject=f'{competition.name}: {judge_name} {milestone_label} {stage_label}',
                template_name='emails/judge_progress.txt',
                context={
                    'judge_name': judge_name,
                    'stage_label': stage_label,
                    'milestone_label': milestone_label,
                    'completed': completed,
                    'total': total,
                    'progress_url': progress_url,
                },
                recipient_list=recipients,
                fail_silently=True,
            )
        except Exception:
            notification.delete()
            logger.exception(
                'Judge progress email failed for competition #%s, judge #%s, stage %s.',
                competition.id,
                judge.id,
                stage,
            )
            continue

        if result:
            sent_milestones.append(milestone)
        else:
            notification.delete()
            logger.warning(
                'Judge progress email was not sent for competition #%s, judge #%s, stage %s.',
                competition.id,
                judge.id,
                stage,
            )

    return sent_milestones
