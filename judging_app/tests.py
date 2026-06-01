import io
import tempfile
import zipfile
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from types import SimpleNamespace

from .models import Competition, CompetitionMembership, Photo, PhotoStatusVote, RoundOneScore, RubricCriterion, Score, ZipImportJob, competition_photo_upload_path
from .middleware import UserTimezoneMiddleware
from .views import collect_photo_rule_flags, decode_csv_bytes, find_matching_image, normalize_match_key, prepare_image_for_cloudinary, process_photos_only_zip_job


class PhotoStatusWorkflowTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(name='Youth POTY', slug='youth-poty')
        self.guest_judge = User.objects.create_user(username='judge', password='test-pass')
        self.internal_judge = User.objects.create_user(username='reviewer', password='test-pass')
        self.organizer = User.objects.create_user(username='organizer', password='test-pass')
        CompetitionMembership.objects.create(
            competition=self.competition,
            user=self.guest_judge,
            role=CompetitionMembership.Role.VIP_JUDGE,
        )
        CompetitionMembership.objects.create(
            competition=self.competition,
            user=self.internal_judge,
            role=CompetitionMembership.Role.INTERNAL_JUDGE,
        )
        CompetitionMembership.objects.create(
            competition=self.competition,
            user=self.organizer,
            role=CompetitionMembership.Role.ORGANIZER,
        )

    def create_photo(self, title, status, **overrides):
        defaults = {
            'competition': self.competition,
            'title': title,
            'photographer_name': 'Hidden Entrant',
            'category': 'General',
            'image': 'competition_photos/placeholder.jpg',
            'status': status,
        }
        defaults.update(overrides)
        return Photo.objects.create(
            **defaults,
        )

    def test_guest_judge_router_only_serves_shortlisted_photos(self):
        self.create_photo('Pending image', Photo.Status.PENDING)
        self.create_photo('Round 1 image', Photo.Status.ROUND_1)
        shortlisted = self.create_photo('Shortlisted image', Photo.Status.SHORTLISTED)
        self.create_photo('Rejected image', Photo.Status.REJECTED)

        self.client.force_login(self.guest_judge)
        response = self.client.get(reverse('judge_router', args=[self.competition.slug]))

        self.assertRedirects(
            response,
            reverse('judge_photo', args=[self.competition.slug, shortlisted.id]),
            fetch_redirect_response=False,
        )

    def test_guest_judge_cannot_open_pending_photo_directly(self):
        pending = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.guest_judge)
        response = self.client.get(reverse('judge_photo', args=[self.competition.slug, pending.id]))

        self.assertEqual(response.status_code, 404)

    def test_judge_can_review_and_update_submitted_scores(self):
        criterion = RubricCriterion.objects.create(
            competition=self.competition,
            name='Composition',
            description='Visual strength',
            weight=1.0,
        )
        photo = self.create_photo('Shortlisted image', Photo.Status.SHORTLISTED, entry_code='PRIVATE-FILE-001')
        Score.objects.create(
            photo=photo,
            judge=self.guest_judge,
            criteria_scores={str(criterion.id): 72.5},
            total_score=72.5,
            comment='Initial calibration note.',
        )

        self.client.force_login(self.guest_judge)
        review_response = self.client.get(reverse('judge_review', args=[self.competition.slug]))

        self.assertContains(review_response, 'My submitted scores')
        self.assertContains(review_response, 'SimplyJudge ID: #')
        self.assertContains(review_response, 'Edit Score')
        self.assertNotContains(review_response, 'PRIVATE-FILE-001')

        edit_response = self.client.get(
            f"{reverse('judge_photo', args=[self.competition.slug, photo.id])}?return=review"
        )

        self.assertContains(edit_response, 'value="72.5"')
        self.assertContains(edit_response, 'Initial calibration note.')
        self.assertContains(edit_response, 'Update Evaluation')
        self.assertContains(edit_response, 'Image zoom controls')
        self.assertNotContains(edit_response, 'PRIVATE-FILE-001')

        post_response = self.client.post(
            f"{reverse('judge_photo', args=[self.competition.slug, photo.id])}?return=review",
            {
                f'criterion_{criterion.id}': '88',
                'comment': 'Adjusted after seeing the full field.',
                'return_to': 'review',
            },
        )

        self.assertRedirects(post_response, reverse('judge_review', args=[self.competition.slug]))
        score = Score.objects.get(photo=photo, judge=self.guest_judge)
        self.assertEqual(score.total_score, 88)
        self.assertEqual(score.criteria_scores[str(criterion.id)], 88.0)
        self.assertEqual(score.comment, 'Adjusted after seeing the full field.')

    def test_rubric_score_out_of_controls_judge_scale_and_normalized_total(self):
        criterion = RubricCriterion.objects.create(
            competition=self.competition,
            name='Impact',
            description='Immediate visual impact',
            weight=1.0,
            score_out_of=10,
        )
        photo = self.create_photo('Shortlisted image', Photo.Status.SHORTLISTED)

        self.client.force_login(self.guest_judge)
        response = self.client.get(reverse('judge_photo', args=[self.competition.slug, photo.id]))

        self.assertContains(response, 'Out of 10')
        self.assertContains(response, 'max="10"')
        self.assertContains(response, '/ 10')

        self.client.post(
            reverse('judge_photo', args=[self.competition.slug, photo.id]),
            {
                f'criterion_{criterion.id}': '8',
                'comment': 'Strong image.',
            },
        )

        score = Score.objects.get(photo=photo, judge=self.guest_judge)
        self.assertEqual(score.criteria_scores[str(criterion.id)], 8.0)
        self.assertEqual(score.total_score, 80.0)

    def test_feedback_portal_judge_receives_pending_photos(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        CompetitionMembership.objects.create(
            competition=feedback_competition,
            user=self.guest_judge,
            role=CompetitionMembership.Role.VIP_JUDGE,
        )
        pending = Photo.objects.create(
            competition=feedback_competition,
            title='Member image',
            photographer_name='Club Member',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.PENDING,
        )

        self.client.force_login(self.guest_judge)
        response = self.client.get(reverse('judge_router', args=[feedback_competition.slug]))

        self.assertRedirects(
            response,
            reverse('judge_photo', args=[feedback_competition.slug, pending.id]),
            fetch_redirect_response=False,
        )

        direct_response = self.client.get(reverse('judge_photo', args=[feedback_competition.slug, pending.id]))
        self.assertEqual(direct_response.status_code, 200)
        self.assertContains(direct_response, 'Member feedback review')

    def test_single_internal_reviewer_vote_finalizes_photo_status(self):
        photo = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.internal_judge)
        response = self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )

        self.assertRedirects(response, reverse('elimination_mode', args=[self.competition.slug]))
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.ROUND_1)
        self.assertEqual(photo.status_votes.get(voter=self.internal_judge).decision, PhotoStatusVote.Decision.ROUND_1)

    def test_internal_review_majority_required_when_multiple_reviewers_exist(self):
        second_reviewer = User.objects.create_user(username='reviewer-two', password='test-pass')
        CompetitionMembership.objects.create(
            competition=self.competition,
            user=second_reviewer,
            role=CompetitionMembership.Role.INTERNAL_JUDGE,
        )
        photo = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.internal_judge)
        self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.PENDING)

        self.client.force_login(second_reviewer)
        self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.ROUND_1)

    def test_internal_reviewer_does_not_see_photos_they_already_voted_on(self):
        photo = self.create_photo('Pending image', Photo.Status.PENDING)
        PhotoStatusVote.objects.create(photo=photo, voter=self.internal_judge, decision=PhotoStatusVote.Decision.REJECT)

        self.client.force_login(self.internal_judge)
        response = self.client.get(reverse('elimination_mode', args=[self.competition.slug]))

        self.assertContains(response, 'No pending photos left for you.')

    def test_internal_round_1_review_displays_full_context_and_records_score(self):
        photo = self.create_photo(
            'Context image',
            Photo.Status.ROUND_1,
            category='Portrait',
            description='A full story for the photo.',
            camera_settings='50mm, f/2.8, ISO 400',
        )

        self.client.force_login(self.internal_judge)
        response = self.client.get(reverse('round_1_review', args=[self.competition.slug]))

        self.assertContains(response, 'Context image')
        self.assertContains(response, 'Portrait')
        self.assertContains(response, 'A full story for the photo.')
        self.assertContains(response, '50mm, f/2.8, ISO 400')

        response = self.client.post(
            reverse('round_1_review', args=[self.competition.slug]),
            {'photo_id': photo.id, 'score': '8'},
        )

        self.assertRedirects(response, reverse('round_1_review', args=[self.competition.slug]))
        self.assertEqual(photo.round_1_scores.get(judge=self.internal_judge).score, 8)

    def test_finalize_shortlist_uses_top_ten_percent_of_round_1_scores(self):
        photos = [
            self.create_photo(f'Round 1 image {index}', Photo.Status.ROUND_1)
            for index in range(10)
        ]
        for index, photo in enumerate(photos):
            RoundOneScore.objects.create(photo=photo, judge=self.internal_judge, score=index + 1)

        self.client.force_login(self.organizer)
        response = self.client.post(reverse('finalize_shortlist', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))
        statuses = {photo.id: Photo.objects.get(id=photo.id).status for photo in photos}
        self.assertEqual(statuses[photos[-1].id], Photo.Status.SHORTLISTED)
        self.assertEqual(
            sum(1 for status in statuses.values() if status == Photo.Status.SHORTLISTED),
            1,
        )

    def test_organizer_only_sees_assigned_competitions_on_home(self):
        other_competition = Competition.objects.create(name='Private Client Cup', slug='private-client-cup')

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('home_hub'))

        self.assertContains(response, self.competition.name)
        self.assertContains(response, 'Your events')
        self.assertContains(response, 'Your judging events, organized.')
        self.assertNotContains(response, other_competition.name)

        direct_response = self.client.get(reverse('upload_spreadsheet', args=[other_competition.slug]))
        self.assertRedirects(direct_response, reverse('home_hub'))

    def test_internal_reviewer_cannot_upload_or_finalize(self):
        self.client.force_login(self.internal_judge)

        upload_response = self.client.get(reverse('upload_spreadsheet', args=[self.competition.slug]))
        finalize_response = self.client.post(reverse('finalize_shortlist', args=[self.competition.slug]))

        self.assertRedirects(upload_response, reverse('home_hub'))
        self.assertRedirects(finalize_response, reverse('home_hub'))

    def test_completed_zip_import_points_organizer_to_workspace_next_steps(self):
        job = ZipImportJob.objects.create(
            competition=self.competition,
            uploaded_by=self.organizer,
            source_name='entries.zip',
            status=ZipImportJob.Status.COMPLETED,
            total_rows=3,
            processed_rows=3,
            matched_images=3,
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('zip_import_status', args=[self.competition.slug, job.id]))

        self.assertContains(response, 'Import completed successfully')
        self.assertContains(response, 'Back to Workspace')
        self.assertContains(response, 'Review Imported Entries')
        self.assertNotContains(response, 'Open Feedback Report')

    def test_completed_zip_import_allows_reviewer_to_start_triage(self):
        CompetitionMembership.objects.create(
            competition=self.competition,
            user=self.organizer,
            role=CompetitionMembership.Role.INTERNAL_JUDGE,
        )
        job = ZipImportJob.objects.create(
            competition=self.competition,
            uploaded_by=self.organizer,
            source_name='entries.zip',
            status=ZipImportJob.Status.COMPLETED,
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('zip_import_status', args=[self.competition.slug, job.id]))

        self.assertContains(response, 'Start Triage Review')

    def test_feedback_portal_hides_funnel_actions(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        CompetitionMembership.objects.create(
            competition=feedback_competition,
            user=self.guest_judge,
            role=CompetitionMembership.Role.VIP_JUDGE,
        )
        CompetitionMembership.objects.create(
            competition=feedback_competition,
            user=self.internal_judge,
            role=CompetitionMembership.Role.INTERNAL_JUDGE,
        )

        self.client.force_login(self.guest_judge)
        home_response = self.client.get(reverse('home_hub'))
        self.assertContains(home_response, 'Feedback portal')
        self.assertContains(home_response, 'Review photos')
        self.assertContains(home_response, 'My submitted scores')
        self.assertNotContains(home_response, 'Triage review')

        self.client.force_login(self.internal_judge)
        elimination_response = self.client.get(reverse('elimination_mode', args=[feedback_competition.slug]))
        self.assertRedirects(elimination_response, reverse('home_hub'))

        judge_response = self.client.get(reverse('judge_router', args=[feedback_competition.slug]))
        self.assertEqual(judge_response.status_code, 200)

    def test_feedback_portal_organizer_can_upload_data_without_finalize_action(self):
        shutter_organizer = User.objects.create_user(username='shutter-organizer', password='test-pass')
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        CompetitionMembership.objects.create(
            competition=feedback_competition,
            user=shutter_organizer,
            role=CompetitionMembership.Role.ORGANIZER,
        )

        self.client.force_login(shutter_organizer)
        response = self.client.get(reverse('home_hub'))

        self.assertContains(response, 'Upload data')
        self.assertNotContains(response, 'Finalize shortlist')

        upload_response = self.client.get(reverse('upload_spreadsheet', args=[feedback_competition.slug]))
        self.assertEqual(upload_response.status_code, 200)

    def test_photos_only_zip_creates_entries_from_sorted_filename_codes(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        image = Image.new('RGB', (20, 20), color='white')
        image_payload = io.BytesIO()
        image.save(image_payload, format='JPEG')
        image_bytes = image_payload.getvalue()

        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
            temp_path = temp_zip.name

        with zipfile.ZipFile(temp_path, 'w') as package:
            package.writestr('photos/SS010.jpg', image_bytes)
            package.writestr('photos/SS001.jpg', image_bytes)
            package.writestr('photos/SS002.jpg', image_bytes)

        job = ZipImportJob.objects.create(
            competition=feedback_competition,
            uploaded_by=self.organizer,
            source_name='photos-only.zip',
            temp_path=temp_path,
        )

        process_photos_only_zip_job(job.id)
        job.refresh_from_db()

        self.assertEqual(job.status, ZipImportJob.Status.COMPLETED)
        self.assertEqual(job.total_rows, 3)
        self.assertEqual(job.processed_rows, 3)
        self.assertEqual(job.matched_images, 3)
        self.assertEqual(
            list(Photo.objects.filter(competition=feedback_competition).order_by('entry_code').values_list('entry_code', flat=True)),
            ['SS001', 'SS002', 'SS010'],
        )
        self.assertEqual(
            list(Photo.objects.filter(competition=feedback_competition).order_by('entry_code').values_list('title', flat=True)),
            ['SS001', 'SS002', 'SS010'],
        )

        report_response = self.client.get(reverse('feedback_report', args=[feedback_competition.slug]))
        self.assertContains(report_response, 'Photo reference: SS001')

    def test_leaderboard_is_public(self):
        photo = self.create_photo('Public ranked image', Photo.Status.SHORTLISTED)
        Score.objects.create(photo=photo, judge=self.guest_judge, criteria_scores={}, total_score=87.5)

        response = self.client.get(reverse('leaderboard', args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Live leaderboard')
        self.assertContains(response, '87.5')

    def test_feedback_portal_report_is_public_without_admin_edit_controls(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        photo = Photo.objects.create(
            competition=feedback_competition,
            title='Member image',
            photographer_name='Club Member',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.PENDING,
            organizer_notes='Private organizer context',
        )
        Score.objects.create(
            photo=photo,
            judge=self.guest_judge,
            criteria_scores={},
            total_score=91,
            comment='Strong composition and clear intent.',
        )

        response = self.client.get(reverse('feedback_report', args=[feedback_competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Strong composition and clear intent.')
        self.assertContains(response, 'Private organizer context')
        self.assertNotContains(response, '/admin/judging_app/photo/')
        self.assertNotContains(response, '>Edit<')

    def test_full_competition_feedback_report_is_not_public(self):
        response = self.client.get(reverse('feedback_report', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))


class AuthNavigationTests(TestCase):
    def test_logged_in_standard_user_can_log_out_from_topbar(self):
        user = User.objects.create_user(username='standard', password='test-pass')

        self.client.force_login(user)
        home_response = self.client.get(reverse('home_hub'))

        self.assertContains(home_response, 'Log Out')

        logout_response = self.client.post(reverse('logout'))

        self.assertRedirects(logout_response, reverse('home_hub'))
        self.assertNotIn('_auth_user_id', self.client.session)


class UserTimezoneMiddlewareTests(TestCase):
    def test_timezone_cookie_activates_user_local_timezone(self):
        request = RequestFactory().get('/')
        request.COOKIES['simplyjudge_timezone'] = 'America/New_York'
        middleware = UserTimezoneMiddleware(lambda current_request: current_request)

        try:
            middleware(request)

            self.assertEqual(timezone.get_current_timezone_name(), 'America/New_York')
        finally:
            timezone.deactivate()


class CsvEncodingTests(TestCase):
    def test_decode_csv_bytes_accepts_windows_1252_smart_quotes(self):
        csv_bytes = b'Criterion Name,Description,Weight\r\nComposition,\x93Strong frame\x94,1.0\r\n'

        decoded = decode_csv_bytes(csv_bytes)

        self.assertIn('\u201cStrong frame\u201d', decoded)


class PhotoUploadPathTests(TestCase):
    def test_competition_photo_upload_path_uses_competition_name_folder(self):
        competition = Competition(name='Youth POTY 2026', slug='youth-poty')
        photo = Photo(competition=competition)

        self.assertEqual(
            competition_photo_upload_path(photo, 'Rising Tide.jpg'),
            'competition_photos/youth-poty-2026/Rising Tide.jpg',
        )


class ZipImageMatchingTests(TestCase):
    def test_normalize_match_key_handles_double_image_extensions(self):
        self.assertEqual(
            normalize_match_key('rsa_rubensteyn_thegreatescape.jpeg..jpg'),
            'rsarubensteynthegreatescape',
        )

    def test_find_matching_image_allows_prefixed_title_filename(self):
        image = SimpleNamespace(filename='ZA_CalvinSeverin_RisingTide.jpeg')
        images = {normalize_match_key(image.filename): image}

        self.assertIs(find_matching_image(images, ['Rising Tide']), image)

    def test_find_matching_image_uses_photographer_for_duplicate_titles(self):
        calvin = SimpleNamespace(filename='ZA_Calvin_TheHunt.jpeg')
        yehuda = SimpleNamespace(filename='ZA_YehudaRabin_TheHunt.jpeg')
        images = {
            normalize_match_key(calvin.filename): calvin,
            normalize_match_key(yehuda.filename): yehuda,
        }

        self.assertIs(find_matching_image(images, ['The Hunt'], photographer='Yehuda Rabin'), yehuda)


class CloudinaryCompressionTests(TestCase):
    def test_prepare_image_for_cloudinary_leaves_small_images_unchanged(self):
        payload = b'small-image-bytes'

        result = prepare_image_for_cloudinary(payload, 'small.jpg', max_bytes=1024)

        self.assertEqual(result['bytes'], payload)
        self.assertFalse(result['compressed'])
        self.assertEqual(result['filename'], 'small.jpg')

    def test_prepare_image_for_cloudinary_compresses_large_images_under_limit(self):
        image = Image.effect_noise((1200, 1200), 100).convert('RGB')
        source = io.BytesIO()
        image.save(source, format='JPEG', quality=95)
        payload = source.getvalue()

        result = prepare_image_for_cloudinary(payload, 'large-original.png', max_bytes=90_000)

        self.assertTrue(result['compressed'])
        self.assertLessEqual(len(result['bytes']), 90_000)
        self.assertEqual(result['filename'], 'large-original.jpg')


class CompetitionRuleReviewTests(TestCase):
    def no_exif_image_bytes(self):
        image = Image.new('RGB', (20, 20), color='white')
        output = io.BytesIO()
        image.save(output, format='JPEG')
        return output.getvalue()

    def test_youth_poty_runs_exif_rule_review(self):
        competition = Competition.objects.create(name='Youth POTY', slug='youth-poty')

        flags = collect_photo_rule_flags(competition, self.no_exif_image_bytes())

        self.assertIn('No EXIF data found', ' '.join(flags))

    def test_non_youth_poty_skips_exif_rule_review(self):
        competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )

        flags = collect_photo_rule_flags(competition, self.no_exif_image_bytes())

        self.assertEqual(flags, [])
