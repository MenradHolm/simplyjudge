from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils.text import slugify
from django.utils import timezone

def competition_photo_upload_path(instance, filename):
    competition = getattr(instance, 'competition', None)
    folder_name = ''
    if competition is not None:
        folder_name = slugify(competition.name or competition.slug or '')
    if not folder_name:
        folder_name = 'uncategorized'
    return f'competition_photos/{folder_name}/{filename}'

class Competition(models.Model):
    class Workflow(models.TextChoices):
        FULL_COMPETITION = 'FULL_COMPETITION', 'Full competition funnel'
        FEEDBACK_PORTAL = 'FEEDBACK_PORTAL', 'Feedback portal'

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, null=True, blank=True, help_text="Clean URL text (e.g., 'youth-poty' or 'shutter-society')")
    workflow = models.CharField(max_length=30, choices=Workflow.choices, default=Workflow.FULL_COMPETITION)
    entry_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
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

class CompetitionMembership(models.Model):
    class Role(models.TextChoices):
        ORGANIZER = 'ORGANIZER', 'Competition Organizer'
        INTERNAL_JUDGE = 'INTERNAL_JUDGE', 'Internal Reviewer'
        VIP_JUDGE = 'VIP_JUDGE', 'VIP Guest Judge'

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='competition_memberships')
    role = models.CharField(max_length=30, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('competition', 'user', 'role')
        ordering = ['competition__name', 'user__username', 'role']

    def __str__(self):
        return f"{self.user} - {self.competition} - {self.get_role_display()}"

class EntryOrder(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='entry_orders')
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='entry_orders')
    stripe_checkout_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = 'paid' if self.is_paid else 'pending'
        return f"{self.user} - {self.competition} - {status}"

class RubricCriterion(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='rubrics')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    weight = models.FloatField(default=1.0)
    score_out_of = models.PositiveIntegerField(default=100, validators=[MinValueValidator(1)])

    def __str__(self):
        return f"{self.name} ({self.competition.name})"

class Photo(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        REJECTED = 'REJECTED', 'Rejected'
        ROUND_1 = 'ROUND_1', 'Round 1'
        SHORTLISTED = 'SHORTLISTED', 'Shortlisted'

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE)
    entry_code = models.CharField(max_length=120, blank=True)
    title = models.CharField(max_length=200)
    photographer_name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    image = models.ImageField(upload_to=competition_photo_upload_path, max_length=255)
    rule_flags = models.TextField(blank=True, null=True)
    organizer_notes = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True) 
    
    # --- NEW CAMERA SETTINGS FIELD ---
    camera_settings = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} - #{self.id}"

class PhotoStatusVote(models.Model):
    class Decision(models.TextChoices):
        ROUND_1 = 'ROUND_1', 'Advance to Round 1'
        REJECT = 'REJECT', 'Reject'

    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='status_votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='photo_status_votes')
    decision = models.CharField(max_length=20, choices=Decision.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('photo', 'voter')

    def __str__(self):
        return f"{self.photo_id} - {self.voter} - {self.get_decision_display()}"

class RoundOneScore(models.Model):
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='round_1_scores')
    judge = models.ForeignKey(User, on_delete=models.CASCADE, related_name='round_1_scores')
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(10)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('photo', 'judge')

    def __str__(self):
        return f"{self.photo_id} - {self.judge} - {self.score}/10"

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
