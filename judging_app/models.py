from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class Competition(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, null=True, blank=True, help_text="Clean URL text (e.g., 'youth-poty' or 'shutter-society')")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    judges = models.ManyToManyField(User, blank=True)
    tie_breaker_criterion = models.ForeignKey(
        'RubricCriterion', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='tie_breaker_for'
    )

    def __str__(self):
        return self.name

class RubricCriterion(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='rubrics')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    weight = models.FloatField(default=1.0)

    def __str__(self):
        return f"{self.name} ({self.competition.name})"

class Photo(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
        REJECTED = 'REJECTED', 'Rejected'

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    photographer_name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    image = models.ImageField(upload_to='competition_photos/', max_length=255)
    rule_flags = models.TextField(blank=True, null=True)
    organizer_notes = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True) 
    
    # --- NEW CAMERA SETTINGS FIELD ---
    camera_settings = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} - #{self.id}"

class Score(models.Model):
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE)
    judge = models.ForeignKey(User, on_delete=models.CASCADE)
    criteria_scores = models.JSONField(default=dict)
    total_score = models.FloatField(default=0.0)
    comment = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('photo', 'judge')

class ZipImportJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='zip_import_jobs')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    source_name = models.CharField(max_length=255)
    source_url = models.URLField(blank=True, max_length=1000)
    temp_path = models.CharField(max_length=1000, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    matched_images = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.source_name} - {self.get_status_display()}"
