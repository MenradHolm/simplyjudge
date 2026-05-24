"""Django admin configuration for the judging app."""

from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Photo, RubricCriterion, Score

@admin.register(Photo)
class PhotoAdmin(ImportExportModelAdmin):
    list_display = ('title', 'photographer_name', 'category', 'uploaded_at')
    list_filter = ('category',)

@admin.register(RubricCriterion)
class RubricAdmin(ImportExportModelAdmin):
    list_display = ('name', 'max_points', 'weight')

@admin.register(Score)
class ScoreAdmin(ImportExportModelAdmin):
    list_display = ('photo', 'judge', 'total_score', 'updated_at')
    list_filter = ('judge',)