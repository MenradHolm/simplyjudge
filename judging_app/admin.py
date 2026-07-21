import csv
import io
import os
import zipfile

from django.contrib import admin, messages
from django.core.files.base import ContentFile
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Competition, CompetitionMembership, EntryOrder, RoundOneScore, RubricCriterion, Photo, PhotoStatusVote, Score, ZipImportJob
from .utils import send_automated_email
from .views import IMAGE_EXTENSIONS, normalize_match_key, prepare_image_for_cloudinary, truncate_text, unique_import_filename

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
    list_display = ('id', 'name', 'slug', 'workflow', 'entry_fee', 'emails_enabled', 'results_published', 'is_active', 'created_at')
    list_filter = ('workflow', 'emails_enabled', 'results_published', 'is_active')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    exclude = ('judges',)
    readonly_fields = ('photo_corrections_link',)
    fieldsets = (
        (None, {
            'fields': (
                'name',
                'slug',
                'workflow',
                'entry_fee',
                'emails_enabled',
                'results_published',
                'is_active',
                'judge_invite_token',
                'tie_breaker_criterion',
                'photo_corrections_link',
            ),
        }),
    )
    inlines = (CompetitionMembershipInline,)
    actions = ('publish_competition_results',)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:competition_id>/photo-corrections/',
                self.admin_site.admin_view(self.photo_corrections_view),
                name='judging_app_competition_photo_corrections',
            ),
        ]
        return custom_urls + urls

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['photo_corrections_url'] = reverse(
            'admin:judging_app_competition_photo_corrections',
            args=[object_id],
        )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    @admin.display(description='Photo correction tools')
    def photo_corrections_link(self, obj):
        if not obj or not obj.pk:
            return '-'
        url = reverse('admin:judging_app_competition_photo_corrections', args=[obj.pk])
        return format_html('<a class="button" href="{}">Open photo corrections</a>', url)

    def photo_corrections_view(self, request, competition_id):
        competition = get_object_or_404(Competition, id=competition_id)

        if request.method == 'GET' and request.GET.get('export') == '1':
            return self.export_photo_corrections_csv(competition)

        result = None
        if request.method == 'POST':
            action = request.POST.get('action', 'metadata')
            apply_changes = request.POST.get('apply') == '1'
            if action == 'images':
                images_zip = request.FILES.get('images_zip')
                if not images_zip:
                    messages.error(request, 'Choose a ZIP of original images before running the image restore.')
                else:
                    result = self.process_original_images_zip(competition, images_zip, apply_changes)
                    level = messages.SUCCESS if apply_changes else messages.WARNING
                    messages.add_message(
                        request,
                        level,
                        (
                            f"{'Restored' if apply_changes else 'Image restore dry run complete'}: "
                            f"{result['update_count']} photo image(s) matched."
                        ),
                    )
            else:
                corrections_file = request.FILES.get('corrections_file')
                if not corrections_file:
                    messages.error(request, 'Choose a corrections CSV before running the repair.')
                else:
                    result = self.process_photo_corrections_csv(competition, corrections_file, apply_changes)
                    level = messages.SUCCESS if apply_changes else messages.WARNING
                    messages.add_message(
                        request,
                        level,
                        (
                            f"{'Applied' if apply_changes else 'Dry run complete'}: "
                            f"{result['update_count']} photo(s) with metadata changes."
                        ),
                    )

        context = {
            **self.admin_site.each_context(request),
            'title': f'Photo metadata corrections: {competition.name}',
            'competition': competition,
            'export_url': f"{reverse('admin:judging_app_competition_photo_corrections', args=[competition.id])}?export=1",
            'result': result,
        }
        return render(request, 'admin/photo_corrections.html', context)

    def export_photo_corrections_csv(self, competition):
        fields = [
            'photo_id',
            'image_name',
            'image_url',
            'image_preview_formula',
            'entry_code',
            'title',
            'photographer_name',
            'photographer_email',
            'category',
            'description',
            'camera_settings',
        ]
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{competition.slug}-photo-corrections.csv"'
        writer = csv.DictWriter(response, fieldnames=fields)
        writer.writeheader()
        for photo in Photo.objects.filter(competition=competition).order_by('id'):
            try:
                image_url = photo.image.url if photo.image else ''
            except ValueError:
                image_url = ''
            writer.writerow({
                'photo_id': photo.id,
                'image_name': photo.image.name if photo.image else '',
                'image_url': image_url,
                'image_preview_formula': f'=IMAGE("{image_url}")' if image_url else '',
                'entry_code': photo.entry_code or '',
                'title': photo.title or '',
                'photographer_name': photo.photographer_name or '',
                'photographer_email': photo.photographer_email or '',
                'category': photo.category or '',
                'description': photo.description or '',
                'camera_settings': photo.camera_settings or '',
            })
        return response

    def process_photo_corrections_csv(self, competition, corrections_file, apply_changes):
        text = corrections_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or 'photo_id' not in reader.fieldnames:
            raise ValueError('Corrections CSV must include a photo_id column.')

        rows = list(enumerate(reader, start=2))
        photo_ids = []
        for row_number, row in rows:
            raw_photo_id = (row.get('photo_id') or '').strip()
            if not raw_photo_id.isdigit():
                raise ValueError(f'Row {row_number}: photo_id is required and must be numeric.')
            photo_ids.append(int(raw_photo_id))

        duplicate_ids = sorted({photo_id for photo_id in photo_ids if photo_ids.count(photo_id) > 1})
        if duplicate_ids:
            raise ValueError(f'Duplicate photo_id value(s): {", ".join(map(str, duplicate_ids[:10]))}')

        photos_by_id = {
            photo.id: photo
            for photo in Photo.objects.filter(competition=competition, id__in=photo_ids)
        }
        missing_ids = sorted(set(photo_ids) - set(photos_by_id))
        if missing_ids:
            raise ValueError(f'Photo ID(s) not found in this competition: {", ".join(map(str, missing_ids[:10]))}')

        updates = []
        fields = [
            'entry_code',
            'title',
            'photographer_name',
            'photographer_email',
            'category',
            'description',
            'camera_settings',
        ]
        for row_number, row in rows:
            photo = photos_by_id[int((row.get('photo_id') or '').strip())]
            changes = {}
            for field in fields:
                if field not in row:
                    continue
                incoming = self.clean_correction_value(field, row.get(field))
                current = getattr(photo, field) or ''
                if current != incoming:
                    changes[field] = (current, incoming)
            if changes:
                updates.append({'photo': photo, 'row_number': row_number, 'changes': changes})

        if apply_changes:
            for update in updates:
                photo = update['photo']
                update_fields = []
                for field, (_old, new) in update['changes'].items():
                    setattr(photo, field, new)
                    update_fields.append(field)
                photo.save(update_fields=update_fields)

        return {
            'kind': 'metadata',
            'apply': apply_changes,
            'update_count': len(updates),
            'updates': updates[:50],
            'truncated_count': max(len(updates) - 50, 0),
        }

    def process_original_images_zip(self, competition, images_zip, apply_changes):
        image_members = {}
        unmatched_files = []
        duplicate_file_keys = set()
        updates = []

        with zipfile.ZipFile(images_zip) as package:
            for info in package.infolist():
                filename = os.path.basename(info.filename)
                if info.is_dir() or not filename or filename.startswith('.'):
                    continue
                _stem, ext = os.path.splitext(filename)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                key = normalize_match_key(filename)
                if key in image_members:
                    duplicate_file_keys.add(key)
                    continue
                image_members[key] = info

            if duplicate_file_keys:
                raise ValueError(
                    'The ZIP has duplicate image filename keys: '
                    f'{", ".join(sorted(duplicate_file_keys)[:10])}'
                )

            photos = list(Photo.objects.filter(competition=competition).order_by('id'))
            for key, info in image_members.items():
                matches = [
                    photo for photo in photos
                    if self.photo_matches_original_image_key(photo, key)
                ]
                if len(matches) != 1:
                    unmatched_files.append((info.filename, len(matches)))
                    continue

                photo = matches[0]
                updates.append({
                    'photo': photo,
                    'source_filename': info.filename,
                    'current_image_name': photo.image.name if photo.image else '',
                    'new_filename': unique_import_filename(f'restore{competition.id}', photo.id, info.filename),
                    'zip_info': info,
                })

            if apply_changes:
                for update in updates:
                    image_bytes = package.read(update['zip_info'].filename)
                    storage_image = prepare_image_for_cloudinary(image_bytes, update['new_filename'])
                    update['photo'].image = ContentFile(storage_image['bytes'], name=storage_image['filename'])
                    update['photo'].save(update_fields=['image'])

        return {
            'kind': 'images',
            'apply': apply_changes,
            'update_count': len(updates),
            'updates': updates[:50],
            'truncated_count': max(len(updates) - 50, 0),
            'unmatched_files': unmatched_files[:50],
            'unmatched_truncated_count': max(len(unmatched_files) - 50, 0),
        }

    def photo_matches_original_image_key(self, photo, original_key):
        image_name = photo.image.name if photo.image else ''
        if not image_name:
            return False
        photo_key = normalize_match_key(image_name)
        return photo_key == original_key or photo_key.endswith(original_key)

    def clean_correction_value(self, field, value):
        value = str(value or '').strip()
        if field == 'entry_code':
            return truncate_text(value, 120)
        if field == 'title':
            return truncate_text(value or 'Untitled', 200)
        if field == 'photographer_name':
            return truncate_text(value or 'Unknown', 200)
        if field == 'category':
            return truncate_text(value or 'General', 100)
        return value

    @admin.action(description='Publish results and email shortlisted photographers')
    def publish_competition_results(self, request, queryset):
        total_shortlisted = 0
        sent_count = 0
        skipped_without_email = 0
        suppressed_count = 0

        for competition in queryset:
            competition.results_published = True
            competition.save(update_fields=['results_published'])

            shortlisted_photos = Photo.objects.filter(
                competition=competition,
                status=Photo.Status.SHORTLISTED,
            )
            for photo in shortlisted_photos:
                total_shortlisted += 1
                if not photo.photographer_email:
                    skipped_without_email += 1
                    continue

                result = send_automated_email(
                    competition=competition,
                    subject=f'Congratulations from {competition.name}',
                    template_name='emails/congratulations.txt',
                    context={'photo': photo},
                    recipient_list=[photo.photographer_email],
                )
                if result:
                    sent_count += 1
                else:
                    suppressed_count += 1

        self.message_user(
            request,
            (
                f'Results published. Shortlisted photos: {total_shortlisted}. '
                f'Emails sent: {sent_count}. '
                f'Suppressed by email safety switch: {suppressed_count}. '
                f'Skipped without photographer email: {skipped_without_email}.'
            ),
        )

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
    list_display = ('id', 'entry_code', 'title', 'competition', 'photographer_name', 'photographer_email', 'category', 'status', 'is_raw_verified')
    list_filter = ('competition', 'category', 'status', 'is_raw_verified')
    search_fields = ('entry_code', 'title', 'photographer_name', 'photographer_email', 'rule_flags', 'exif_warning_flag')
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
