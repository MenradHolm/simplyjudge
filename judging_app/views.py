import csv
import os
import zipfile
from django.contrib import messages
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from .models import Competition, Photo, Score

# =====================================================================
# 1. USER ACCOUNT REGISTRATION & PERMISSIONS
# =====================================================================

def register_user(request):
    """Allows users to register an account. Access is restricted until given permissions."""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

def is_approved_judge(user, competition):
    """Bouncer helper: Checks if user is explicitly assigned to this competition, or is a superuser."""
    if user.is_superuser:
        return True
    return competition.judges.filter(id=user.id).exists()


# =====================================================================
# 2. GLOBAL HOME HUB
# =====================================================================

def home_hub(request):
    """The landing page. Shows all active competitions available on the system."""
    active_competitions = Competition.objects.filter(is_active=True).order_by('-created_at')
    return render(request, 'judging_app/home.html', {'competitions': active_competitions})

# =====================================================================
# 3. COMPETITION JUDGING PANEL & ROUTER
# =====================================================================

@login_required(login_url='/login/')
def judge_router(request, comp_id):
    """Finds the next unrated photo within a specific competition for this judge."""
    competition = get_object_or_404(Competition, id=comp_id)
    
    # NEW SECURITY CHECK
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
        
    # Find the first photo...
    next_photo = Photo.objects.filter(competition=competition).exclude(score__judge=request.user).first()
    
    if next_photo:
        return redirect('judge_photo', comp_id=competition.id, photo_id=next_photo.id)
        
    return render(request, 'judging_app/done.html', {'competition': competition})


@login_required(login_url='/login/')
def judge_photo(request, comp_id, photo_id):
    """Displays a single photo and handles saving judge metrics for a specific competition."""
    
    # 1. Grab the competition FIRST
    competition = get_object_or_404(Competition, id=comp_id)
    
    # 2. THEN check if the user is on the VIP list for this specific event
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
        
    # 3. THEN grab the photo and continue as normal
    photo = get_object_or_404(Photo, id=photo_id, competition=competition)
    
    # ... (the rest of your rubric and scoring logic goes here) ...
    
    # Grab only the criteria assigned to this specific competition
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
            defaults={
                'criteria_scores': criteria_scores,
                'total_score': total_score,
                'comment': comment
            }
        )
        return redirect('judge_router', comp_id=competition.id)

    context = {
        'competition': competition,
        'photo': photo,
        'rubric': rubric,
        'progress': f"{scored_photos + 1} / {total_photos}"
    }
    return render(request, 'judging_app/judge.html', context)


# =====================================================================
# 4. LIVE LEADERBOARD
# =====================================================================

from django.db.models import Avg, Q, FloatField
from django.db.models.functions import Cast

@login_required(login_url='/login/')
def leaderboard(request, comp_id):
    """Calculates average scores and ranks photos, using a chosen criterion as a tie-breaker."""
    competition = get_object_or_404(Competition, id=comp_id)
    tie_criterion = competition.tie_breaker_criterion

    # 1. Start building the photo query for this competition
    photos_query = Photo.objects.filter(competition=competition).annotate(
        average_score=Avg('score__total_score')
    )

    # 2. If a specific tie-breaker criterion is selected, calculate its specific average
    if tie_criterion:
        # We parse the criteria_scores JSON field to find the value matching this criterion's ID
        # Since JSON keys are strings, we look up str(tie_criterion.id)
        criterion_key = f"criteria_scores__{tie_criterion.id}"
        
        ranked_photos = photos_query.annotate(
            tie_breaker_score=Avg(Cast(f'score__criteria_scores__{tie_criterion.id}', FloatField()))
        ).filter(
            average_score__isnull=False
        ).order_by('-average_score', '-tie_breaker_score') # <-- Sorts by total avg first, tie-breaker second!
    else:
        # Fallback to standard sorting if no tie-breaker is chosen
        ranked_photos = photos_query.filter(
            average_score__isnull=False
        ).order_by('-average_score')

    return render(request, 'judging_app/leaderboard.html', {
        'competition': competition, 
        'photos': ranked_photos,
        'tie_criterion': tie_criterion
    })

# =====================================================================
# 5. PUBLIC PHOTO SUBMISSION PORTAL
# =====================================================================

def submit_photo(request, comp_id):
    """Public upload form for photographers to enter a specific competition."""
    competition = get_object_or_404(Competition, id=comp_id)
    error_message = None

    if request.method == "POST":
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
        category = request.POST.get('category')
        image = request.FILES.get('image')

        if title and photographer_name and category and image:
            max_size_bytes = 5 * 1024 * 1024 # 5 MB Limit
            
            if image.size > max_size_bytes:
                error_message = "The uploaded file is too large! Please keep your photo under 5 MB."
            else:
                Photo.objects.create(
                    competition=competition,
                    title=title,
                    photographer_name=photographer_name,
                    category=category,
                    image=image
                )
                return render(request, 'judging_app/submit_success.html', {'competition': competition})

    # THIS IS THE CRUCIAL LINE! Make sure it is backed out to this exact indentation level:
    return render(request, 'judging_app/submit.html', {
        'competition': competition, 
        'error_message': error_message
    })

# =====================================================================
# 6. ORGANIZER FEEDBACK REPORT
# =====================================================================

@login_required(login_url='/login/')
def feedback_report(request, comp_id):
    """A master view for the organizer to see every individual judge's score and comment."""
    if not request.user.is_staff:
        return redirect('home_hub')

    competition = get_object_or_404(Competition, id=comp_id)
    
    # 1. Fetch the photos
    photos = list(Photo.objects.filter(competition=competition))
    
    # 2. Fetch all scores for this competition explicitly
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    
    # 3. Manually group the scores to their exact photo (bypasses Django's strict naming rules)
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]

    return render(request, 'judging_app/feedback_report.html', {
        'competition': competition,
        'photos': photos
    })



@login_required(login_url='/login/')
def upload_spreadsheet(request, comp_id):
    """Allows the organizer to upload a CSV file and forces database IDs to match Kyle's custom codes."""
    if not request.user.is_staff:
        return redirect('home_hub')

    competition = get_object_or_404(Competition, id=comp_id)

    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Error: This is not a CSV file!')
            return redirect('upload_spreadsheet', comp_id=comp_id)

        try:
            file_data = csv_file.read().decode('utf-8').splitlines()
            reader = csv.DictReader(file_data)

            import_count = 0
            for row in reader:
                title = row.get('Title') or row.get('title') or 'Untitled'
                photographer = row.get('Photographer') or row.get('photographer') or 'Unknown'
                category = row.get('Category') or row.get('category') or 'General'
                
                # Grab Kyle's custom code column from the spreadsheet
                custom_code = row.get('Code') or row.get('ID') or row.get('Number') or row.get('id')
                
                if not custom_code:
                    continue

                # Create the entry using Kyle's exact code number as the primary database key
                Photo.objects.create(
                    id=int(custom_code.strip()),
                    competition=competition,
                    title=title,
                    photographer_name=photographer,
                    category=category,
                    image='competition_photos/placeholder.jpg'
                )
                import_count += 1

            messages.success(request, f'Successfully imported {import_count} entries with their unique codes!')
            return redirect('feedback_report', comp_id=comp_id)

        except Exception as e:
            messages.error(request, f'Error parsing spreadsheet: {str(e)}')
            return redirect('upload_spreadsheet', comp_id=comp_id)

    return render(request, 'judging_app/upload_spreadsheet.html', {'competition': competition})


@login_required(login_url='/login/')
def upload_photos_zip(request, comp_id):
    """Unzips folder and matches files (e.g. '101.jpg') directly to the unique database ID."""
    if not request.user.is_staff:
        return redirect('home_hub')

    competition = get_object_or_404(Competition, id=comp_id)

    if request.method == 'POST' and request.FILES.get('zip_file'):
        zip_file = request.FILES['zip_file']

        if not zip_file.name.endswith('.zip'):
            messages.error(request, 'Error: This is not a ZIP file!')
            return redirect('upload_spreadsheet', comp_id=comp_id)

        try:
            success_count = 0
            missing_photos = []

            with zipfile.ZipFile(zip_file) as z:
                for file_info in z.infolist():
                    if file_info.is_dir() or '/' in file_info.filename or file_info.filename.startswith('.'):
                        continue

                    filename = file_info.filename
                    name_without_ext, ext = os.path.splitext(filename)
                    
                    try:
                        # Convert the filename (e.g. '142') to an integer ID
                        target_id = int(name_without_ext.strip())
                        
                        # Find the photo record that matches this exact ID number
                        photo = Photo.objects.filter(competition=competition, id=target_id).first()
                        
                        if photo:
                            file_bytes = z.read(filename)
                            photo.image.save(filename, ContentFile(file_bytes))
                            success_count += 1
                        else:
                            missing_photos.append(filename)
                            
                    except ValueError:
                        # Skips non-numeric files like 'instructions.txt' or hidden system files safely
                        continue

            if missing_photos:
                messages.warning(request, f"Matched {success_count} photos. No spreadsheet records found for IDs: {', '.join(missing_photos[:5])}")
            else:
                messages.success(request, f"Successfully matched and uploaded all {success_count} photos to Cloudinary via unique code numbers!")

            return redirect('feedback_report', comp_id=comp_id)

        except Exception as e:
            messages.error(request, f"Error processing ZIP file: {str(e)}")
            return redirect('upload_spreadsheet', comp_id=comp_id)

    return redirect('upload_spreadsheet', comp_id=comp_id)

def public_results(request, comp_id):
    """Public results page for all participants to see the feedback ledger."""
    competition = get_object_or_454(Competition, id=comp_id)
    
    # Fetch photos and manually map the scores
    photos = list(Photo.objects.filter(competition=competition))
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]

    return render(request, 'judging_app/feedback_report.html', {
        'competition': competition,
        'photos': photos
    })