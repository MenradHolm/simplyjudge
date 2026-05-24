from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Avg 
from .models import Photo, RubricCriterion, Score

@login_required(login_url='/admin/login/') 
def judge_router(request):
    """Finds the first photo this judge HAS NOT scored yet and sends them to it."""
    next_photo = Photo.objects.exclude(score__judge=request.user).first()
    
    if next_photo:
        return redirect('judge_photo', photo_id=next_photo.id)
    
    return render(request, 'judging_app/done.html')

@login_required(login_url='/admin/login/') 
def judge_photo(request, photo_id):
    """Displays a single photo and handles the score saving."""
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

@login_required(login_url='/admin/login/')
def leaderboard(request):
    """Calculates the average score for each photo and ranks them."""
    # We use 'annotate' to calculate the average of all related scores for each photo.
    # We filter out photos that have NO scores yet, and order them highest to lowest.
    ranked_photos = Photo.objects.annotate(
        average_score=Avg('score__total_score')
    ).filter(
        average_score__isnull=False
    ).order_by('-average_score')

    context = {
        'photos': ranked_photos
    }
    return render(request, 'judging_app/leaderboard.html', context)

def submit_photo(request):
    """Public form for photographers to submit their work."""
    if request.method == "POST":
        # Grab the text fields
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
        category = request.POST.get('category')
        
        # Grab the actual image file
        image = request.FILES.get('image')

        # If everything is filled out, create the photo in the database
        if title and photographer_name and category and image:
            Photo.objects.create(
                title=title,
                photographer_name=photographer_name,
                category=category,
                image=image
            )
            # Send them to a simple "Thank You" page
            return render(request, 'judging_app/submit_success.html')

    # If they just arrived at the page, show them the empty form
    return render(request, 'judging_app/submit.html')