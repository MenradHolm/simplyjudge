from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Avg
from .models import Photo, RubricCriterion, Score

# =====================================================================
# 1. USER ACCOUNT REGISTRATION & PERMISSIONS
# =====================================================================

def register_user(request):
    """Allows anyone to create an account, but they won't have judge permissions yet."""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

def is_approved_judge(user):
    """Bouncer helper function: Checks if the user is a superuser or in the Judges group."""
    return user.groups.filter(name='Judges').exists() or user.is_superuser


# =====================================================================
# 2. JUDGING PANEL & ROUTER
# =====================================================================

@login_required(login_url='/login/')
def judge_router(request):
    """Finds the next unrated photo for this specific judge or sends them to the waiting room."""
    if not is_approved_judge(request.user):
        return render(request, 'judging_app/pending.html')

    next_photo = Photo.objects.exclude(score__judge=request.user).first()
    if next_photo:
        return redirect('judge_photo', photo_id=next_photo.id)
    return render(request, 'judging_app/done.html')

@login_required(login_url='/login/')
def judge_photo(request, photo_id):
    """Displays a single photo and handles saving judge metrics."""
    if not is_approved_judge(request.user):
        return render(request, 'judging_app/pending.html')

    photo = get_object_or_404(Photo, id=photo_id)
    rubric = RubricCriterion.objects.all()
    
    total_photos = Photo.objects.count()
    scored_photos = Score.objects.filter(judge=request.user).count()
    
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
        return redirect('judge_router')

    context = {
        'photo': photo,
        'rubric': rubric,
        'progress': f"{scored_photos + 1} / {total_photos}"
    }
    return render(request, 'judging_app/judge.html', context)


# =====================================================================
# 3. LIVE LEADERBOARD
# =====================================================================

@login_required(login_url='/login/')
def leaderboard(request):
    """Calculates the average score for each photo and ranks them."""
    ranked_photos = Photo.objects.annotate(
        average_score=Avg('score__total_score')
    ).filter(
        average_score__isnull=False
    ).order_by('-average_score')

    return render(request, 'judging_app/leaderboard.html', {'photos': ranked_photos})


# =====================================================================
# 4. PUBLIC PHOTO SUBMISSION PORTAL
# =====================================================================

def submit_photo(request):
    """Public upload form for photographers to submit their work directly to Cloudinary."""
    if request.method == "POST":
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
        category = request.POST.get('category')
        image = request.FILES.get('image')

        if title and photographer_name and category and image:
            Photo.objects.create(
                title=title,
                photographer_name=photographer_name,
                category=category,
                image=image
            )
            return render(request, 'judging_app/submit_success.html')

    return render(request, 'judging_app/submit.html')