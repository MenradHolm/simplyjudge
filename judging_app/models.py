from django.db import models
from django.contrib.auth.models import User

# =====================================================================
# 1. THE PARENT COMPETITION CONTAINER
# =====================================================================
class Competition(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


# =====================================================================
# 2. COMPETITION-LINKED MODELS
# =====================================================================
class Photo(models.Model):
    # This tethers every single photo upload to a specific competition
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='photos')
    title = models.CharField(max_length=200)
    photographer_name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    image = models.ImageField(upload_to='photos/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [
            ("can_judge_photos", "Can explicitly access judging panels and submit grades"),
        ]

    def __str__(self):
        return f"{self.title} (Comp: {self.competition.name})"


class RubricCriterion(models.Model):
    # This allows different competitions to have totally unique scoring metrics
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='criteria')
    name = models.CharField(max_length=100)
    max_points = models.IntegerField()
    weight = models.FloatField(default=1.0)

    def __str__(self):
        return f"{self.name} - Max: {self.max_points} (Comp: {self.competition.name})"


# =====================================================================
# 3. TRANSITIONAL SCORE TRACKING
# =====================================================================
class Score(models.Model):
    # This naturally inherits the competition through the linked photo
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='score')
    judge = models.ForeignKey(User, on_delete=models.CASCADE)
    criteria_scores = models.JSONField(default=dict)  # Stores individual sub-scores
    total_score = models.FloatField()
    comment = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('photo', 'judge')

    def __str__(self):
        return f"{self.judge.username} rated {self.photo.title} -> {self.total_score}"