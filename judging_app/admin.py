from django.contrib import admin
from .models import Competition, Photo, RubricCriterion, Score

@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)

@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ('title', 'competition', 'photographer_name', 'category', 'uploaded_at')
    list_filter = ('competition', 'category')
    search_fields = ('title', 'photographer_name')

@admin.register(RubricCriterion)
class RubricCriterionAdmin(admin.ModelAdmin):
    list_display = ('name', 'competition', 'max_points', 'weight')
    list_filter = ('competition',)

@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    # Changed 'updated_at' to 'submitted_at' below to match our model fix!
    list_display = ('photo', 'judge', 'total_score', 'submitted_at')
    list_filter = ('photo__competition', 'judge')