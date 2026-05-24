from django.shortcuts import render, redirect, get_object_or_404
from .models import Photo, RubricCriterion, Score

def judge_router(request):
    """Finds the first photo this judge HAS NOT scored yet and sends them to it."""
    # Find a photo where a score from the current user does NOT exist
    next_photo = Photo.objects.exclude(score__judge=request.user).first()
    
    if next_photo:
        return redirect('judge_photo', photo_id=next_photo.id)
    
    # If no unscored photos are left, they are done!
    return render(request, 'judging_app/done.html')

def judge_photo(request, photo_id):
    """Displays a single photo and handles the score saving."""
    photo = get_object_or_404(Photo, id=photo_id)
    rubric = RubricCriterion.objects.all()
    
    # Optional: Calculate progress to show the judge
    total_photos = Photo.objects.count()
    scored_photos = Score.objects.filter(judge=request.user).count()
    
    # If the judge clicked "Save Score"
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
        
        # Save the score
        Score.objects.update_or_create(
            photo=photo,
            judge=request.user,
            defaults={
                'criteria_scores': criteria_scores,
                'total_score': total_score,
                'comment': comment
            }
        )
        # Redirect back to the router to automatically fetch the NEXT photo!
        return redirect('judge_router')

    # If it's just a normal page load, show the photo and form
    context = {
        'photo': photo,
        'rubric': rubric,
        'progress': f"{scored_photos + 1} / {total_photos}"
    }
    return render(request, 'judging_app/judge.html', context)