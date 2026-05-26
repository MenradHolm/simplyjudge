from django.contrib import admin
from .models import Competition, GutCheckScore, RubricCriterion, Photo, PhotoStatusVote, Score, ZipImportJob

# --- SIMPLYJUDGE ADMIN BRANDING OVERRIDES ---
admin.site.site_header = "SimplyJudge Admin Engine"
admin.site.site_title = "SimplyJudge Administration"
admin.site.index_title = "Platform Administration Console"

@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug', 'is_active', 'created_at')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)} 
    filter_horizontal = ('judges',)

@admin.register(RubricCriterion)
class RubricCriterionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'competition', 'weight')
    list_filter = ('competition',)
    search_fields = ('name',)

class PhotoStatusVoteInline(admin.TabularInline):
    model = PhotoStatusVote
    extra = 0
    readonly_fields = ('voter', 'decision', 'created_at', 'updated_at')
    can_delete = False

class GutCheckScoreInline(admin.TabularInline):
    model = GutCheckScore
    extra = 0
    readonly_fields = ('judge', 'score', 'created_at', 'updated_at')
    can_delete = False

@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'competition', 'photographer_name', 'category', 'status')
    list_filter = ('competition', 'category', 'status')
    search_fields = ('title', 'photographer_name', 'rule_flags')
    inlines = (PhotoStatusVoteInline, GutCheckScoreInline)

@admin.register(PhotoStatusVote)
class PhotoStatusVoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'photo', 'voter', 'decision', 'updated_at')
    list_filter = ('decision', 'photo__competition', 'voter')
    search_fields = ('photo__title', 'voter__username')

@admin.register(GutCheckScore)
class GutCheckScoreAdmin(admin.ModelAdmin):
    list_display = ('id', 'photo', 'judge', 'score', 'updated_at')
    list_filter = ('score', 'photo__competition', 'judge')
    search_fields = ('photo__title', 'judge__username')

@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    list_display = ('id', 'photo', 'judge', 'total_score')
    list_filter = ('photo__competition', 'judge')
    search_fields = ('photo__title', 'judge__username')

@admin.register(ZipImportJob)
class ZipImportJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'source_name', 'competition', 'status', 'processed_rows', 'total_rows', 'matched_images', 'created_at', 'finished_at')
    list_filter = ('status', 'competition')
    search_fields = ('source_name', 'error_message')
    readonly_fields = (
        'competition', 'uploaded_by', 'source_name', 'source_url', 'temp_path', 'status',
        'total_rows', 'processed_rows', 'matched_images', 'error_message',
        'created_at', 'updated_at', 'finished_at',
    )
