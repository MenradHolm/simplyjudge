import csv
import gc
import json
import math
import os
import re
import tempfile
import uuid
import zipfile
import io
import threading
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from PIL import Image
from PIL.ExifTags import TAGS
from PIL import ImageOps

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import close_old_connections, transaction
from django.db.models import Avg
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import Competition, CompetitionMembership, EntryOrder, Photo, PhotoStatusVote, RoundOneScore, Score, RubricCriterion, ZipImportJob

try:
    import stripe
except ImportError:
    stripe = None

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
CLOUDINARY_FREE_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
CLOUDINARY_SAFE_UPLOAD_TARGET_BYTES = int(CLOUDINARY_FREE_UPLOAD_LIMIT_BYTES * 0.92)
MAX_IMAGE_PROCESSING_DIMENSION = 2400
PENDING_JUDGE_INVITE_SESSION_KEY = 'pending_judge_invite_token'

def grant_judge_invite_access(user, competition):
    competition.judges.add(user)
    CompetitionMembership.objects.update_or_create(
        competition=competition,
        user=user,
        role=CompetitionMembership.Role.VIP_JUDGE,
        defaults={'is_active': True},
    )

def apply_pending_judge_invite(request):
    token = request.session.pop(PENDING_JUDGE_INVITE_SESSION_KEY, None)
    if not token or not request.user.is_authenticated:
        return None
    competition = Competition.objects.filter(judge_invite_token=token).first()
    if competition:
        grant_judge_invite_access(request.user, competition)
    return competition

def register_user(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            invited_competition = apply_pending_judge_invite(request)
            if invited_competition:
                messages.success(request, f'You have joined {invited_competition.name} as a judge.')
                return redirect('judge_router', comp_slug=invited_competition.slug)
            return redirect('home_hub')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

def accept_judge_invite(request, token):
    competition = get_object_or_404(Competition, judge_invite_token=token, is_active=True)
    request.session[PENDING_JUDGE_INVITE_SESSION_KEY] = str(token)

    if request.user.is_authenticated:
        grant_judge_invite_access(request.user, competition)
        request.session.pop(PENDING_JUDGE_INVITE_SESSION_KEY, None)
        messages.success(request, f'You have joined {competition.name} as a judge.')
        return redirect('judge_router', comp_slug=competition.slug)

    login_url = f"{reverse('login')}?next={request.path}"
    return redirect(login_url)

ORGANIZER_ROLES = {CompetitionMembership.Role.ORGANIZER}
INTERNAL_REVIEW_ROLES = {
    CompetitionMembership.Role.INTERNAL_JUDGE,
}
VIP_JUDGE_ROLES = {CompetitionMembership.Role.VIP_JUDGE}
COMPETITION_MEMBER_ROLES = {
    CompetitionMembership.Role.ORGANIZER,
    CompetitionMembership.Role.INTERNAL_JUDGE,
    CompetitionMembership.Role.VIP_JUDGE,
}

def has_competition_role(user, competition, roles):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return CompetitionMembership.objects.filter(
        user=user,
        competition=competition,
        role__in=roles,
        is_active=True,
    ).exists()

def is_competition_member(user, competition):
    return has_competition_role(user, competition, COMPETITION_MEMBER_ROLES)

def is_competition_organizer(user, competition):
    return has_competition_role(user, competition, ORGANIZER_ROLES)

def is_internal_reviewer(user, competition):
    return has_competition_role(user, competition, INTERNAL_REVIEW_ROLES)

def is_approved_judge(user, competition):
    if has_competition_role(user, competition, VIP_JUDGE_ROLES):
        return True
    if (
        competition.workflow == Competition.Workflow.FEEDBACK_PORTAL
        and has_competition_role(user, competition, INTERNAL_REVIEW_ROLES)
    ):
        return True
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return competition.judges.filter(id=user.id).exists()

def competition_role_for_user(user, competition):
    if user.is_superuser:
        return 'Platform admin'
    membership = CompetitionMembership.objects.filter(
        user=user,
        competition=competition,
        is_active=True,
    ).order_by('role').first()
    return membership.get_role_display() if membership else ''

def is_full_competition(competition):
    return competition.workflow == Competition.Workflow.FULL_COMPETITION

def is_feedback_portal(competition):
    return competition.workflow == Competition.Workflow.FEEDBACK_PORTAL

def is_youth_poty_competition(competition):
    identity = normalize_match_key(f'{competition.slug or ""} {competition.name or ""}')
    return any(
        marker in identity
        for marker in ('youthpoty', 'ypoty', 'youthphotographeroftheyear')
    )

def should_run_youth_poty_rule_review(competition):
    return is_full_competition(competition) and is_youth_poty_competition(competition)

def collect_photo_rule_flags(competition, image_bytes):
    if not should_run_youth_poty_rule_review(competition):
        return []
    return audit_photo_metadata(image_bytes)['flags']

def anonymize_camera_settings(value):
    value = str(value or '').strip()
    if not value:
        return ''

    cleaned_blocks = []
    for block in re.split(r'\n\s*\n', value):
        cleaned = block.strip()
        label_match = re.match(
            r'camera settings?\s*\d{0,2}\s*\[(?P<label>[^\]]+)\]',
            cleaned,
            flags=re.IGNORECASE,
        )

        if label_match:
            cleaned = label_match.group('label').strip()
            if ' - ' in cleaned:
                cleaned = cleaned.split(' - ', 1)[1].strip()
        else:
            cleaned = re.sub(
                r'^camera settings?\s*\d{1,2}\s*[:.)-]\s*',
                '',
                cleaned,
                flags=re.IGNORECASE,
            ).strip()

        cleaned = re.sub(
            r'\s*images? shot with these settings:\s*nr\s*\d{1,2}\s*$',
            '',
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(r'\s*\bnr\s*\d{1,2}\s*$', '', cleaned, flags=re.IGNORECASE).strip(' -:;')
        if cleaned:
            cleaned_blocks.append(cleaned)

    return '\n\n'.join(cleaned_blocks)

def anonymize_photo_title(value, fallback='Untitled entry'):
    cleaned = strip_number_prefix(value)
    if not cleaned or cleaned in {'-', 'â€”'}:
        return fallback

    basename = os.path.basename(cleaned)
    stem, ext = os.path.splitext(basename)
    if ext.lower() in IMAGE_EXTENSIONS:
        cleaned = stem or fallback

    if '__' in cleaned:
        cleaned = cleaned.rsplit('__', 1)[1].strip()
    elif '_' in cleaned:
        parts = [part.strip() for part in re.split(r'_+', cleaned) if part.strip()]
        if len(parts) >= 3 and re.fullmatch(r'[A-Z]{2,3}', parts[0]):
            cleaned = ' '.join(parts[2:]).strip()
        elif len(parts) >= 2 and re.fullmatch(r'[A-Z]{2,3}', parts[0]):
            cleaned = ' '.join(parts[1:]).strip()

    if cleaned and ' ' not in cleaned and re.search(r'[a-z][A-Z]', cleaned):
        cleaned = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', cleaned)

    return cleaned or fallback

def judging_photo_queryset(competition, user):
    queryset = Photo.objects.filter(competition=competition).order_by('entry_code', 'id')
    if user.is_superuser or is_feedback_portal(competition):
        return queryset
    return queryset.filter(status=Photo.Status.SHORTLISTED)

def photo_report_queryset(competition):
    return Photo.objects.filter(competition=competition).order_by('entry_code', 'id')

def photos_missing_imported_image_queryset(competition):
    return Photo.objects.filter(
        competition=competition,
        rule_flags__icontains='No matching image file found',
    )

def triage_photo_queryset(competition):
    return Photo.objects.filter(competition=competition).exclude(
        rule_flags__icontains='No matching image file found',
    )

def rubric_max_score(rubric):
    return sum(float(criterion.score_out_of) * float(criterion.weight) for criterion in rubric)

def score_raw_total(score, rubric_by_id):
    if not rubric_by_id or not score.criteria_scores:
        return float(score.total_score or 0)

    raw_total = 0.0
    for criterion_id, raw_value in score.criteria_scores.items():
        criterion = rubric_by_id.get(str(criterion_id))
        if criterion is None:
            continue
        try:
            score_value = float(raw_value)
        except (TypeError, ValueError):
            score_value = 0.0
        score_value = max(0.0, min(score_value, float(criterion.score_out_of)))
        raw_total += score_value * float(criterion.weight)
    return raw_total

def attach_score_display_values(scores, rubric, max_score):
    rubric_by_id = {str(criterion.id): criterion for criterion in rubric}
    for score in scores:
        score.display_total = score_raw_total(score, rubric_by_id)
        score.display_percentage = (score.display_total / max_score * 100) if max_score else None
        score.rubric_breakdown = []
        for criterion in rubric:
            raw_value = score.criteria_scores.get(str(criterion.id)) if score.criteria_scores else None
            if raw_value in (None, ''):
                continue
            try:
                criterion_score = float(raw_value)
            except (TypeError, ValueError):
                continue
            criterion_score = max(0.0, min(criterion_score, float(criterion.score_out_of)))
            score.rubric_breakdown.append({
                'name': criterion.name,
                'score': criterion_score,
                'max_score': float(criterion.score_out_of),
            })
    return scores

def attach_photo_average_values(photos, scores, max_score):
    scores_by_photo = {}
    for score in scores:
        scores_by_photo.setdefault(score.photo_id, []).append(score)

    for photo in photos:
        photo.judge_scores = scores_by_photo.get(photo.id, [])
        if photo.judge_scores:
            photo.average_score = sum(score.display_total for score in photo.judge_scores) / len(photo.judge_scores)
            photo.average_percentage = (photo.average_score / max_score * 100) if max_score else None
        else:
            photo.average_score = None
            photo.average_percentage = None
        photo.max_score = max_score
    return photos

def home_hub(request):
    active_competitions = Competition.objects.filter(is_active=True).order_by('-created_at')
    if request.user.is_authenticated and not request.user.is_superuser:
        active_competitions = active_competitions.filter(
            memberships__user=request.user,
            memberships__is_active=True,
        ).distinct()
    for competition in active_competitions:
        competition.user_role = competition_role_for_user(request.user, competition) if request.user.is_authenticated else ''
        competition.can_manage = is_competition_organizer(request.user, competition)
        competition.can_review = is_full_competition(competition) and is_internal_reviewer(request.user, competition)
        competition.can_judge = is_approved_judge(request.user, competition)
        competition.can_finalize = is_full_competition(competition) and competition.can_manage
        competition.start_label = 'Review photos' if is_feedback_portal(competition) else 'Start judging'
    return render(request, 'judging_app/home.html', {'competitions': active_competitions})

def internal_review_panel_count(competition):
    return CompetitionMembership.objects.filter(
        competition=competition,
        role__in=INTERNAL_REVIEW_ROLES,
        is_active=True,
        user__is_active=True,
    ).values('user').distinct().count()

def majority_threshold(panel_size):
    return (panel_size // 2) + 1 if panel_size else 1

def apply_status_vote_majority(photo):
    panel_size = internal_review_panel_count(photo.competition)
    threshold = majority_threshold(panel_size)
    round_1_votes = photo.status_votes.filter(decision=PhotoStatusVote.Decision.ROUND_1).count()
    reject_votes = photo.status_votes.filter(decision=PhotoStatusVote.Decision.REJECT).count()

    if round_1_votes >= threshold:
        photo.status = Photo.Status.ROUND_1
        photo.save(update_fields=['status'])
    elif reject_votes >= threshold:
        photo.status = Photo.Status.REJECTED
        photo.save(update_fields=['status'])

    return {
        'panel_size': panel_size,
        'threshold': threshold,
        'round_1_votes': round_1_votes,
        'reject_votes': reject_votes,
    }

@login_required(login_url='/accounts/login/')
def judge_router(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    photo_queue = judging_photo_queryset(competition, request.user)
    next_photo = photo_queue.exclude(score__judge=request.user).first()
    if next_photo:
        return redirect('judge_photo', comp_slug=competition.slug, photo_id=next_photo.id)
    return render(request, 'judging_app/done.html', {'competition': competition})

@login_required(login_url='/accounts/login/')
def judge_review(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')

    visible_photos = judging_photo_queryset(competition, request.user)
    submitted_scores = (
        Score.objects
        .filter(judge=request.user, photo__in=visible_photos)
        .select_related('photo')
        .order_by('photo__id')
    )
    remaining_count = visible_photos.exclude(score__judge=request.user).count()
    return render(
        request,
        'judging_app/judge_review.html',
        {
            'competition': competition,
            'scores': submitted_scores,
            'remaining_count': remaining_count,
        },
    )

@login_required(login_url='/accounts/login/')
def elimination_mode(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_full_competition(competition):
        return redirect('home_hub')
    if not is_internal_reviewer(request.user, competition):
        return redirect('home_hub')

    if request.method == 'POST':
        photo_id = request.POST.get('photo_id', '')
        decision = request.POST.get('decision')
        if not photo_id.isdigit() or decision not in {'reject', 'round_1'}:
            return redirect('elimination_mode', comp_slug=competition.slug)

        with transaction.atomic():
            photo = get_object_or_404(
                triage_photo_queryset(competition).select_for_update(),
                id=int(photo_id),
                status=Photo.Status.PENDING,
            )
            vote_decision = (
                PhotoStatusVote.Decision.REJECT
                if decision == 'reject'
                else PhotoStatusVote.Decision.ROUND_1
            )
            PhotoStatusVote.objects.update_or_create(
                photo=photo,
                voter=request.user,
                defaults={'decision': vote_decision},
            )
            apply_status_vote_majority(photo)
        return redirect('elimination_mode', comp_slug=competition.slug)

    pending_triage_photos = triage_photo_queryset(competition).filter(
        status=Photo.Status.PENDING,
    )
    current_photo = pending_triage_photos.exclude(status_votes__voter=request.user).order_by('id').first()
    counts = {
        'pending': pending_triage_photos.count(),
        'round_1': Photo.objects.filter(competition=competition, status=Photo.Status.ROUND_1).count(),
        'shortlisted': Photo.objects.filter(competition=competition, status=Photo.Status.SHORTLISTED).count(),
        'rejected': Photo.objects.filter(competition=competition, status=Photo.Status.REJECTED).count(),
        'for_you': pending_triage_photos.exclude(status_votes__voter=request.user).count(),
        'missing_images': photos_missing_imported_image_queryset(competition).filter(status=Photo.Status.PENDING).count(),
    }
    vote_summary = None
    if current_photo:
        panel_size = internal_review_panel_count(competition)
        vote_summary = {
            'panel_size': panel_size,
            'threshold': majority_threshold(panel_size),
            'round_1_votes': current_photo.status_votes.filter(decision=PhotoStatusVote.Decision.ROUND_1).count(),
            'reject_votes': current_photo.status_votes.filter(decision=PhotoStatusVote.Decision.REJECT).count(),
        }
    return render(
        request,
        'judging_app/elimination_mode.html',
        {
            'competition': competition,
            'photo': current_photo,
            'counts': counts,
            'vote_summary': vote_summary,
        },
    )

@login_required(login_url='/accounts/login/')
def round_1_review(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_full_competition(competition):
        return redirect('home_hub')
    if not is_internal_reviewer(request.user, competition):
        return redirect('home_hub')

    if request.method == 'POST':
        photo_id = request.POST.get('photo_id', '')
        raw_score = request.POST.get('score', '')
        try:
            score = int(raw_score)
        except ValueError:
            score = 0

        if photo_id.isdigit() and 1 <= score <= 10:
            photo = get_object_or_404(
                Photo,
                id=int(photo_id),
                competition=competition,
                status=Photo.Status.ROUND_1,
            )
            RoundOneScore.objects.update_or_create(
                photo=photo,
                judge=request.user,
                defaults={'score': score},
            )
        return redirect('round_1_review', comp_slug=competition.slug)

    unscored_photos = Photo.objects.filter(
        competition=competition,
        status=Photo.Status.ROUND_1,
    ).exclude(round_1_scores__judge=request.user).order_by('id')
    unscored_photo_ids = list(unscored_photos.values_list('id', flat=True))
    requested_photo_id = request.GET.get('photo_id', '')
    current_photo = None
    if requested_photo_id.isdigit() and int(requested_photo_id) in unscored_photo_ids:
        current_photo = unscored_photos.filter(id=int(requested_photo_id)).first()
    if current_photo is None:
        current_photo = unscored_photos.first()

    previous_photo_id = None
    next_photo_id = None
    if current_photo and len(unscored_photo_ids) > 1:
        current_position = unscored_photo_ids.index(current_photo.id)
        if current_position > 0:
            previous_photo_id = unscored_photo_ids[current_position - 1]
        if current_position < len(unscored_photo_ids) - 1:
            next_photo_id = unscored_photo_ids[current_position + 1]

    counts = {
        'for_you': len(unscored_photo_ids),
        'round_1': Photo.objects.filter(competition=competition, status=Photo.Status.ROUND_1).count(),
        'shortlisted': Photo.objects.filter(competition=competition, status=Photo.Status.SHORTLISTED).count(),
    }
    return render(
        request,
        'judging_app/round_1_review.html',
        {
            'competition': competition,
            'photo': current_photo,
            'counts': counts,
            'score_range': range(1, 11),
            'anonymous_camera_settings': anonymize_camera_settings(current_photo.camera_settings) if current_photo else '',
            'anonymous_photo_title': anonymize_photo_title(current_photo.title) if current_photo else '',
            'previous_photo_id': previous_photo_id,
            'next_photo_id': next_photo_id,
        },
    )

@login_required(login_url='/accounts/login/')
def finalize_shortlist(request, comp_slug):
    if request.method != 'POST':
        return redirect('home_hub')

    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_full_competition(competition):
        return redirect('home_hub')
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')
    scored_round_1 = list(
        Photo.objects.filter(competition=competition, status=Photo.Status.ROUND_1)
        .annotate(round_1_average=Avg('round_1_scores__score'))
        .filter(round_1_average__isnull=False)
        .order_by('-round_1_average', 'id')
    )
    promote_count = math.ceil(len(scored_round_1) * 0.1)
    selected = scored_round_1[:promote_count]
    selected_ids = [photo.id for photo in selected]

    if selected_ids:
        Photo.objects.filter(id__in=selected_ids, competition=competition).update(status=Photo.Status.SHORTLISTED)
        messages.success(request, f'Finalized {len(selected_ids)} Round 1 photo(s) for the VIP shortlist.')
    else:
        messages.error(request, 'No scored Round 1 photos are ready for shortlist finalization yet.')

    return redirect('home_hub')

@login_required(login_url='/accounts/login/')
def judge_photo(request, comp_slug, photo_id):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    return_to = request.POST.get('return_to') or request.GET.get('return')
    photo_queryset = judging_photo_queryset(competition, request.user).filter(id=photo_id)
    photo = get_object_or_404(photo_queryset)
    rubric = list(RubricCriterion.objects.filter(competition=competition))
    visible_photos = judging_photo_queryset(competition, request.user)
    total_photos = visible_photos.count()
    visible_photo_ids = list(visible_photos.values_list('id', flat=True))
    try:
        photo_position = visible_photo_ids.index(photo.id)
    except ValueError:
        photo_position = 0
    previous_photo_id = None
    next_photo_id = None
    if len(visible_photo_ids) > 1:
        previous_photo_id = visible_photo_ids[photo_position - 1]
        next_photo_id = visible_photo_ids[(photo_position + 1) % len(visible_photo_ids)]
    scored_query = Score.objects.filter(photo__competition=competition, judge=request.user)
    if not request.user.is_superuser and is_full_competition(competition):
        scored_query = scored_query.filter(photo__status=Photo.Status.SHORTLISTED)
    scored_photos = scored_query.count()
    existing_score = Score.objects.filter(photo=photo, judge=request.user).first()
    existing_criteria_scores = existing_score.criteria_scores if existing_score else {}
    for criterion in rubric:
        criterion.saved_score = existing_criteria_scores.get(str(criterion.id), '')
    
    if request.method == "POST":
        criteria_scores = {}
        total_score = 0.0
        for criterion in rubric:
            val = request.POST.get(f'criterion_{criterion.id}', 0)
            try:
                score_val = float(val)
            except ValueError:
                score_val = 0.0
            score_val = max(0.0, min(score_val, float(criterion.score_out_of)))
            criteria_scores[str(criterion.id)] = score_val
            total_score += (score_val * criterion.weight)
        comment = request.POST.get('comment', '')
        Score.objects.update_or_create(
            photo=photo,
            judge=request.user,
            defaults={'criteria_scores': criteria_scores, 'total_score': total_score, 'comment': comment}
        )
        if return_to == 'review':
            return redirect('judge_review', comp_slug=competition.slug)
        return redirect('judge_router', comp_slug=competition.slug)

    current_index = photo_position + 1 if visible_photo_ids else 1
    current_index = min(max(current_index, 1), total_photos or 1)
    context = {
        'competition': competition,
        'photo': photo,
        'rubric': rubric,
        'progress': f"{current_index} / {total_photos}",
        'current_index': current_index,
        'total_photos': total_photos,
        'previous_photo_id': previous_photo_id,
        'next_photo_id': next_photo_id,
        'existing_score': existing_score,
        'return_to': return_to,
        'anonymous_camera_settings': anonymize_camera_settings(photo.camera_settings),
        'anonymous_photo_title': anonymize_photo_title(photo.title),
    }
    return render(request, 'judging_app/judge.html', context)

@login_required(login_url='/accounts/login/')
def autosave_judge_score(request, comp_slug, photo_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Autosave requires POST.'}, status=405)

    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return JsonResponse({'error': 'Judging access required.'}, status=403)

    photo_queryset = judging_photo_queryset(competition, request.user).filter(id=photo_id)
    photo = get_object_or_404(photo_queryset)
    rubric = list(RubricCriterion.objects.filter(competition=competition))
    criteria_scores = {}
    total_score = 0.0

    for criterion in rubric:
        raw_value = request.POST.get(f'criterion_{criterion.id}', '')
        if raw_value in (None, ''):
            continue
        try:
            score_val = float(raw_value)
        except (TypeError, ValueError):
            continue
        score_val = max(0.0, min(score_val, float(criterion.score_out_of)))
        criteria_scores[str(criterion.id)] = score_val
        total_score += (score_val * criterion.weight)

    comment = request.POST.get('comment', '')
    score, _created = Score.objects.update_or_create(
        photo=photo,
        judge=request.user,
        defaults={
            'criteria_scores': criteria_scores,
            'total_score': total_score,
            'comment': comment,
        },
    )
    return JsonResponse(
        {
            'ok': True,
            'score_id': score.id,
            'total_score': total_score,
            'saved_at': timezone.now().isoformat(),
        }
    )

def leaderboard(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    tie_criterion = competition.tie_breaker_criterion
    rubric = list(RubricCriterion.objects.filter(competition=competition))
    max_score = rubric_max_score(rubric)
    scores = list(Score.objects.filter(photo__competition=competition).select_related('judge'))
    attach_score_display_values(scores, rubric, max_score)
    photos = list(Photo.objects.filter(competition=competition))
    attach_photo_average_values(photos, scores, max_score)
    ranked_photos = [photo for photo in photos if photo.average_score is not None]
    if tie_criterion:
        for photo in ranked_photos:
            tie_scores = []
            for score in photo.judge_scores:
                try:
                    tie_scores.append(float(score.criteria_scores.get(str(tie_criterion.id), 0)))
                except (TypeError, ValueError):
                    tie_scores.append(0.0)
            photo.tie_breaker_score = sum(tie_scores) / len(tie_scores) if tie_scores else 0.0
        ranked_photos.sort(key=lambda photo: (photo.average_score, photo.tie_breaker_score), reverse=True)
    else:
        ranked_photos.sort(key=lambda photo: photo.average_score, reverse=True)
    return render(
        request,
        'judging_app/leaderboard.html',
        {
            'competition': competition,
            'photos': ranked_photos,
            'tie_criterion': tie_criterion,
            'max_score': max_score,
        },
    )

def submit_photo(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    error_message = None
    if request.method == "POST":
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
        photographer_email = request.POST.get('photographer_email', '')
        category = request.POST.get('category')
        image = request.FILES.get('image')
        description = request.POST.get('description', '')
        camera_settings = request.POST.get('camera_settings', '')
        if title and photographer_name and category and image:
            if image.size > 5 * 1024 * 1024:
                error_message = "The uploaded file is too large! Please keep your photo under 5 MB."
            else:
                Photo.objects.create(
                    competition=competition, title=title, photographer_name=photographer_name,
                    photographer_email=photographer_email, category=category, image=image,
                    description=description, camera_settings=camera_settings
                )
                return render(request, 'judging_app/submit_success.html', {'competition': competition})
    return render(request, 'judging_app/submit.html', {'competition': competition, 'error_message': error_message})

def user_owns_photo_submission(user, photo):
    user_email = (getattr(user, 'email', '') or '').strip().lower()
    photographer_email = (photo.photographer_email or '').strip().lower()
    return bool(user.is_authenticated and user_email and photographer_email and user_email == photographer_email)

@login_required(login_url='/accounts/login/')
def upload_raw_file(request, comp_slug, photo_id):
    photo = get_object_or_404(Photo.objects.select_related('competition'), id=photo_id, competition__slug=comp_slug)

    if photo.status != Photo.Status.SHORTLISTED:
        raise PermissionDenied('RAW verification is only available for shortlisted finalist photos.')

    if not photo.competition.results_published:
        raise PermissionDenied('RAW verification is only available after results have been published.')

    if not user_owns_photo_submission(request.user, photo):
        raise PermissionDenied('You do not have permission to upload a RAW file for this photo.')

    if request.method == 'POST':
        raw_upload = request.FILES.get('raw_file')
        if not raw_upload:
            messages.error(request, 'Please choose a RAW file to upload.')
            return render(request, 'judging_app/upload_raw_file.html', {'photo': photo, 'competition': photo.competition})

        photo.raw_file = raw_upload
        photo.is_raw_verified = False
        photo.exif_warning_flag = ''
        photo.save(update_fields=['raw_file', 'is_raw_verified', 'exif_warning_flag'])
        messages.success(request, 'RAW file uploaded. Verification is pending.')
        return redirect('upload_raw_file', comp_slug=photo.competition.slug, photo_id=photo.id)

    return render(request, 'judging_app/upload_raw_file.html', {'photo': photo, 'competition': photo.competition})

@login_required(login_url='/accounts/login/')
def create_checkout_session(request, comp_slug):
    if request.method != 'POST':
        return redirect('submit_photo', comp_slug=comp_slug)

    competition = get_object_or_404(Competition, slug=comp_slug, is_active=True)
    entry_fee = competition.entry_fee or Decimal('0.00')

    if entry_fee <= 0:
        messages.info(request, 'This event does not require an entry payment.')
        return redirect('submit_photo', comp_slug=competition.slug)

    if stripe is None:
        messages.error(request, 'Stripe payments are not available on this server yet.')
        return redirect('submit_photo', comp_slug=competition.slug)

    stripe.api_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not stripe.api_key:
        messages.error(request, 'Stripe payments are not configured yet.')
        return redirect('submit_photo', comp_slug=competition.slug)

    amount_in_cents = int((entry_fee * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    currency = getattr(settings, 'STRIPE_CURRENCY', 'usd')
    submit_url = reverse('submit_photo', kwargs={'comp_slug': competition.slug})
    success_url = request.build_absolute_uri(
        f"{submit_url}?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = request.build_absolute_uri(f"{submit_url}?checkout=cancelled")

    try:
        checkout_session = stripe.checkout.Session.create(
            mode='payment',
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': currency,
                        'product_data': {
                            'name': f"{competition.name} entry",
                        },
                        'unit_amount': amount_in_cents,
                    },
                    'quantity': 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                'competition_id': str(competition.id),
                'competition_slug': competition.slug or '',
                'user_id': str(request.user.id),
            },
        )
    except Exception:
        messages.error(request, 'Could not start Stripe checkout. Please try again.')
        return redirect('submit_photo', comp_slug=competition.slug)

    EntryOrder.objects.create(
        user=request.user,
        competition=competition,
        stripe_checkout_id=checkout_session.id,
        amount_paid=entry_fee,
        is_paid=False,
    )
    return redirect(checkout_session.url)

@csrf_exempt
def stripe_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    payload = request.body
    webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')

    try:
        if webhook_secret:
            if stripe is None:
                return HttpResponse(status=503)
            signature = request.META.get('HTTP_STRIPE_SIGNATURE', '')
            event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
        else:
            event = json.loads(payload.decode('utf-8'))
    except ValueError:
        return HttpResponse(status=400)
    except Exception as exc:
        signature_error = getattr(getattr(stripe, 'error', None), 'SignatureVerificationError', None) if stripe else None
        if signature_error and isinstance(exc, signature_error):
            return HttpResponse(status=400)
        raise

    if event.get('type') == 'checkout.session.completed':
        session = event.get('data', {}).get('object', {})
        session_id = session.get('id')
        if not session_id:
            return HttpResponse(status=400)

        with transaction.atomic():
            order = (
                EntryOrder.objects.select_for_update()
                .select_related('user', 'competition')
                .filter(stripe_checkout_id=session_id)
                .first()
            )
            if order:
                if not order.is_paid:
                    order.is_paid = True
                    order.save(update_fields=['is_paid'])
                CompetitionMembership.objects.update_or_create(
                    competition=order.competition,
                    user=order.user,
                    role=CompetitionMembership.Role.ENTRANT,
                    defaults={'is_active': True},
                )

    return HttpResponse(status=200)

def feedback_report_context(competition, can_edit_notes=False):
    photos = list(photo_report_queryset(competition))
    rubric = list(RubricCriterion.objects.filter(competition=competition))
    max_score = rubric_max_score(rubric)
    all_scores = list(Score.objects.filter(photo__competition=competition).select_related('judge'))
    attach_score_display_values(all_scores, rubric, max_score)
    attach_photo_average_values(photos, all_scores, max_score)
    return {
        'competition': competition,
        'photos': photos,
        'can_edit_notes': can_edit_notes,
        'max_score': max_score,
    }

def feedback_report(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    can_edit_notes = is_competition_organizer(request.user, competition)
    if not is_feedback_portal(competition) and not can_edit_notes:
        return redirect('home_hub')
    return render(
        request,
        'judging_app/feedback_report.html',
        feedback_report_context(competition, can_edit_notes=can_edit_notes),
    )

def split_entrant_name(full_name):
    parts = str(full_name or '').strip().split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], ' '.join(parts[1:])

def judge_display_name(user):
    return user.get_full_name() or user.username or user.email or f'Judge {user.id}'

def score_report_thumbnail_url(photo, width=640, height=480):
    if not photo.image:
        return '/media/competition_photos/placeholder.jpg'
    try:
        image_url = photo.image.url
    except ValueError:
        return '/media/competition_photos/placeholder.jpg'

    if 'res.cloudinary.com' in image_url and '/upload/' in image_url:
        prefix, asset_path = image_url.split('/upload/', 1)
        transformation = f'c_fit,w_{width},h_{height},q_auto:good,f_auto'
        return f'{prefix}/upload/{transformation}/{asset_path}'

    return image_url

def competition_score_summary(competition):
    photos = list(Photo.objects.filter(competition=competition).order_by('category', 'title', 'id'))
    rubric = list(RubricCriterion.objects.filter(competition=competition))
    max_score = rubric_max_score(rubric)
    scores = list(
        Score.objects.filter(photo__competition=competition)
        .select_related('photo', 'judge')
        .order_by('judge__username', 'judge__id')
    )
    attach_score_display_values(scores, rubric, max_score)
    judges_by_id = {}
    scores_by_photo = {}

    for score in scores:
        judges_by_id[score.judge_id] = score.judge
        scores_by_photo.setdefault(score.photo_id, {})[score.judge_id] = score

    judges = sorted(
        judges_by_id.values(),
        key=lambda judge: (judge_display_name(judge).lower(), judge.id),
    )
    rows = []

    for photo in photos:
        photo_scores = scores_by_photo.get(photo.id, {})
        score_values = [score.display_total for score in photo_scores.values()]
        average_score = sum(score_values) / len(score_values) if score_values else None
        judge_cells = [
            {
                'judge': judge,
                'judge_name': judge_display_name(judge),
                'score': photo_scores.get(judge.id),
            }
            for judge in judges
        ]
        first_name, last_name = split_entrant_name(photo.photographer_name)
        rows.append({
            'photo': photo,
            'first_name': first_name,
            'last_name': last_name,
            'entrant_name': ' '.join(part for part in [first_name, last_name] if part),
            'average_score': average_score,
            'thumbnail_url': score_report_thumbnail_url(photo),
            'judge_cells': judge_cells,
            'score_rowspan': len(judge_cells) or 1,
        })

    return rows, judges, max_score

@login_required(login_url='/accounts/login/')
def export_competition_results_csv(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    rows, judges, max_score = competition_score_summary(competition)
    header = [
        'Entrant First Name',
        'Entrant Last Name',
        'Country',
        'Email',
        'Category/Section',
        'Image Title',
        'Total Score',
        'Final Status',
    ]
    for judge in judges:
        display_name = judge_display_name(judge)
        header.extend([f'{display_name} Score', f'{display_name} Feedback'])

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="competition_results.csv"'
    writer = csv.writer(response)
    writer.writerow(header)

    for row in rows:
        photo = row['photo']
        total_score = row['average_score']
        csv_row = [
            row['first_name'],
            row['last_name'],
            '',
            photo.photographer_email,
            photo.category,
            photo.title,
            f'{total_score:.2f}' if total_score is not None else '',
            photo.get_status_display(),
        ]
        for cell in row['judge_cells']:
            score = cell['score']
            csv_row.extend([
                f'{score.display_total:.2f}' if score else '',
                score.comment if score and score.comment else '',
            ])
        writer.writerow(csv_row)

    return response

@login_required(login_url='/accounts/login/')
def competition_score_summary_pdf(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    rows, judges, max_score = competition_score_summary(competition)
    return render(
        request,
        'judging_app/score_summary_pdf.html',
        {
            'competition': competition,
            'rows': rows,
            'judges': judges,
            'max_score': max_score,
            'generated_at': timezone.now(),
            'is_shareable_report': False,
        },
    )

@login_required(login_url='/accounts/login/')
def shareable_score_summary_pdf(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    rows, judges, max_score = competition_score_summary(competition)
    return render(
        request,
        'judging_app/score_summary_pdf.html',
        {
            'competition': competition,
            'rows': rows,
            'judges': judges,
            'max_score': max_score,
            'generated_at': timezone.now(),
            'is_shareable_report': True,
        },
    )

@login_required(login_url='/accounts/login/')
def upload_spreadsheet(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Error: This is not a CSV file!')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        try:
            file_data = decode_csv_bytes(csv_file.read()).splitlines()
            reader = csv.DictReader(file_data)
            headers = [h.lower().strip() for h in reader.fieldnames] if reader.fieldnames else []
            
            if 'criterion name' in headers or 'criterion' in headers:
                rubric_count = 0
                for row in reader:
                    name = row.get('Criterion Name') or row.get('criterion name') or row.get('Criterion') or row.get('criterion')
                    desc = row.get('Description') or row.get('description') or ''
                    weight_val = row.get('Weight') or row.get('weight') or '1.0'
                    score_out_of_val = (
                        row.get('Score Out Of') or row.get('score out of') or
                        row.get('Max Score') or row.get('max score') or
                        row.get('Out Of') or row.get('out of') or
                        '100'
                    )
                    
                    if not name:
                        continue
                        
                    RubricCriterion.objects.create(
                        competition=competition,
                        name=name.strip(),
                        description=desc.strip(),
                        weight=float(weight_val.strip()),
                        score_out_of=max(1, int(float(str(score_out_of_val).strip()))),
                    )
                    rubric_count += 1
                
                messages.success(request, f'Successfully built a dynamic {rubric_count}-column rubric matrix!')
                return redirect('home_hub')

            else:
                import_count = 0
                for row in reader:
                    title = row.get('Title') or row.get('title') or 'Untitled'
                    photographer = row.get('Photographer') or row.get('photographer') or 'Unknown'
                    photographer_email = (
                        row.get('Photographer Email') or row.get('photographer_email') or
                        row.get('Email') or row.get('email') or row.get('Email Address') or
                        row.get('email_address') or ''
                    )
                    category = row.get('Category') or row.get('category') or 'General'
                    custom_code = row.get('Code') or row.get('ID') or row.get('Number') or row.get('id')
                    desc = row.get('Description') or row.get('description') or row.get('Story') or row.get('story') or ''
                    
                    # Look for settings column
                    cam_settings = row.get('Camera Settings') or row.get('camera settings') or row.get('Settings') or row.get('settings') or ''

                    if not custom_code:
                        continue

                    Photo.objects.create(
                        id=int(custom_code.strip()), competition=competition, title=title,
                        entry_code=custom_code.strip(), photographer_name=photographer,
                        photographer_email=photographer_email, category=category,
                        image='competition_photos/placeholder.jpg', description=desc, camera_settings=cam_settings
                    )
                    import_count += 1
                
                messages.success(request, f'Successfully imported {import_count} entries into the judging queue!')
                return redirect('feedback_report', comp_slug=competition.slug)

        except Exception as e:
            messages.error(request, f'Error parsing spreadsheet data: {str(e)}')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

    recent_jobs = ZipImportJob.objects.filter(competition=competition)[:5]
    return render(request, 'judging_app/upload_spreadsheet.html', {'competition': competition, 'recent_jobs': recent_jobs})

@login_required(login_url='/accounts/login/')
def upload_photos_zip(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    import_mode = request.POST.get('zip_import_mode', 'spreadsheet_package')
    process_target = process_photos_only_zip_job if import_mode == 'filename_codes' else process_entry_zip_job
    photo_only_message = 'Photo-only import started. SimplyJudge will create entries from the sorted filenames.'

    if request.method == 'POST' and request.POST.get('zip_url'):
        zip_url = request.POST.get('zip_url', '').strip()
        parsed_url = urlparse(zip_url)
        if parsed_url.scheme not in {'http', 'https'}:
            messages.error(request, 'Please provide a direct http(s) download link to the ZIP package.')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        job = ZipImportJob.objects.create(
            competition=competition,
            uploaded_by=request.user,
            source_name=os.path.basename(parsed_url.path) or 'remote-package.zip',
            source_url=zip_url,
        )
        worker = threading.Thread(
            target=process_target,
            args=(job.id,),
            daemon=True,
        )
        worker.start()
        messages.success(
            request,
            photo_only_message
            if import_mode == 'filename_codes'
            else 'Remote ZIP sync job created. SimplyJudge will download and import it in the background.',
        )
        return redirect('zip_import_status', comp_slug=competition.slug, job_id=job.id)

    if request.method == 'POST' and request.FILES.get('zip_file'):
        zip_file = request.FILES['zip_file']
        if not zip_file.name.lower().endswith('.zip'):
            messages.error(request, 'Please upload a .zip package containing EntryForm.csv and the photo files.')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        job = ZipImportJob.objects.create(
            competition=competition,
            uploaded_by=request.user,
            source_name=zip_file.name,
        )
        job.temp_path = save_uploaded_zip_to_temp_file(zip_file, job.id)
        job.save(update_fields=['temp_path', 'updated_at'])

        worker = threading.Thread(
            target=process_target,
            args=(job.id,),
            daemon=True,
        )
        worker.start()
        messages.success(
            request,
            photo_only_message
            if import_mode == 'filename_codes'
            else 'ZIP sync job created. SimplyJudge is matching images and importing entries in the background.',
        )
        return redirect('zip_import_status', comp_slug=competition.slug, job_id=job.id)
    return redirect('upload_spreadsheet', comp_slug=competition.slug)

@login_required(login_url='/accounts/login/')
def upload_photos_only_zip(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')

    zip_file = request.FILES.get('photos_zip_file')
    zip_url = request.POST.get('photos_zip_url', '').strip()

    if request.method != 'POST':
        return redirect('upload_spreadsheet', comp_slug=competition.slug)

    if zip_url:
        parsed_url = urlparse(zip_url)
        if parsed_url.scheme not in {'http', 'https'}:
            messages.error(request, 'Please provide a direct http(s) download link to the photos ZIP.')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)
        job = ZipImportJob.objects.create(
            competition=competition,
            uploaded_by=request.user,
            source_name=os.path.basename(parsed_url.path) or 'photos-only-package.zip',
            source_url=zip_url,
        )
    elif zip_file:
        if not zip_file.name.lower().endswith('.zip'):
            messages.error(request, 'Please upload a .zip package containing photo files.')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)
        job = ZipImportJob.objects.create(
            competition=competition,
            uploaded_by=request.user,
            source_name=zip_file.name,
        )
        job.temp_path = save_uploaded_zip_to_temp_file(zip_file, job.id)
        job.save(update_fields=['temp_path', 'updated_at'])
    else:
        messages.error(request, 'Choose a photos ZIP file or paste a direct photos ZIP download link.')
        return redirect('upload_spreadsheet', comp_slug=competition.slug)

    worker = threading.Thread(
        target=process_photos_only_zip_job,
        args=(job.id,),
        daemon=True,
    )
    worker.start()
    messages.success(
        request,
        'Photo-only import started. SimplyJudge will create entries from the sorted filenames.',
    )
    return redirect('zip_import_status', comp_slug=competition.slug, job_id=job.id)

@login_required(login_url='/accounts/login/')
def upload_zip_chunk(request, comp_slug):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required.'}, status=405)

    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return JsonResponse({'error': 'Competition organizer access required.'}, status=403)
    chunk = request.FILES.get('chunk')
    upload_id = request.POST.get('upload_id', '')
    filename = request.POST.get('filename', 'upload.zip')
    import_mode = request.POST.get('zip_import_mode', 'spreadsheet_package')

    try:
        chunk_index = int(request.POST.get('chunk_index', '0'))
        total_chunks = int(request.POST.get('total_chunks', '0'))
    except ValueError:
        return JsonResponse({'error': 'Invalid chunk metadata.'}, status=400)

    if not chunk or not upload_id or total_chunks < 1 or chunk_index < 0 or chunk_index >= total_chunks:
        return JsonResponse({'error': 'Missing or invalid chunk upload data.'}, status=400)
    if not filename.lower().endswith('.zip'):
        return JsonResponse({'error': 'Please upload a .zip package.'}, status=400)

    chunk_dir = get_chunk_upload_dir(upload_id)
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, f'{chunk_index:06d}.part')
    with open(chunk_path, 'wb') as target:
        for piece in chunk.chunks():
            target.write(piece)

    received_chunks = len([name for name in os.listdir(chunk_dir) if name.endswith('.part')])
    if received_chunks < total_chunks:
        return JsonResponse({
            'status': 'receiving',
            'received_chunks': received_chunks,
            'total_chunks': total_chunks,
        })

    job = ZipImportJob.objects.create(
        competition=competition,
        uploaded_by=request.user,
        source_name=filename,
    )
    job.temp_path = assemble_chunked_zip(upload_id, filename, total_chunks, job.id)
    job.save(update_fields=['temp_path', 'updated_at'])

    worker = threading.Thread(
        target=process_photos_only_zip_job if import_mode == 'filename_codes' else process_entry_zip_job,
        args=(job.id,),
        daemon=True,
    )
    worker.start()

    return JsonResponse({
        'status': 'started',
        'job_id': job.id,
        'status_url': request.build_absolute_uri(
            redirect('zip_import_status', comp_slug=competition.slug, job_id=job.id).url
        ),
    })

@login_required(login_url='/accounts/login/')
def zip_import_status(request, comp_slug, job_id):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_competition_organizer(request.user, competition):
        return redirect('home_hub')
    job = get_object_or_404(ZipImportJob, id=job_id, competition=competition)
    context = {
        'competition': competition,
        'job': job,
        'can_start_triage': is_full_competition(competition) and is_internal_reviewer(request.user, competition),
        'can_start_feedback_review': is_feedback_portal(competition) and is_approved_judge(request.user, competition),
        'is_feedback_portal': is_feedback_portal(competition),
        'unmatched_images_count': max(job.processed_rows - job.matched_images, 0),
    }
    return render(request, 'judging_app/zip_import_status.html', context)

def public_results(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    return render(request, 'judging_app/feedback_report.html', feedback_report_context(competition))

def save_uploaded_zip_to_temp_file(uploaded_file, job_id):
    temp_dir = os.path.join(tempfile.gettempdir(), 'simplyjudge_zip_imports')
    os.makedirs(temp_dir, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(uploaded_file.name))
    target_path = os.path.join(temp_dir, f'{job_id}_{uuid.uuid4().hex}_{safe_name}')
    with open(target_path, 'wb') as target:
        for chunk in uploaded_file.chunks():
            target.write(chunk)
    return target_path

def download_zip_url_to_temp_file(source_url, job_id):
    temp_dir = os.path.join(tempfile.gettempdir(), 'simplyjudge_zip_imports')
    os.makedirs(temp_dir, exist_ok=True)
    parsed_url = urlparse(source_url)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(parsed_url.path) or 'remote-package.zip')
    target_path = os.path.join(temp_dir, f'{job_id}_{uuid.uuid4().hex}_{safe_name}')
    request = Request(source_url, headers={'User-Agent': 'SimplyJudge ZIP Importer'})
    with urlopen(request, timeout=60) as response, open(target_path, 'wb') as target:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
    return target_path

def get_chunk_upload_dir(upload_id):
    safe_upload_id = re.sub(r'[^a-zA-Z0-9_-]', '', upload_id)
    if not safe_upload_id:
        raise ValueError('Invalid upload id.')
    return os.path.join(tempfile.gettempdir(), 'simplyjudge_zip_chunks', safe_upload_id)

def assemble_chunked_zip(upload_id, filename, total_chunks, job_id):
    chunk_dir = get_chunk_upload_dir(upload_id)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(filename))
    temp_dir = os.path.join(tempfile.gettempdir(), 'simplyjudge_zip_imports')
    os.makedirs(temp_dir, exist_ok=True)
    target_path = os.path.join(temp_dir, f'{job_id}_{uuid.uuid4().hex}_{safe_name}')

    with open(target_path, 'wb') as target:
        for index in range(total_chunks):
            chunk_path = os.path.join(chunk_dir, f'{index:06d}.part')
            if not os.path.exists(chunk_path):
                raise ValueError(f'Missing upload chunk {index + 1} of {total_chunks}.')
            with open(chunk_path, 'rb') as source:
                for piece in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(piece)

    for name in os.listdir(chunk_dir):
        os.remove(os.path.join(chunk_dir, name))
    os.rmdir(chunk_dir)
    return target_path

def clean_cell(row, *names, default=''):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    lower_row = {str(k).lower().strip(): v for k, v in row.items() if k is not None}
    for name in names:
        value = lower_row.get(name.lower().strip())
        if value is not None and str(value).strip():
            return str(value).strip()
    return default

def decode_csv_bytes(csv_bytes):
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return csv_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return csv_bytes.decode('utf-8-sig', errors='replace')

def split_multi_value_cell(value):
    value = str(value or '').strip()
    if not value:
        return []
    if ';' in value:
        return [part.strip() for part in value.split(';') if part.strip()]
    return [part.strip() for part in value.splitlines() if part.strip()]

def strip_number_prefix(value):
    value = str(value or '').strip()
    return re.sub(r'^(?:nr|no|number|image|photo|entry)?\s*#?\s*\d{1,2}\s*[:.)-]\s*', '', value, flags=re.IGNORECASE).strip()

def display_title_from_reference(value, fallback):
    cleaned = strip_number_prefix(value)
    if not cleaned or cleaned in {'-', '—'}:
        return fallback
    basename = os.path.basename(cleaned)
    stem, ext = os.path.splitext(basename)
    if ext.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}:
        return stem or fallback
    return cleaned

def parse_numbered_short_lines(value):
    numbered = {}
    plain = []
    for line in split_multi_value_cell(value):
        match = re.match(r'^(?:nr|no|number|image|photo|entry)?\s*#?\s*(\d{1,2})\s*[:.)-]\s*(.+)$', line, flags=re.IGNORECASE)
        if match:
            numbered[int(match.group(1))] = match.group(2).strip()
        else:
            plain.append(line.strip())
    return numbered, plain

def parse_numbered_long_blocks(value):
    value = str(value or '').strip()
    if not value:
        return {}
    blocks = {}
    pattern = re.compile(r'(?:^|\n)\s*(\d{1,2})\.\s+(.*?)(?=(?:\n\s*\d{1,2}\.\s+)|\Z)', re.DOTALL)
    for match in pattern.finditer(value):
        blocks[int(match.group(1))] = match.group(2).strip()
    return blocks

def parse_camera_setting_blocks(value):
    value = str(value or '').strip()
    if not value:
        return {}
    settings = {}
    blocks = [block.strip() for block in re.split(r'\n\s*\n', value) if block.strip()]
    for block in blocks:
        numbers = [int(num) for num in re.findall(r'\bNr\s*(\d{1,2})\b', block, flags=re.IGNORECASE)]
        if not numbers:
            match = re.search(r'camera settings?\s*(\d{1,2})', block, flags=re.IGNORECASE)
            numbers = [int(match.group(1))] if match else []
        for number in numbers:
            settings[number] = block
    return settings

def is_participant_entry_row(row):
    keys = {str(key).lower().strip() for key in row.keys() if key is not None}
    return 'picture titles' in keys and ('10 uploads' in keys or '15 uploads' in keys)

def expand_participant_entry_row(row):
    first_name = clean_cell(row, 'First name', 'First Name')
    last_name = clean_cell(row, 'Last name', 'Last Name')
    photographer = ' '.join(part for part in [first_name, last_name] if part).strip() or 'Unknown'
    photographer_email = clean_cell(row, 'Email', 'email', 'Email Address', 'email_address', 'Photographer Email')
    camera_settings = parse_camera_setting_blocks(clean_cell(row, 'Camera settings'))
    title_map, plain_titles = parse_numbered_short_lines(clean_cell(row, 'Picture titles'))
    stories_10 = parse_numbered_long_blocks(clean_cell(row, "10 Story's & Context's"))
    stories_15 = parse_numbered_long_blocks(clean_cell(row, "15 Story's & Context's"))
    upload_refs = split_multi_value_cell(clean_cell(row, '10 uploads')) + split_multi_value_cell(clean_cell(row, '15 uploads'))

    expected_count_match = re.search(r'\d+', clean_cell(row, 'How many are you planning to submit'))
    expected_count = int(expected_count_match.group(0)) if expected_count_match else 0
    plain_title_count = len(plain_titles) if len(plain_titles) > 1 else 0
    entry_count = max(len(upload_refs), len(title_map), plain_title_count)
    if entry_count == 0:
        entry_count = expected_count
    expanded = []

    for index in range(1, entry_count + 1):
        raw_title = title_map.get(index)
        if not raw_title and len(plain_titles) == entry_count:
            raw_title = plain_titles[index - 1]
        elif not raw_title and len(plain_titles) == 1:
            raw_title = f'{plain_titles[0]} {index}'
        upload_ref = upload_refs[index - 1] if index <= len(upload_refs) else ''
        fallback_title = f'{photographer} entry {index}'
        display_title = display_title_from_reference(raw_title or upload_ref, fallback_title)

        expanded.append({
            'Title': display_title,
            'Photographer': photographer,
            'Photographer Email': photographer_email,
            'Category': 'General',
            'Description': stories_10.get(index) or stories_15.get(index) or '',
            'Camera Settings': camera_settings.get(index, ''),
            'Image': upload_ref,
            'Filename': '',
            'Entry Code': '',
        })
    return expanded

def expand_entry_rows(rows):
    expanded = []
    for row in rows:
        if is_participant_entry_row(row):
            expanded.extend(expand_participant_entry_row(row))
        else:
            expanded.append(row)
    return expanded

def truncate_text(value, max_length):
    value = str(value or '').strip()
    if len(value) <= max_length:
        return value
    return value[:max_length].rstrip()

def truncate_filename(filename, max_length=180):
    filename = os.path.basename(str(filename or 'photo.jpg')).strip() or 'photo.jpg'
    stem, ext = os.path.splitext(filename)
    if len(filename) <= max_length:
        return filename
    allowed_stem_length = max_length - len(ext)
    return f'{stem[:allowed_stem_length].rstrip()}{ext}'

def unique_import_filename(job_id, row_identifier, filename):
    filename = truncate_filename(filename)
    safe_prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', f'import_{job_id}_{row_identifier}_{uuid.uuid4().hex[:10]}')
    return truncate_filename(f'{safe_prefix}_{filename}')

def normalize_match_key(value):
    stem = os.path.splitext(os.path.basename(str(value).strip()))[0]
    while True:
        nested_stem, nested_ext = os.path.splitext(stem.rstrip('. '))
        if nested_ext.lower() not in IMAGE_EXTENSIONS:
            break
        stem = nested_stem
    return re.sub(r'[^a-z0-9]', '', stem.lower())

def photographer_tokens(photographer):
    return [
        normalize_match_key(token)
        for token in re.split(r'\s+', str(photographer or '').strip())
        if len(normalize_match_key(token)) >= 4
    ]

def image_key_matches_photographer(image_key, photographer):
    full_name = normalize_match_key(photographer)
    if full_name and full_name in image_key:
        return True
    return any(token in image_key for token in photographer_tokens(photographer))

def find_matching_image(images, candidates, photographer='', allow_suffix=True):
    candidate_keys = [normalize_match_key(candidate) for candidate in candidates if candidate]

    for key in candidate_keys:
        if key in images:
            return images[key]

    if not allow_suffix:
        return None

    for key in candidate_keys:
        if not key or key.isdigit():
            continue
        suffix_matches = [
            (image_key, image_info)
            for image_key, image_info in images.items()
            if image_key.endswith(key)
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0][1]

        photographer_matches = [
            image_info
            for image_key, image_info in suffix_matches
            if image_key_matches_photographer(image_key, photographer)
        ]
        if len(photographer_matches) == 1:
            return photographer_matches[0]

    return None

def find_entry_csv(zip_file):
    csv_members = [
        info for info in zip_file.infolist()
        if not info.is_dir()
        and not os.path.basename(info.filename).startswith('.')
        and info.filename.lower().endswith('.csv')
    ]
    if not csv_members:
        raise ValueError('No CSV file found in the ZIP package.')
    for info in csv_members:
        if os.path.basename(info.filename).lower() == 'entryform.csv':
            return info
    return csv_members[0]

def collect_zip_image_index(zip_file):
    images = {}
    for info in zip_file.infolist():
        filename = os.path.basename(info.filename)
        if info.is_dir() or not filename or filename.startswith('.'):
            continue
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in IMAGE_EXTENSIONS:
            continue
        images[normalize_match_key(stem)] = info
    return images

def natural_sort_key(value):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', str(value))
    ]

def collect_zip_image_members(zip_file):
    image_members = []
    for info in zip_file.infolist():
        filename = os.path.basename(info.filename)
        if info.is_dir() or not filename or filename.startswith('.'):
            continue
        _, ext = os.path.splitext(filename)
        if ext.lower() in IMAGE_EXTENSIONS:
            image_members.append(info)
    return sorted(image_members, key=lambda info: natural_sort_key(os.path.basename(info.filename)))

def parse_entry_rows(csv_bytes):
    text = decode_csv_bytes(csv_bytes)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('The CSV has no header row.')
    return list(reader)

def process_entry_zip_job(job_id):
    close_old_connections()
    job = None
    try:
        job = ZipImportJob.objects.select_related('competition').get(id=job_id)
        job.status = ZipImportJob.Status.PROCESSING
        job.error_message = ''
        job.save(update_fields=['status', 'error_message', 'updated_at'])

        if job.source_url and not job.temp_path:
            job.temp_path = download_zip_url_to_temp_file(job.source_url, job.id)
            job.save(update_fields=['temp_path', 'updated_at'])

        with zipfile.ZipFile(job.temp_path) as package:
            csv_info = find_entry_csv(package)
            rows = expand_entry_rows(parse_entry_rows(package.read(csv_info.filename)))
            images = collect_zip_image_index(package)

            job.total_rows = len(rows)
            job.save(update_fields=['total_rows', 'updated_at'])

            with transaction.atomic():
                imported = 0
                matched = 0
                for row_number, row in enumerate(rows, start=2):
                    image_payload = None
                    storage_image = None
                    defaults = None
                    try:
                        title = truncate_text(clean_cell(row, 'Title', 'title', default='Untitled'), 200)
                        photographer = truncate_text(clean_cell(row, 'Photographer', 'photographer', 'Photographer Name', 'photographer_name', default='Unknown'), 200)
                        photographer_email = clean_cell(
                            row,
                            'Photographer Email',
                            'photographer_email',
                            'Email',
                            'email',
                            'Email Address',
                            'email_address',
                        )
                        category = truncate_text(clean_cell(row, 'Category', 'category', default='General'), 100)
                        entry_code = clean_cell(row, 'Code', 'ID', 'Number', 'Entry ID', 'Entry Code', 'id')
                        image_references = [
                            clean_cell(row, 'Image'),
                            clean_cell(row, 'Image File'),
                            clean_cell(row, 'Filename'),
                            clean_cell(row, 'File Name'),
                            clean_cell(row, 'Photo File'),
                            clean_cell(row, 'Photo Filename'),
                            clean_cell(row, 'Asset'),
                        ]
                        description = clean_cell(row, 'Description', 'description', 'Story', 'story')
                        camera_settings = clean_cell(row, 'Camera Settings', 'camera settings', 'Settings', 'settings')

                        image_info = find_matching_image(images, [*image_references, entry_code], photographer=photographer)
                        if not image_info:
                            image_info = find_matching_image(images, [title], allow_suffix=False)
                        if image_info:
                            image_payload = {
                                'filename': unique_import_filename(job.id, row_number, image_info.filename),
                                'bytes': package.read(image_info.filename),
                            }

                        defaults = {
                            'competition': job.competition,
                            'entry_code': truncate_text(entry_code, 120) if entry_code else '',
                            'title': title,
                            'photographer_name': photographer,
                            'photographer_email': photographer_email,
                            'category': category,
                            'description': description,
                            'camera_settings': camera_settings,
                        }

                        if image_payload:
                            storage_image = prepare_image_for_cloudinary(
                                image_payload['bytes'],
                                image_payload['filename'],
                            )
                            flags = collect_photo_rule_flags(job.competition, image_payload['bytes'])
                            if storage_image['compressed']:
                                flags.append(
                                    'Image optimized for Cloudinary upload limit '
                                    f"({storage_image['original_size']} bytes -> {storage_image['size']} bytes)."
                                )
                            defaults['rule_flags'] = ' | '.join(flags) if flags else ''
                            defaults['image'] = ContentFile(storage_image['bytes'], name=storage_image['filename'])
                            matched += 1
                        else:
                            defaults['image'] = 'competition_photos/placeholder.jpg'
                            defaults['rule_flags'] = 'No matching image file found in uploaded ZIP package.'

                        if entry_code:
                            try:
                                photo_id = int(entry_code.strip())
                            except ValueError:
                                photo_id = None
                        else:
                            photo_id = None

                        if photo_id is not None:
                            existing = Photo.objects.filter(id=photo_id).first()
                            if existing and existing.competition_id != job.competition_id:
                                raise ValueError(f'CSV row {row_number}: entry code {photo_id} already belongs to another competition.')
                            if existing and not image_payload:
                                defaults.pop('image', None)
                            Photo.objects.update_or_create(id=photo_id, defaults=defaults)
                        else:
                            Photo.objects.update_or_create(
                                competition=job.competition,
                                title=title,
                                photographer_name=photographer,
                                defaults=defaults,
                            )
                        imported += 1
                    finally:
                        del image_payload
                        del storage_image
                        del defaults
                        gc.collect()

        job.status = ZipImportJob.Status.COMPLETED
        job.processed_rows = imported
        job.matched_images = matched
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'processed_rows', 'matched_images', 'finished_at', 'updated_at'])
        print(f'SimplyJudge ZIP sync completed for {job.source_name}: {imported} rows, {matched} images matched.')
    except Exception as exc:
        if job is not None:
            job.status = ZipImportJob.Status.FAILED
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
        print(f'SimplyJudge ZIP sync failed for job {job_id}: {exc}')
    finally:
        if job is not None and job.temp_path and os.path.exists(job.temp_path):
            try:
                os.remove(job.temp_path)
            except OSError:
                pass
        close_old_connections()

def process_photos_only_zip_job(job_id):
    close_old_connections()
    job = None
    try:
        job = ZipImportJob.objects.select_related('competition').get(id=job_id)
        job.status = ZipImportJob.Status.PROCESSING
        job.error_message = ''
        job.save(update_fields=['status', 'error_message', 'updated_at'])

        if job.source_url and not job.temp_path:
            job.temp_path = download_zip_url_to_temp_file(job.source_url, job.id)
            job.save(update_fields=['temp_path', 'updated_at'])

        with zipfile.ZipFile(job.temp_path) as package:
            image_members = collect_zip_image_members(package)
            if not image_members:
                raise ValueError('No photo files found in the ZIP package.')

            entry_codes = [
                truncate_text(os.path.splitext(os.path.basename(info.filename))[0], 120)
                for info in image_members
            ]
            duplicate_codes = sorted({code for code in entry_codes if entry_codes.count(code) > 1})
            if duplicate_codes:
                raise ValueError(f'Duplicate photo filename code(s) found: {", ".join(duplicate_codes[:5])}.')

            job.total_rows = len(image_members)
            job.save(update_fields=['total_rows', 'updated_at'])

            with transaction.atomic():
                imported = 0
                for row_number, (info, entry_code) in enumerate(zip(image_members, entry_codes), start=1):
                    image_bytes = None
                    storage_image = None
                    defaults = None
                    try:
                        image_bytes = package.read(info.filename)
                        storage_image = prepare_image_for_cloudinary(
                            image_bytes,
                            unique_import_filename(job.id, row_number, info.filename),
                        )
                        flags = collect_photo_rule_flags(job.competition, image_bytes)
                        if storage_image['compressed']:
                            flags.append(
                                'Image optimized for Cloudinary upload limit '
                                f"({storage_image['original_size']} bytes -> {storage_image['size']} bytes)."
                            )

                        defaults = {
                            'title': entry_code,
                            'photographer_name': 'Unknown',
                            'category': 'General',
                            'description': '',
                            'camera_settings': '',
                            'rule_flags': ' | '.join(flags) if flags else '',
                            'image': ContentFile(storage_image['bytes'], name=storage_image['filename']),
                        }
                        Photo.objects.update_or_create(
                            competition=job.competition,
                            entry_code=entry_code,
                            defaults=defaults,
                        )
                        imported += 1
                    finally:
                        del image_bytes
                        del storage_image
                        del defaults
                        gc.collect()

        job.status = ZipImportJob.Status.COMPLETED
        job.processed_rows = imported
        job.matched_images = imported
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'processed_rows', 'matched_images', 'finished_at', 'updated_at'])
        print(f'SimplyJudge photo-only sync completed for {job.source_name}: {imported} photos imported.')
    except Exception as exc:
        if job is not None:
            job.status = ZipImportJob.Status.FAILED
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
        print(f'SimplyJudge photo-only sync failed for job {job_id}: {exc}')
    finally:
        if job is not None and job.temp_path and os.path.exists(job.temp_path):
            try:
                os.remove(job.temp_path)
            except OSError:
                pass
        close_old_connections()

def audit_photo_metadata(file_bytes):
    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            exif_raw = img.getexif()
            audit_results = {'date_valid': False, 'has_gps': False, 'flags': []}
            if not exif_raw:
                audit_results['flags'].append("No EXIF data found (Likely stripped by editing software/Wix).")
                return audit_results
            for tag_id, value in exif_raw.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    year = str(value).split(':')[0]
                    if year in ['2025', '2026']:
                        audit_results['date_valid'] = True
                    else:
                        audit_results['flags'].append(f"Taken outside valid date range: {year}")
                    break
            gps_info = exif_raw.get_ifd(0x8825)
            if gps_info:
                audit_results['has_gps'] = True
            else:
                audit_results['flags'].append("No GPS location data found (Privacy filter applied).")
            return audit_results
    except Exception as e:
        return {'date_valid': False, 'has_gps': False, 'flags': ["Corrupted file or unable to read metadata."]}

def prepare_image_for_cloudinary(file_bytes, filename, max_bytes=CLOUDINARY_FREE_UPLOAD_LIMIT_BYTES):
    if len(file_bytes) <= max_bytes:
        return {
            'bytes': file_bytes,
            'filename': filename,
            'compressed': False,
            'original_size': len(file_bytes),
            'size': len(file_bytes),
        }

    target_bytes = min(CLOUDINARY_SAFE_UPLOAD_TARGET_BYTES, int(max_bytes * 0.92))
    stem, _ = os.path.splitext(os.path.basename(filename))
    output_filename = truncate_filename(f'{stem or "photo"}.jpg')
    image = None
    working = None

    try:
        with Image.open(io.BytesIO(file_bytes)) as source:
            source.draft('RGB', (MAX_IMAGE_PROCESSING_DIMENSION, MAX_IMAGE_PROCESSING_DIMENSION))
            source.load()
            image = ImageOps.exif_transpose(source)

        if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel('A'))
            rgba.close()
            if image is not background:
                image.close()
            image = background
        elif image.mode != 'RGB':
            converted = image.convert('RGB')
            image.close()
            image = converted

        image.thumbnail(
            (MAX_IMAGE_PROCESSING_DIMENSION, MAX_IMAGE_PROCESSING_DIMENSION),
            Image.Resampling.LANCZOS,
        )
        working = image
        min_quality = 62
        max_dimension = max(working.size)

        while max_dimension >= 320:
            best_payload = None
            for quality in range(88, min_quality - 1, -4):
                output = io.BytesIO()
                working.save(output, format='JPEG', quality=quality, optimize=True, progressive=True)
                payload = output.getvalue()
                output.close()
                if len(payload) <= target_bytes:
                    best_payload = payload
                    break

            if best_payload is not None:
                return {
                    'bytes': best_payload,
                    'filename': output_filename,
                    'compressed': True,
                    'original_size': len(file_bytes),
                    'size': len(best_payload),
                }

            max_dimension = int(max_dimension * 0.82)
            ratio = max_dimension / max(working.size)
            next_size = (
                max(1, int(working.size[0] * ratio)),
                max(1, int(working.size[1] * ratio)),
            )
            resized = working.resize(next_size, Image.Resampling.LANCZOS)
            if working is not image:
                working.close()
            working = resized
    finally:
        if working is not None and working is not image:
            working.close()
        if image is not None:
            image.close()
        gc.collect()

    raise ValueError(
        f'Image {filename} could not be compressed below the Cloudinary upload limit '
        f'of {max_bytes} bytes.'
    )
