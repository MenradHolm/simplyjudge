from django.contrib import admin
from .models import Competition, Photo, RubricCriterion, Score

# --- NEW: The Rubric Grid Layout ---
class RubricCriterionInline(admin.TabularInline):
    model = RubricCriterion
    extra = 5  # This gives you 5 blank rows automatically when creating a competition

# --- UPDATED: The Competition Admin ---
@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    inlines = [RubricCriterionInline]  # <-- This attaches the grid to the competition page!

# --- The rest stays exactly the same ---
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
    list_display = ('photo', 'judge', 'total_score', 'submitted_at')
    list_filter = ('photo__competition', 'judge')