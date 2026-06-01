from django.contrib import admin
from .models import Competition, CompetitionMembership, EntryOrder, RoundOneScore, RubricCriterion, Photo, PhotoStatusVote, Score, ZipImportJob

# --- SIMPLYJUDGE ADMIN BRANDING OVERRIDES ---
admin.site.site_header = "SimplyJudge Admin Engine"
admin.site.site_title = "SimplyJudge Administration"
admin.site.index_title = "Platform Administration Console"


def platform_admin_permission(request):
    return request.user.is_active and request.user.is_superuser


admin.site.has_permission = platform_admin_permission

class CompetitionMembershipInline(admin.TabularInline):
    model = CompetitionMembership
    extra = 0

@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug', 'workflow', 'entry_fee', 'is_active', 'created_at')
    list_filter = ('workflow', 'is_active')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    exclude = ('judges',)
    inlines = (CompetitionMembershipInline,)

@admin.register(CompetitionMembership)
class CompetitionMembershipAdmin(admin.ModelAdmin):
    list_display = ('id', 'competition', 'user', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active', 'competition')
    search_fields = ('competition__name', 'user__username', 'user__email')

@admin.register(EntryOrder)
class EntryOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'competition', 'amount_paid', 'is_paid', 'stripe_checkout_id', 'created_at')
    list_filter = ('is_paid', 'competition')
    search_fields = ('user__username', 'user__email', 'competition__name', 'stripe_checkout_id')
    readonly_fields = ('created_at',)

@admin.register(RubricCriterion)
class RubricCriterionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'competition', 'score_out_of', 'weight')
    list_filter = ('competition',)
    search_fields = ('name',)

class PhotoStatusVoteInline(admin.TabularInline):
    model = PhotoStatusVote
    extra = 0
    readonly_fields = ('voter', 'decision', 'created_at', 'updated_at')
    can_delete = False

class RoundOneScoreInline(admin.TabularInline):
    model = RoundOneScore
    extra = 0
    readonly_fields = ('judge', 'score', 'created_at', 'updated_at')
    can_delete = False

@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ('id', 'entry_code', 'title', 'competition', 'photographer_name', 'category', 'status')
    list_filter = ('competition', 'category', 'status')
    search_fields = ('entry_code', 'title', 'photographer_name', 'rule_flags')
    inlines = (PhotoStatusVoteInline, RoundOneScoreInline)

@admin.register(PhotoStatusVote)
class PhotoStatusVoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'photo', 'voter', 'decision', 'updated_at')
    list_filter = ('decision', 'photo__competition', 'voter')
    search_fields = ('photo__title', 'voter__username')

@admin.register(RoundOneScore)
class RoundOneScoreAdmin(admin.ModelAdmin):
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
