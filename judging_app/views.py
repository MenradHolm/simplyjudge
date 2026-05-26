import csv
import os
import re
import tempfile
import uuid
import zipfile
import io
import threading
from PIL import Image
from PIL.ExifTags import TAGS

from django.contrib import messages
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import close_old_connections, transaction
from django.db.models import Avg, FloatField
from django.db.models.functions import Cast
from django.utils import timezone

from .models import Competition, Photo, Score, RubricCriterion, ZipImportJob

def register_user(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'judging_app/register.html', {'form': form})

def is_approved_judge(user, competition):
    if user.is_superuser:
        return True
    return competition.judges.filter(id=user.id).exists()

def home_hub(request):
    active_competitions = Competition.objects.filter(is_active=True).order_by('-created_at')
    return render(request, 'judging_app/home.html', {'competitions': active_competitions})

@login_required(login_url='/accounts/login/')
def judge_router(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    next_photo = Photo.objects.filter(competition=competition).exclude(score__judge=request.user).first()
    if next_photo:
        return redirect('judge_photo', comp_slug=competition.slug, photo_id=next_photo.id)
    return render(request, 'judging_app/done.html', {'competition': competition})

@login_required(login_url='/accounts/login/')
def judge_photo(request, comp_slug, photo_id):
    competition = get_object_or_404(Competition, slug=comp_slug)
    if not is_approved_judge(request.user, competition):
        return render(request, 'judging_app/pending.html')
    photo = get_object_or_404(Photo, id=photo_id, competition=competition)
    rubric = RubricCriterion.objects.filter(competition=competition)
    total_photos = Photo.objects.filter(competition=competition).count()
    scored_photos = Score.objects.filter(photo__competition=competition, judge=request.user).count()
    
    if request.method == "POST":
        criteria_scores = {}
        total_score = 0.0
        for criterion in rubric:
            val = request.POST.get(f'criterion_{criterion.id}', 0)
            try:
                score_val = float(val)
            except ValueError:
                score_val = 0.0
            criteria_scores[str(criterion.id)] = score_val
            total_score += (score_val * criterion.weight)
        comment = request.POST.get('comment', '')
        Score.objects.update_or_create(
            photo=photo,
            judge=request.user,
            defaults={'criteria_scores': criteria_scores, 'total_score': total_score, 'comment': comment}
        )
        return redirect('judge_router', comp_slug=competition.slug)

    context = {
        'competition': competition,
        'photo': photo,
        'rubric': rubric,
        'progress': f"{scored_photos + 1} / {total_photos}"
    }
    return render(request, 'judging_app/judge.html', context)

@login_required(login_url='/accounts/login/')
def leaderboard(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    tie_criterion = competition.tie_breaker_criterion
    photos_query = Photo.objects.filter(competition=competition).annotate(average_score=Avg('score__total_score'))
    if tie_criterion:
        ranked_photos = photos_query.annotate(
            tie_breaker_score=Avg(Cast(f'score__criteria_scores__{tie_criterion.id}', FloatField()))
        ).filter(average_score__isnull=False).order_by('-average_score', '-tie_breaker_score')
    else:
        ranked_photos = photos_query.filter(average_score__isnull=False).order_by('-average_score')
    return render(request, 'judging_app/leaderboard.html', {'competition': competition, 'photos': ranked_photos, 'tie_criterion': tie_criterion})

def submit_photo(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    error_message = None
    if request.method == "POST":
        title = request.POST.get('title')
        photographer_name = request.POST.get('photographer_name')
        category = request.POST.get('category')
        image = request.FILES.get('image')
        description = request.POST.get('description', '')
        camera_settings = request.POST.get('camera_settings', '')
        if title and photographer_name and category and image:
            if image.size > 5 * 1024 * 1024:
                error_message = "The uploaded file is too large! Please keep your photo under 5 MB."
            else:
                Photo.objects.create(
                    competition=competition, title=title, photographer_name=photographer_name,
                    category=category, image=image, description=description, camera_settings=camera_settings
                )
                return render(request, 'judging_app/submit_success.html', {'competition': competition})
    return render(request, 'judging_app/submit.html', {'competition': competition, 'error_message': error_message})

@login_required(login_url='/accounts/login/')
def feedback_report(request, comp_slug):
    if not request.user.is_staff:
        return redirect('home_hub')
    competition = get_object_or_404(Competition, slug=comp_slug)
    photos = list(Photo.objects.filter(competition=competition))
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]
    return render(request, 'judging_app/feedback_report.html', {'competition': competition, 'photos': photos})

@login_required(login_url='/accounts/login/')
def upload_spreadsheet(request, comp_slug):
    if not request.user.is_staff:
        return redirect('home_hub')

    competition = get_object_or_404(Competition, slug=comp_slug)

    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Error: This is not a CSV file!')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        try:
            file_data = csv_file.read().decode('utf-8').splitlines()
            reader = csv.DictReader(file_data)
            headers = [h.lower().strip() for h in reader.fieldnames] if reader.fieldnames else []
            
            if 'criterion name' in headers or 'criterion' in headers:
                rubric_count = 0
                for row in reader:
                    name = row.get('Criterion Name') or row.get('criterion name') or row.get('Criterion') or row.get('criterion')
                    desc = row.get('Description') or row.get('description') or ''
                    weight_val = row.get('Weight') or row.get('weight') or '1.0'
                    
                    if not name:
                        continue
                        
                    RubricCriterion.objects.create(
                        competition=competition,
                        name=name.strip(),
                        description=desc.strip(),
                        weight=float(weight_val.strip())
                    )
                    rubric_count += 1
                
                messages.success(request, f'Successfully built a dynamic {rubric_count}-column rubric matrix!')
                return redirect('home_hub')

            else:
                import_count = 0
                for row in reader:
                    title = row.get('Title') or row.get('title') or 'Untitled'
                    photographer = row.get('Photographer') or row.get('photographer') or 'Unknown'
                    category = row.get('Category') or row.get('category') or 'General'
                    custom_code = row.get('Code') or row.get('ID') or row.get('Number') or row.get('id')
                    desc = row.get('Description') or row.get('description') or row.get('Story') or row.get('story') or ''
                    
                    # Look for settings column
                    cam_settings = row.get('Camera Settings') or row.get('camera settings') or row.get('Settings') or row.get('settings') or ''

                    if not custom_code:
                        continue

                    Photo.objects.create(
                        id=int(custom_code.strip()), competition=competition, title=title,
                        photographer_name=photographer, category=category,
                        image='competition_photos/placeholder.jpg', description=desc, camera_settings=cam_settings
                    )
                    import_count += 1
                
                messages.success(request, f'Successfully imported {import_count} entries into the judging queue!')
                return redirect('feedback_report', comp_slug=competition.slug)

        except Exception as e:
            messages.error(request, f'Error parsing spreadsheet data: {str(e)}')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

    recent_jobs = ZipImportJob.objects.filter(competition=competition)[:5]
    return render(request, 'judging_app/upload_spreadsheet.html', {'competition': competition, 'recent_jobs': recent_jobs})

@login_required(login_url='/accounts/login/')
def upload_photos_zip(request, comp_slug):
    if not request.user.is_staff:
        return redirect('home_hub')
    competition = get_object_or_404(Competition, slug=comp_slug)
    if request.method == 'POST' and request.FILES.get('zip_file'):
        zip_file = request.FILES['zip_file']
        if not zip_file.name.lower().endswith('.zip'):
            messages.error(request, 'Please upload a .zip package containing EntryForm.csv and the photo files.')
            return redirect('upload_spreadsheet', comp_slug=competition.slug)

        job = ZipImportJob.objects.create(
            competition=competition,
            uploaded_by=request.user,
            source_name=zip_file.name,
        )
        job.temp_path = save_uploaded_zip_to_temp_file(zip_file, job.id)
        job.save(update_fields=['temp_path', 'updated_at'])

        worker = threading.Thread(
            target=process_entry_zip_job,
            args=(job.id,),
            daemon=True,
        )
        worker.start()
        messages.success(
            request,
            'ZIP sync job created. SimplyJudge is matching images and importing entries in the background.',
        )
        return redirect('zip_import_status', comp_slug=competition.slug, job_id=job.id)
    return redirect('upload_spreadsheet', comp_slug=competition.slug)

@login_required(login_url='/accounts/login/')
def zip_import_status(request, comp_slug, job_id):
    if not request.user.is_staff:
        return redirect('home_hub')
    competition = get_object_or_404(Competition, slug=comp_slug)
    job = get_object_or_404(ZipImportJob, id=job_id, competition=competition)
    return render(request, 'judging_app/zip_import_status.html', {'competition': competition, 'job': job})

def public_results(request, comp_slug):
    competition = get_object_or_404(Competition, slug=comp_slug)
    photos = list(Photo.objects.filter(competition=competition))
    all_scores = Score.objects.filter(photo__competition=competition).select_related('judge')
    for photo in photos:
        photo.judge_scores = [s for s in all_scores if s.photo_id == photo.id]
    return render(request, 'judging_app/feedback_report.html', {'competition': competition, 'photos': photos})

def save_uploaded_zip_to_temp_file(uploaded_file, job_id):
    temp_dir = os.path.join(tempfile.gettempdir(), 'simplyjudge_zip_imports')
    os.makedirs(temp_dir, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(uploaded_file.name))
    target_path = os.path.join(temp_dir, f'{job_id}_{uuid.uuid4().hex}_{safe_name}')
    with open(target_path, 'wb') as target:
        for chunk in uploaded_file.chunks():
            target.write(chunk)
    return target_path

def clean_cell(row, *names, default=''):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    lower_row = {str(k).lower().strip(): v for k, v in row.items() if k is not None}
    for name in names:
        value = lower_row.get(name.lower().strip())
        if value is not None and str(value).strip():
            return str(value).strip()
    return default

def normalize_match_key(value):
    stem = os.path.splitext(os.path.basename(str(value).strip()))[0]
    return re.sub(r'[^a-z0-9]', '', stem.lower())

def find_entry_csv(zip_file):
    csv_members = [
        info for info in zip_file.infolist()
        if not info.is_dir()
        and not os.path.basename(info.filename).startswith('.')
        and info.filename.lower().endswith('.csv')
    ]
    if not csv_members:
        raise ValueError('No CSV file found in the ZIP package.')
    for info in csv_members:
        if os.path.basename(info.filename).lower() == 'entryform.csv':
            return info
    return csv_members[0]

def collect_zip_image_index(zip_file):
    image_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
    images = {}
    for info in zip_file.infolist():
        filename = os.path.basename(info.filename)
        if info.is_dir() or not filename or filename.startswith('.'):
            continue
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in image_extensions:
            continue
        images[normalize_match_key(stem)] = info
    return images

def parse_entry_rows(csv_bytes):
    text = csv_bytes.decode('utf-8-sig').splitlines()
    reader = csv.DictReader(text)
    if not reader.fieldnames:
        raise ValueError('The CSV has no header row.')
    return list(reader)

def process_entry_zip_job(job_id):
    close_old_connections()
    job = None
    try:
        job = ZipImportJob.objects.select_related('competition').get(id=job_id)
        job.status = ZipImportJob.Status.PROCESSING
        job.error_message = ''
        job.save(update_fields=['status', 'error_message', 'updated_at'])

        with zipfile.ZipFile(job.temp_path) as package:
            csv_info = find_entry_csv(package)
            rows = parse_entry_rows(package.read(csv_info.filename))
            images = collect_zip_image_index(package)

            job.total_rows = len(rows)
            job.save(update_fields=['total_rows', 'updated_at'])

            with transaction.atomic():
                imported = 0
                matched = 0
                for row in rows:
                    title = clean_cell(row, 'Title', 'title', default='Untitled')
                    photographer = clean_cell(row, 'Photographer', 'photographer', 'Photographer Name', 'photographer_name', default='Unknown')
                    category = clean_cell(row, 'Category', 'category', default='General')
                    entry_code = clean_cell(row, 'Code', 'ID', 'Number', 'Entry ID', 'Entry Code', 'id')
                    image_reference = clean_cell(row, 'Image', 'Image File', 'Filename', 'File Name', 'Photo File', 'Photo Filename', 'Asset')
                    description = clean_cell(row, 'Description', 'description', 'Story', 'story')
                    camera_settings = clean_cell(row, 'Camera Settings', 'camera settings', 'Settings', 'settings')

                    match_candidates = [image_reference, entry_code, title]
                    image_payload = None
                    for candidate in match_candidates:
                        if candidate:
                            image_info = images.get(normalize_match_key(candidate))
                            if image_info:
                                image_payload = {
                                    'filename': os.path.basename(image_info.filename),
                                    'bytes': package.read(image_info.filename),
                                }
                                break

                    defaults = {
                        'competition': job.competition,
                        'title': title,
                        'photographer_name': photographer,
                        'category': category,
                        'description': description,
                        'camera_settings': camera_settings,
                    }

                    if image_payload:
                        audit = audit_photo_metadata(image_payload['bytes'])
                        defaults['rule_flags'] = ' | '.join(audit['flags']) if audit['flags'] else ''
                        defaults['image'] = ContentFile(image_payload['bytes'], name=image_payload['filename'])
                        matched += 1
                    else:
                        defaults['image'] = 'competition_photos/placeholder.jpg'
                        defaults['rule_flags'] = 'No matching image file found in uploaded ZIP package.'

                    if entry_code:
                        try:
                            photo_id = int(entry_code.strip())
                        except ValueError:
                            photo_id = None
                    else:
                        photo_id = None

                    if photo_id is not None:
                        existing = Photo.objects.filter(id=photo_id).first()
                        if existing and existing.competition_id != job.competition_id:
                            raise ValueError(f'Entry code {photo_id} already belongs to another competition.')
                        if existing and not image_payload:
                            defaults.pop('image', None)
                        Photo.objects.update_or_create(id=photo_id, defaults=defaults)
                    else:
                        Photo.objects.update_or_create(
                            competition=job.competition,
                            title=title,
                            photographer_name=photographer,
                            defaults=defaults,
                        )
                    imported += 1

        job.status = ZipImportJob.Status.COMPLETED
        job.processed_rows = imported
        job.matched_images = matched
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'processed_rows', 'matched_images', 'finished_at', 'updated_at'])
        print(f'SimplyJudge ZIP sync completed for {job.source_name}: {imported} rows, {matched} images matched.')
    except Exception as exc:
        if job is not None:
            job.status = ZipImportJob.Status.FAILED
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
        print(f'SimplyJudge ZIP sync failed for job {job_id}: {exc}')
    finally:
        if job is not None and job.temp_path and os.path.exists(job.temp_path):
            try:
                os.remove(job.temp_path)
            except OSError:
                pass
        close_old_connections()

def audit_photo_metadata(file_bytes):
    try:
        img = Image.open(io.BytesIO(file_bytes))
        exif_raw = img.getexif()
        audit_results = {'date_valid': False, 'has_gps': False, 'flags': []}
        if not exif_raw:
            audit_results['flags'].append("No EXIF data found (Likely stripped by editing software/Wix).")
            return audit_results
        for tag_id, value in exif_raw.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'DateTimeOriginal':
                year = str(value).split(':')[0]
                if year in ['2025', '2026']:
                    audit_results['date_valid'] = True
                else:
                    audit_results['flags'].append(f"Taken outside valid date range: {year}")
                break
        gps_info = exif_raw.get_ifd(0x8825)
        if gps_info:
            audit_results['has_gps'] = True
        else:
            audit_results['flags'].append("No GPS location data found (Privacy filter applied).")
        return audit_results
    except Exception as e:
        return {'date_valid': False, 'has_gps': False, 'flags': ["Corrupted file or unable to read metadata."]}
