import csv
import gc
import math
import os
import re
import tempfile
import uuid
import zipfile
import io
import threading
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from PIL import Image
from PIL.ExifTags import TAGS
from PIL import ImageOps

from django.contrib import messages
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import close_old_connections, transaction
from django.db.models import Avg, FloatField
from django.db.models.functions import Cast
from django.utils import timezone

from .models import Competition, CompetitionMembership, Photo, PhotoStatusVote, RoundOneScore, Score, RubricCriterion, ZipImportJob

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
CLOUDINARY_FREE_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
CLOUDINARY_SAFE_UPLOAD_TARGET_BYTES = int(CLOUDINARY_FREE_UPLOAD_LIMIT_BYTES * 0.92)
MAX_IMAGE_PROCESSING_DIMENSION = 2400

def register_user(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

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

def judging_photo_queryset(competition, user):
    queryset = Photo.objects.filter(competition=competition).order_by('entry_code', 'id')
    if user.is_superuser or is_feedback_portal(competition):
        return queryset
    return queryset.filter(status=Photo.Status.SHORTLISTED)

def photo_report_queryset(competition):
    return Photo.objects.filter(competition=competition).order_by('entry_code', 'id')

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
                Photo.objects.select_for_update(),
                id=int(photo_id),
                competition=competition,
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

    current_photo = Photo.objects.filter(
        competition=competition,
        status=Photo.Status.PENDING,
    ).exclude(status_votes__voter=request.user).order_by('id').first()
    counts = {
        'pending': Photo.objects.filter(competition=competition, status=Photo.Status.PENDING).count(),
        'round_1': Photo.objects.filter(competition=competition, status=Photo.Status.ROUND_1).count(),
        'shortlisted': Photo.objects.filter(competition=competition, status=Photo.Status.SHORTLISTED).count(),
        'rejected': Photo.objects.filter(competition=competition, status=Photo.Status.REJECTED).count(),
        'for_you': Photo.objects.filter(
            competition=competition,
            status=Photo.Status.PENDING,
        ).exclude(status_votes__voter=request.user).count(),
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

    current_photo = Photo.objects.filter(
        competition=competition,
        status=Photo.Status.ROUND_1,
    ).exclude(round_1_scores__judge=request.user).order_by('id').first()

    counts = {
        'for_you': Photo.objects.filter(
            competition=competition,
            status=Photo.Status.ROUND_1,
        ).exclude(round_1_scores__judge=request.user).count(),
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

    current_index = scored_photos if existing_score else scored_photos + 1
    current_index = min(max(current_index, 1), total_photos or 1)
    context = {
        'competition': competition,
        'photo': photo,
        'rubric': rubric,
        'progress': f"{current_index} / {total_photos}",
        'current_index': current_index,
        'total_photos': total_photos,
        'existing_score': existing_score,
        'return_to': return_to,
    }
    return render(request, 'judging_app/judge.html', context)

def leaderboard(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    tie_criterion = competition.tie_breaker_criterion
    photos_query = Photo.objects.filter(competition=competition).annotate(average_score=Avg('score__total_score'))
    if tie_criterion:
        ranked_photos = photos_query.annotate(
            tie_breaker_score=Avg(Cast(f'score__criteria_scores__{tie_criterion.id}', FloatField()))
        ).filter(average_score__isnull=False).order_by('-average_score', '-tie_breaker_score')
    else:
        ranked_photos = photos_query.filter(average_score__isnull=False).order_by('-average_score')
    return render(request, 'judging_app/leaderboard.html', {'competition': competition, 'photos': ranked_photos, 'tie_criterion': tie_criterion})

def submit_photo(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    error_message = None
    if request.method == "POST":
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
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
                    category=category, image=image, description=description, camera_settings=camera_settings
                )
                return render(request, 'judging_app/submit_success.html', {'competition': competition})
    return render(request, 'judging_app/submit.html', {'competition': competition, 'error_message': error_message})

def feedback_report_context(competition, can_edit_notes=False):
    photos = list(photo_report_queryset(competition))
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]
    return {
        'competition': competition,
        'photos': photos,
        'can_edit_notes': can_edit_notes,
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
                    
                    if not name:
                        continue
                        
                    RubricCriterion.objects.create(
                        competition=competition,
                        name=name.strip(),
                        description=desc.strip(),
                        weight=float(weight_val.strip())
                    )
                    rubric_count += 1
                
                messages.success(request, f'Successfully built a dynamic {rubric_count}-column rubric matrix!')
                return redirect('home_hub')

            else:
                import_count = 0
                for row in reader:
                    title = row.get('Title') or row.get('title') or 'Untitled'
                    photographer = row.get('Photographer') or row.get('photographer') or 'Unknown'
                    category = row.get('Category') or row.get('category') or 'General'
                    custom_code = row.get('Code') or row.get('ID') or row.get('Number') or row.get('id')
                    desc = row.get('Description') or row.get('description') or row.get('Story') or row.get('story') or ''
                    
                    # Look for settings column
                    cam_settings = row.get('Camera Settings') or row.get('camera settings') or row.get('Settings') or row.get('settings') or ''

                    if not custom_code:
                        continue

                    Photo.objects.create(
                        id=int(custom_code.strip()), competition=competition, title=title,
                        entry_code=custom_code.strip(), photographer_name=photographer, category=category,
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
            'Category': 'General',
            'Description': stories_10.get(index) or stories_15.get(index) or '',
            'Camera Settings': camera_settings.get(index, ''),
            'Image': upload_ref,
            'Filename': raw_title or '',
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

def find_matching_image(images, candidates, photographer=''):
    candidate_keys = [normalize_match_key(candidate) for candidate in candidates if candidate]

    for key in candidate_keys:
        if key in images:
            return images[key]

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

                        match_candidates = [*image_references, entry_code, title]
                        image_info = find_matching_image(images, match_candidates, photographer=photographer)
                        if image_info:
                            image_payload = {
                                'filename': truncate_filename(image_info.filename),
                                'bytes': package.read(image_info.filename),
                            }

                        defaults = {
                            'competition': job.competition,
                            'entry_code': truncate_text(entry_code, 120) if entry_code else '',
                            'title': title,
                            'photographer_name': photographer,
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
                for info, entry_code in zip(image_members, entry_codes):
                    image_bytes = None
                    storage_image = None
                    defaults = None
                    try:
                        image_bytes = package.read(info.filename)
                        storage_image = prepare_image_for_cloudinary(
                            image_bytes,
                            truncate_filename(info.filename),
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
