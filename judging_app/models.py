"""Models for the judging app."""

from django.db import models
from django.contrib.auth.models import User

class Photo(models.Model):
    """Represents a submitted photograph in the competition."""
    title = models.CharField(max_length=255)
    photographer_name = models.CharField(max_length=255)
    category = models.CharField(max_length=100)
    image = models.ImageField(upload_to='photos/') 
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} by {self.photographer_name}"

class RubricCriterion(models.Model):
    """Represents a scoring criterion in the competition rubric."""
    name = models.CharField(max_length=100)
    max_points = models.FloatField(default=10.0)
    weight = models.FloatField(default=1.0)

    def __str__(self) -> str:
        return str(self.name)

class Score(models.Model):
    """Represents a judge's score for a submitted photograph."""
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE)
    judge = models.ForeignKey(User, on_delete=models.CASCADE)
    criteria_scores = models.JSONField(default=dict)
    total_score = models.FloatField(default=0.0)
    comment = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # pylint: disable=missing-class-docstring, too-few-public-methods
        unique_together = ('photo', 'judge')

    def __str__(self):
        return f"Score by {self.judge.username} for {self.photo.title}"