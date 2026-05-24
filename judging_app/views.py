from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required  # <-- Add this!
from .models import Photo, RubricCriterion, Score

@login_required(login_url='/admin/login/') # <-- Add this!
def judge_router(request):
    """Finds the first photo this judge HAS NOT scored yet and sends them to it."""
    next_photo = Photo.objects.exclude(score__judge=request.user).first()
    
    if next_photo:
        return redirect('judge_photo', photo_id=next_photo.id)
    
    return render(request, 'judging_app/done.html')

@login_required(login_url='/admin/login/') # <-- Add this!
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