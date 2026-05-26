import csv
import os
import zipfile
import io
from PIL import Image
from PIL.ExifTags import TAGS

from django.contrib import messages
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Avg, FloatField
from django.db.models.functions import Cast

from .models import Competition, Photo, Score, RubricCriterion

def register_user(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

def is_approved_judge(user, competition):
    if user.is_superuser:
        return True
    return competition.judges.filter(id=user.id).exists()

def home_hub(request):
    active_competitions = Competition.objects.filter(is_active=True).order_by('-created_at')
    return render(request, 'judging_app/home.html', {'competitions': active_competitions})

# --- FIXED: Redirects now correctly point to /accounts/login/ ---
@login_required(login_url='/accounts/login/')
def judge_router(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    next_photo = Photo.objects.filter(competition=competition).exclude(score__judge=request.user).first()
    if next_photo:
        return redirect('judge_photo', comp_slug=competition.slug, photo_id=next_photo.id)
    return render(request, 'judging_app/done.html', {'competition': competition})

@login_required(login_url='/accounts/login/')
def judge_photo(request, comp_slug, photo_id):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    photo = get_object_or_404(Photo, id=photo_id, competition=competition)
    rubric = RubricCriterion.objects.filter(competition=competition)
    total_photos = Photo.objects.filter(competition=competition).count()
    scored_photos = Score.objects.filter(photo__competition=competition, judge=request.user).count()
    
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
        return redirect('judge_router', comp_slug=competition.slug)

    context = {
        'competition': competition,
        'photo': photo,
        'rubric': rubric,
        'progress': f"{scored_photos + 1} / {total_photos}"
    }
    return render(request, 'judging_app/judge.html', context)

@login_required(login_url='/accounts/login/')
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
        if title and photographer_name and category and image:
            if image.size > 5 * 1024 * 1024:
                error_message = "The uploaded file is too large! Please keep your photo under 5 MB."
            else:
                Photo.objects.create(
                    competition=competition, title=title, photographer_name=photographer_name,
                    category=category, image=image, description=description
                )
                return render(request, 'judging_app/submit_success.html', {'competition': competition})
    return render(request, 'judging_app/submit.html', {'competition': competition, 'error_message': error_message})

@login_required(login_url='/accounts/login/')
def feedback_report(request, comp_slug):
    if not request.user.is_staff:
        return redirect('home_hub')
    competition = get_object_or_404(Competition, slug=comp_slug)
    photos = list(Photo.objects.filter(competition=competition))
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]
    return render(request, 'judging_app/feedback_report.html', {'competition': competition, 'photos': photos})

@login_required(login_url='/accounts/login/')
def upload_spreadsheet(request, comp_slug):
    if not request.user.is_staff:
        return redirect('home_hub')

    competition = get_object_or_404(Competition, slug=comp_slug)

    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Error: This is not a CSV file!')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        try:
            file_data = csv_file.read().decode('utf-8').splitlines()
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
                    title = row.get('Title') or row.get('title')