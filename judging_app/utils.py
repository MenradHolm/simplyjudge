from io import BytesIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Avg, Count, StdDev
from django.template.loader import render_to_string
from PIL import Image
from PIL.ExifTags import TAGS

from .models import Score

EXIF_FIELDS = {
    'camera_model': ('Model',),
    'original_datetime': ('DateTimeOriginal', 'DateTime'),
    'focal_length': ('FocalLength',),
    'exposure_time': ('ExposureTime',),
}


def send_automated_email(
    competition,
    subject,
    template_name,
    context,
    recipient_list,
    from_email=None,
    fail_silently=False,
    html_template_name=None,
):
    if not competition.emails_enabled:
        return False

    email_context = {
        **(context or {}),
        'competition': competition,
    }
    message = render_to_string(template_name, email_context).strip()
    html_message = None
    if html_template_name:
        html_message = render_to_string(html_template_name, email_context)

    return send_mail(
        subject,
        message,
        from_email or getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        recipient_list,
        fail_silently=fail_silently,
        html_message=html_message,
    )


def calculate_judge_calibration(competition_id):
    scores = Score.objects.filter(photo__competition_id=competition_id)
    overall_stats = scores.aggregate(
        average=Avg('total_score'),
        standard_deviation=StdDev('total_score'),
        score_count=Count('id'),
    )
    overall_average = overall_stats['average']
    standard_deviation = overall_stats['standard_deviation'] or 0

    if overall_average is None:
        return {
            'competition_id': competition_id,
            'overall_average': None,
            'standard_deviation': None,
            'threshold': None,
            'judges': [],
            'flagged_judges': [],
        }

    threshold = standard_deviation * 1.5
    judge_rows = (
        scores.values('judge_id', 'judge__username', 'judge__first_name', 'judge__last_name')
        .annotate(average=Avg('total_score'), score_count=Count('id'))
        .order_by('average')
    )

    judges = []
    flagged_judges = []
    for row in judge_rows:
        judge_average = row['average'] or 0
        deviation = judge_average - overall_average
        z_score = deviation / standard_deviation if standard_deviation else 0
        is_flagged = bool(standard_deviation and abs(deviation) > threshold)
        judge_name = ' '.join(
            part for part in [row['judge__first_name'], row['judge__last_name']] if part
        ).strip() or row['judge__username']
        direction = ''
        if is_flagged:
            direction = 'lenient' if deviation > 0 else 'harsh'

        judge_result = {
            'judge_id': row['judge_id'],
            'judge_username': row['judge__username'],
            'judge_name': judge_name,
            'average': judge_average,
            'score_count': row['score_count'],
            'deviation': deviation,
            'z_score': z_score,
            'is_flagged': is_flagged,
            'direction': direction,
        }
        judges.append(judge_result)
        if is_flagged:
            flagged_judges.append(judge_result)

    return {
        'competition_id': competition_id,
        'overall_average': overall_average,
        'standard_deviation': standard_deviation,
        'threshold': threshold,
        'score_count': overall_stats['score_count'],
        'judges': judges,
        'flagged_judges': flagged_judges,
    }


def normalize_exif_value(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', errors='ignore').strip()
        except UnicodeDecodeError:
            return repr(value)
    if isinstance(value, (tuple, list)):
        return '/'.join(normalize_exif_value(item) for item in value)
    numerator = getattr(value, 'numerator', None)
    denominator = getattr(value, 'denominator', None)
    if numerator is not None and denominator:
        return f'{numerator}/{denominator}'
    return str(value).strip()


def read_file_field_bytes(file_field):
    if not file_field:
        return b''

    file_url = getattr(file_field, 'url', '')
    parsed_url = urlparse(file_url)
    if parsed_url.scheme in {'http', 'https'}:
        request = Request(file_url, headers={'User-Agent': 'SimplyJudge RAW verifier'})
        with urlopen(request, timeout=20) as response:
            return response.read()

    file_field.open('rb')
    try:
        return file_field.read()
    finally:
        file_field.close()


def extract_exif_metadata(file_bytes):
    if not file_bytes:
        return {}, 'File is empty.'

    try:
        with Image.open(BytesIO(file_bytes)) as image:
            raw_exif = image.getexif()
            if not raw_exif:
                return {}, 'No embedded EXIF metadata found.'

            named_exif = {}
            for tag_id, value in raw_exif.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                named_exif[tag_name] = value

            metadata = {}
            missing_fields = []
            for field_name, tag_names in EXIF_FIELDS.items():
                value = ''
                for tag_name in tag_names:
                    value = normalize_exif_value(named_exif.get(tag_name))
                    if value:
                        break
                metadata[field_name] = value
                if not value:
                    missing_fields.append(field_name.replace('_', ' '))

            if missing_fields:
                return metadata, f"Missing EXIF field(s): {', '.join(missing_fields)}."
            return metadata, ''
    except Exception as exc:
        return {}, f'Could not read EXIF metadata: {exc}'


def compare_exif_data(photo_instance):
    failures = []

    if not photo_instance.image:
        failures.append('Original submitted image is missing.')
        original_metadata = {}
    else:
        original_bytes = read_file_field_bytes(photo_instance.image)
        original_metadata, original_error = extract_exif_metadata(original_bytes)
        if original_error:
            failures.append(f'Original image: {original_error}')

    if not photo_instance.raw_file:
        failures.append('RAW file is missing.')
        raw_metadata = {}
    else:
        raw_bytes = read_file_field_bytes(photo_instance.raw_file)
        raw_metadata, raw_error = extract_exif_metadata(raw_bytes)
        if raw_error:
            failures.append(f'RAW file: {raw_error}')

    for field_name in EXIF_FIELDS:
        original_value = original_metadata.get(field_name, '')
        raw_value = raw_metadata.get(field_name, '')
        if not original_value or not raw_value:
            continue
        if original_value != raw_value:
            label = field_name.replace('_', ' ')
            failures.append(
                f'{label} mismatch: original "{original_value}" vs RAW "{raw_value}".'
            )

    if failures:
        photo_instance.is_raw_verified = False
        photo_instance.exif_warning_flag = ' '.join(failures)
    else:
        photo_instance.is_raw_verified = True
        photo_instance.exif_warning_flag = ''

    photo_instance.save(update_fields=['is_raw_verified', 'exif_warning_flag'])
    return photo_instance.is_raw_verified
