import csv
import io
import json
import tempfile
import zipfile
from decimal import Decimal
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .admin import CompetitionAdmin
from .models import Competition, CompetitionMembership, EntryOrder, Photo, PhotoStatusVote, RoundOneScore, RubricCriterion, Score, ZipImportJob, competition_photo_upload_path
from .middleware import UserTimezoneMiddleware
from .utils import calculate_judge_calibration, compare_exif_data, send_automated_email
from .views import collect_photo_rule_flags, decode_csv_bytes, find_matching_image, normalize_match_key, prepare_image_for_cloudinary, process_photos_only_zip_job, score_report_thumbnail_url


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

    def test_create_checkout_session_creates_unpaid_entry_order(self):
        self.competition.entry_fee = Decimal('25.50')
        self.competition.save(update_fields=['entry_fee'])
        self.client.force_login(self.guest_judge)
        checkout_create = Mock(
            return_value=SimpleNamespace(
                id='cs_test_123',
                url='https://checkout.stripe.com/c/pay/cs_test_123',
            )
        )
        fake_stripe = SimpleNamespace(
            api_key='',
            checkout=SimpleNamespace(
                Session=SimpleNamespace(create=checkout_create),
            ),
        )

        with self.settings(STRIPE_SECRET_KEY='sk_test_123', STRIPE_CURRENCY='zar'):
            with patch('judging_app.views.stripe', fake_stripe):
                response = self.client.post(reverse('create_checkout_session', args=[self.competition.slug]))

        self.assertRedirects(
            response,
            'https://checkout.stripe.com/c/pay/cs_test_123',
            fetch_redirect_response=False,
        )
        order = EntryOrder.objects.get()
        self.assertEqual(order.user, self.guest_judge)
        self.assertEqual(order.competition, self.competition)
        self.assertEqual(order.stripe_checkout_id, 'cs_test_123')
        self.assertEqual(order.amount_paid, Decimal('25.50'))
        self.assertFalse(order.is_paid)
        self.assertEqual(fake_stripe.api_key, 'sk_test_123')

        checkout_kwargs = checkout_create.call_args.kwargs
        self.assertEqual(checkout_kwargs['mode'], 'payment')
        self.assertEqual(checkout_kwargs['line_items'][0]['price_data']['currency'], 'zar')
        self.assertEqual(checkout_kwargs['line_items'][0]['price_data']['unit_amount'], 2550)
        self.assertEqual(checkout_kwargs['metadata']['competition_slug'], self.competition.slug)
        self.assertEqual(checkout_kwargs['metadata']['user_id'], str(self.guest_judge.id))

    def test_stripe_webhook_marks_order_paid_and_grants_entrant_access(self):
        entrant = User.objects.create_user(username='entrant', password='test-pass')
        order = EntryOrder.objects.create(
            user=entrant,
            competition=self.competition,
            stripe_checkout_id='cs_test_paid',
            amount_paid=Decimal('25.50'),
            is_paid=False,
        )
        payload = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_paid',
                },
            },
        }

        with self.settings(STRIPE_WEBHOOK_SECRET=''):
            response = self.client.post(
                reverse('stripe_webhook'),
                data=json.dumps(payload),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertTrue(
            CompetitionMembership.objects.filter(
                competition=self.competition,
                user=entrant,
                role=CompetitionMembership.Role.ENTRANT,
                is_active=True,
            ).exists()
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

    def test_judge_photo_arrow_navigation_hooks_are_photo_links(self):
        first_photo = self.create_photo('First shortlisted image', Photo.Status.SHORTLISTED)
        current_photo = self.create_photo('Current shortlisted image', Photo.Status.SHORTLISTED)
        next_photo = self.create_photo('Next shortlisted image', Photo.Status.SHORTLISTED)
        RubricCriterion.objects.create(competition=self.competition, name='Impact', score_out_of=10)

        self.client.force_login(self.guest_judge)
        response = self.client.get(reverse('judge_photo', args=[self.competition.slug, current_photo.id]))

        self.assertContains(
            response,
            f'id="btn-previous" class="button button-secondary" href="{reverse("judge_photo", args=[self.competition.slug, first_photo.id])}"',
        )
        self.assertContains(
            response,
            f'id="btn-next" class="button button-secondary" href="{reverse("judge_photo", args=[self.competition.slug, next_photo.id])}"',
        )
        self.assertNotContains(response, 'id="btn-next" type="submit"')

    def test_autosave_judge_score_persists_scores_and_comment(self):
        criterion = RubricCriterion.objects.create(
            competition=self.competition,
            name='Composition',
            score_out_of=15,
            weight=1.0,
        )
        photo = self.create_photo('Shortlisted image', Photo.Status.SHORTLISTED)

        self.client.force_login(self.guest_judge)
        response = self.client.post(
            reverse('autosave_judge_score', args=[self.competition.slug, photo.id]),
            {
                f'criterion_{criterion.id}': '12.5',
                'comment': 'Strong atmosphere, saved while typing.',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        score = Score.objects.get(photo=photo, judge=self.guest_judge)
        self.assertEqual(score.criteria_scores[str(criterion.id)], 12.5)
        self.assertEqual(score.total_score, 12.5)
        self.assertEqual(score.comment, 'Strong atmosphere, saved while typing.')

    def test_organizer_can_export_competition_results_csv(self):
        photo = self.create_photo(
            'Storm, Over Valley',
            Photo.Status.SHORTLISTED,
            photographer_name='Amina Jacobs',
            photographer_email='amina@example.com',
            category='Landscape',
        )
        Score.objects.create(photo=photo, judge=self.guest_judge, criteria_scores={}, total_score=80)
        Score.objects.create(
            photo=photo,
            judge=self.internal_judge,
            criteria_scores={},
            total_score=90,
            comment='Excellent control of light.',
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('export_competition_results_csv', args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="competition_results.csv"')
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8'))))
        self.assertEqual(
            rows[0],
            [
                'Entrant First Name',
                'Entrant Last Name',
                'Country',
                'Email',
                'Category/Section',
                'Image Title',
                'Total Score',
                'Final Status',
                'judge Score',
                'judge Feedback',
                'reviewer Score',
                'reviewer Feedback',
            ],
        )
        self.assertEqual(
            rows[1],
            [
                'Amina',
                'Jacobs',
                '',
                'amina@example.com',
                'Landscape',
                'Storm, Over Valley',
                '85.00',
                'Shortlisted',
                '80.00',
                '',
                '90.00',
                'Excellent control of light.',
            ],
        )

    def test_non_organizer_cannot_export_competition_results_csv(self):
        self.client.force_login(self.guest_judge)

        response = self.client.get(reverse('export_competition_results_csv', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))

    def test_organizer_can_view_score_summary_pdf_page(self):
        RubricCriterion.objects.create(competition=self.competition, name='Overall', score_out_of=15)
        photo = self.create_photo(
            'Storm Over Valley',
            Photo.Status.SHORTLISTED,
            photographer_name='Amina Jacobs',
            photographer_email='amina@example.com',
            category='Landscape',
            entry_code='SS001',
        )
        Score.objects.create(
            photo=photo,
            judge=self.guest_judge,
            criteria_scores={},
            total_score=11,
            comment='Strong atmosphere.',
        )
        Score.objects.create(
            photo=photo,
            judge=self.internal_judge,
            criteria_scores={},
            total_score=14,
            comment='Excellent control of light.',
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('competition_score_summary_pdf', args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Storm Over Valley')
        self.assertContains(response, 'summary-thumb')
        self.assertContains(response, 'SJ #')
        self.assertContains(response, 'Amina Jacobs')
        self.assertContains(response, 'Shortlisted')
        self.assertContains(response, 'Save as PDF')
        self.assertContains(response, 'Maximum score 15')
        self.assertContains(response, 'judge')
        self.assertContains(response, 'reviewer')
        self.assertContains(response, '11.00')
        self.assertContains(response, 'Excellent control of light.')

    def test_non_organizer_cannot_view_score_summary_pdf_page(self):
        self.client.force_login(self.guest_judge)

        response = self.client.get(reverse('competition_score_summary_pdf', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))

    def test_organizer_can_view_anonymized_shareable_score_summary_pdf_page(self):
        RubricCriterion.objects.create(competition=self.competition, name='Overall', score_out_of=15)
        self.guest_judge.username = 'private_judge_alpha'
        self.guest_judge.save(update_fields=['username'])
        self.internal_judge.username = 'private_judge_beta'
        self.internal_judge.save(update_fields=['username'])
        photo = self.create_photo(
            'Storm Over Valley',
            Photo.Status.SHORTLISTED,
            photographer_name='Amina Jacobs',
            photographer_email='amina@example.com',
            category='Landscape',
            entry_code='SS001',
        )
        Score.objects.create(
            photo=photo,
            judge=self.guest_judge,
            criteria_scores={},
            total_score=11,
            comment='Strong atmosphere.',
        )
        Score.objects.create(
            photo=photo,
            judge=self.internal_judge,
            criteria_scores={},
            total_score=14,
            comment='Excellent control of light.',
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('shareable_score_summary_pdf', args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Shareable group report')
        self.assertContains(response, 'Judge names hidden')
        self.assertContains(response, 'Maximum score 15')
        self.assertContains(response, 'Judge 1')
        self.assertContains(response, 'Judge 2')
        self.assertContains(response, 'Storm Over Valley')
        self.assertContains(response, 'Excellent control of light.')
        self.assertNotContains(response, 'private_judge_alpha')
        self.assertNotContains(response, 'private_judge_beta')
        self.assertNotContains(response, '<th>Entrant</th>', html=True)
        self.assertNotContains(response, '<th>Status</th>', html=True)
        self.assertNotContains(response, 'Amina Jacobs')
        self.assertNotContains(response, 'amina@example.com')
        self.assertNotContains(response, 'Shortlisted')

    def test_score_report_thumbnail_url_uses_small_cloudinary_transformation(self):
        photo = SimpleNamespace(
            image=SimpleNamespace(
                url='https://res.cloudinary.com/demo/image/upload/v123/youth-poty/full-size-image.jpg'
            )
        )

        thumbnail_url = score_report_thumbnail_url(photo)

        self.assertEqual(
            thumbnail_url,
            'https://res.cloudinary.com/demo/image/upload/c_fill,w_128,h_128,q_auto:eco,f_auto/v123/youth-poty/full-size-image.jpg',
        )

    def test_non_organizer_cannot_view_shareable_score_summary_pdf_page(self):
        self.client.force_login(self.guest_judge)

        response = self.client.get(reverse('shareable_score_summary_pdf', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))

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

    def test_rubric_score_out_of_controls_judge_scale_and_raw_total(self):
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
        self.assertEqual(score.total_score, 8.0)

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

    def test_internal_triage_skips_entries_without_matched_images(self):
        missing = self.create_photo(
            'Missing image row',
            Photo.Status.PENDING,
            rule_flags='No matching image file found in uploaded ZIP package.',
        )
        ready = self.create_photo('Ready image row', Photo.Status.PENDING)

        self.client.force_login(self.internal_judge)
        response = self.client.get(reverse('elimination_mode', args=[self.competition.slug]))

        self.assertContains(response, f'SimplyJudge ID: #{ready.id}')
        self.assertContains(response, '1 for you')
        self.assertContains(response, '1 pending')
        self.assertContains(response, '1 missing images')
        self.assertNotContains(response, f'SimplyJudge ID: #{missing.id}')

    def test_completed_zip_status_warns_when_rows_have_no_matching_images(self):
        job = ZipImportJob.objects.create(
            competition=self.competition,
            uploaded_by=self.organizer,
            source_name='late-entries.zip',
            status=ZipImportJob.Status.COMPLETED,
            total_rows=137,
            processed_rows=137,
            matched_images=15,
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('zip_import_status', args=[self.competition.slug, job.id]))

        self.assertContains(response, '122 entries did not match an image file')
        self.assertContains(response, 'not shown in triage')

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
        private_judge = User.objects.create_user(username='private_reviewer_name')
        photo = self.create_photo('Public ranked image', Photo.Status.SHORTLISTED)
        Score.objects.create(photo=photo, judge=private_judge, criteria_scores={}, total_score=87.5)

        response = self.client.get(reverse('leaderboard', args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Live leaderboard')
        self.assertContains(response, '87.5')
        self.assertNotContains(response, private_judge.username)

    def test_reports_show_average_score_out_of_rubric_total_and_percentage(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society-rubric-total',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        criterion_one = RubricCriterion.objects.create(
            competition=feedback_competition,
            name='Impact',
            score_out_of=5,
            weight=1,
        )
        criterion_two = RubricCriterion.objects.create(
            competition=feedback_competition,
            name='Craft',
            score_out_of=10,
            weight=1,
        )
        judge_one = User.objects.create_user(username='private_first_reviewer')
        judge_two = User.objects.create_user(username='private_second_reviewer')
        photo = Photo.objects.create(
            competition=feedback_competition,
            title='Member image',
            photographer_name='Club Member',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.PENDING,
        )
        Score.objects.create(
            photo=photo,
            judge=judge_one,
            criteria_scores={str(criterion_one.id): 4, str(criterion_two.id): 8},
            total_score=80,
            comment='Good structure.',
        )
        Score.objects.create(
            photo=photo,
            judge=judge_two,
            criteria_scores={str(criterion_one.id): 5, str(criterion_two.id): 10},
            total_score=15,
            comment='Excellent.',
        )

        leaderboard_response = self.client.get(reverse('leaderboard', args=[feedback_competition.slug]))
        report_response = self.client.get(reverse('feedback_report', args=[feedback_competition.slug]))

        self.assertContains(leaderboard_response, '13.5 / 15')
        self.assertContains(leaderboard_response, '90.0%')
        self.assertContains(report_response, 'Average 13.5 / 15 (90.0%)')
        self.assertContains(report_response, 'Score 12.0 / 15 (80.0%)')
        self.assertContains(report_response, 'Score 15.0 / 15 (100.0%)')
        self.assertNotContains(leaderboard_response, judge_one.username)
        self.assertNotContains(report_response, judge_one.username)
        self.assertNotContains(leaderboard_response, judge_two.username)
        self.assertNotContains(report_response, judge_two.username)

    def test_feedback_portal_report_is_public_without_admin_edit_controls(self):
        private_judge = User.objects.create_user(username='private_feedback_reviewer')
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
            judge=private_judge,
            criteria_scores={},
            total_score=91,
            comment='Strong composition and clear intent.',
        )

        response = self.client.get(reverse('feedback_report', args=[feedback_competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Strong composition and clear intent.')
        self.assertContains(response, 'Judge feedback')
        self.assertContains(response, 'Private organizer context')
        self.assertNotContains(response, private_judge.username)
        self.assertNotContains(response, '/admin/judging_app/photo/')
        self.assertNotContains(response, '>Edit<')

    def test_organizer_report_shows_judge_names_on_screen_but_hides_them_from_print(self):
        feedback_competition = Competition.objects.create(
            name='Shutter Society',
            slug='shutter-society-internal',
            workflow=Competition.Workflow.FEEDBACK_PORTAL,
        )
        CompetitionMembership.objects.create(
            competition=feedback_competition,
            user=self.organizer,
            role=CompetitionMembership.Role.ORGANIZER,
        )
        photo = Photo.objects.create(
            competition=feedback_competition,
            title='Member image',
            photographer_name='Club Member',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.PENDING,
        )
        Score.objects.create(
            photo=photo,
            judge=self.guest_judge,
            criteria_scores={},
            total_score=91,
            comment='Strong composition and clear intent.',
        )

        self.client.force_login(self.organizer)
        response = self.client.get(reverse('feedback_report', args=[feedback_competition.slug]))

        self.assertContains(response, self.guest_judge.username)
        self.assertContains(response, 'judge-identity-internal no-print')
        self.assertContains(response, 'Judge feedback')

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


class JudgeInviteTests(TestCase):
    def test_logged_in_user_accepts_judge_invite(self):
        competition = Competition.objects.create(name='Invite Event', slug='invite-event')
        user = User.objects.create_user(username='invited-judge', password='test-pass')

        self.client.force_login(user)
        response = self.client.get(reverse('accept_judge_invite', args=[competition.judge_invite_token]))

        self.assertRedirects(response, reverse('judge_router', args=[competition.slug]))
        self.assertTrue(competition.judges.filter(id=user.id).exists())
        self.assertTrue(
            CompetitionMembership.objects.filter(
                competition=competition,
                user=user,
                role=CompetitionMembership.Role.VIP_JUDGE,
                is_active=True,
            ).exists()
        )

    def test_anonymous_invite_redirects_to_login_and_preserves_token(self):
        competition = Competition.objects.create(name='Invite Event', slug='invite-event')

        response = self.client.get(reverse('accept_judge_invite', args=[competition.judge_invite_token]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])
        self.assertEqual(
            self.client.session['pending_judge_invite_token'],
            str(competition.judge_invite_token),
        )

    def test_invited_user_is_added_after_signup(self):
        competition = Competition.objects.create(name='Signup Invite Event', slug='signup-invite-event')
        session = self.client.session
        session['pending_judge_invite_token'] = str(competition.judge_invite_token)
        session.save()

        response = self.client.post(
            reverse('register'),
            {
                'username': 'new-invited-judge',
                'password1': 'a-secure-test-pass-123',
                'password2': 'a-secure-test-pass-123',
            },
        )

        user = User.objects.get(username='new-invited-judge')
        self.assertRedirects(response, reverse('judge_router', args=[competition.slug]))
        self.assertTrue(competition.judges.filter(id=user.id).exists())
        self.assertTrue(
            CompetitionMembership.objects.filter(
                competition=competition,
                user=user,
                role=CompetitionMembership.Role.VIP_JUDGE,
                is_active=True,
            ).exists()
        )


class AutomatedEmailTests(TestCase):
    def test_send_automated_email_returns_false_when_disabled(self):
        competition = Competition.objects.create(name='Quiet Event', slug='quiet-event')

        with patch('judging_app.utils.render_to_string') as render_mock:
            with patch('judging_app.utils.send_mail') as send_mock:
                result = send_automated_email(
                    competition=competition,
                    subject='Results ready',
                    template_name='emails/results.txt',
                    context={'name': 'Entrant'},
                    recipient_list=['entrant@example.com'],
                )

        self.assertFalse(result)
        render_mock.assert_not_called()
        send_mock.assert_not_called()

    def test_send_automated_email_sends_when_enabled(self):
        competition = Competition.objects.create(
            name='Published Event',
            slug='published-event',
            emails_enabled=True,
        )

        with patch('judging_app.utils.render_to_string', return_value='Hello from SimplyJudge') as render_mock:
            with patch('judging_app.utils.send_mail', return_value=1) as send_mock:
                result = send_automated_email(
                    competition=competition,
                    subject='Results ready',
                    template_name='emails/results.txt',
                    context={'name': 'Entrant'},
                    recipient_list=['entrant@example.com'],
                    from_email='team@simplyjudge.com',
                )

        self.assertEqual(result, 1)
        render_mock.assert_called_once_with(
            'emails/results.txt',
            {'name': 'Entrant', 'competition': competition},
        )
        send_mock.assert_called_once_with(
            'Results ready',
            'Hello from SimplyJudge',
            'team@simplyjudge.com',
            ['entrant@example.com'],
            fail_silently=False,
            html_message=None,
        )


class JudgeCalibrationTests(TestCase):
    def test_calculate_judge_calibration_flags_harsh_and_lenient_outliers(self):
        competition = Competition.objects.create(name='Calibration Event', slug='calibration-event')
        photos = [
            Photo.objects.create(
                competition=competition,
                title=f'Entry {index}',
                photographer_name='Entrant',
                category='Open',
                image='competition_photos/placeholder.jpg',
            )
            for index in range(3)
        ]
        fair_judges = [
            User.objects.create_user(username=f'fair_judge_{index}')
            for index in range(9)
        ]
        fair_judge = User.objects.create_user(username='fair_judge', first_name='Fair', last_name='Judge')
        fair_judges.append(fair_judge)
        harsh_judge = User.objects.create_user(username='harsh_judge')
        lenient_judge = User.objects.create_user(username='lenient_judge')

        for photo in photos:
            for judge in fair_judges:
                Score.objects.create(photo=photo, judge=judge, criteria_scores={}, total_score=50)
            Score.objects.create(photo=photo, judge=harsh_judge, criteria_scores={}, total_score=0)
            Score.objects.create(photo=photo, judge=lenient_judge, criteria_scores={}, total_score=100)

        result = calculate_judge_calibration(competition.id)

        self.assertAlmostEqual(result['overall_average'], 50)
        flagged = {judge['judge_username']: judge for judge in result['flagged_judges']}
        self.assertEqual(set(flagged), {'harsh_judge', 'lenient_judge'})
        self.assertEqual(flagged['harsh_judge']['direction'], 'harsh')
        self.assertEqual(flagged['lenient_judge']['direction'], 'lenient')
        self.assertEqual(flagged['harsh_judge']['score_count'], 3)
        fair_result = next(judge for judge in result['judges'] if judge['judge_username'] == 'fair_judge')
        self.assertFalse(fair_result['is_flagged'])
        self.assertEqual(fair_result['judge_name'], 'Fair Judge')

    def test_calculate_judge_calibration_handles_competition_without_scores(self):
        competition = Competition.objects.create(name='Empty Event', slug='empty-event')

        result = calculate_judge_calibration(competition.id)

        self.assertIsNone(result['overall_average'])
        self.assertIsNone(result['standard_deviation'])
        self.assertEqual(result['judges'], [])
        self.assertEqual(result['flagged_judges'], [])


class EntryOrderSignalTests(TestCase):
    def test_payment_receipt_sends_only_when_order_first_becomes_paid(self):
        competition = Competition.objects.create(name='Paid Event', slug='paid-event')
        entrant = User.objects.create_user(
            username='paid-entrant',
            email='entrant@example.com',
            password='test-pass',
        )

        with patch('judging_app.signals.send_automated_email') as email_mock:
            order = EntryOrder.objects.create(
                user=entrant,
                competition=competition,
                stripe_checkout_id='cs_signal_123',
                amount_paid=Decimal('25.50'),
                is_paid=False,
            )
            email_mock.assert_not_called()

            order.is_paid = True
            order.save(update_fields=['is_paid'])
            email_mock.assert_called_once_with(
                competition=competition,
                subject='Payment receipt for Paid Event',
                template_name='emails/payment_receipt.txt',
                context={'order': order, 'user': entrant},
                recipient_list=['entrant@example.com'],
            )

            order.amount_paid = Decimal('30.00')
            order.save(update_fields=['amount_paid'])

        email_mock.assert_called_once()
        self.assertNotIn('_auth_user_id', self.client.session)


class PublishCompetitionResultsAdminActionTests(TestCase):
    def test_publish_competition_results_emails_shortlisted_photographers(self):
        competition = Competition.objects.create(
            name='World Class Photo Awards',
            slug='world-class-photo-awards',
            emails_enabled=True,
        )
        shortlisted = Photo.objects.create(
            competition=competition,
            title='Finalist Image',
            photographer_name='Finalist One',
            photographer_email='finalist@example.com',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.SHORTLISTED,
        )
        Photo.objects.create(
            competition=competition,
            title='Shortlisted Without Email',
            photographer_name='Finalist Two',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.SHORTLISTED,
        )
        Photo.objects.create(
            competition=competition,
            title='Rejected Image',
            photographer_name='Rejected Entrant',
            photographer_email='rejected@example.com',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=Photo.Status.REJECTED,
        )
        request = RequestFactory().post('/admin/judging_app/competition/')
        request.user = User.objects.create_superuser(
            username='platform-admin',
            email='admin@example.com',
            password='test-pass',
        )
        model_admin = CompetitionAdmin(Competition, django_admin.site)

        with patch.object(model_admin, 'message_user') as message_mock:
            with patch('judging_app.admin.send_automated_email', return_value=1) as email_mock:
                model_admin.publish_competition_results(
                    request,
                    Competition.objects.filter(id=competition.id),
                )

        competition.refresh_from_db()
        self.assertTrue(competition.results_published)
        email_mock.assert_called_once_with(
            competition=competition,
            subject='Congratulations from World Class Photo Awards',
            template_name='emails/congratulations.txt',
            context={'photo': shortlisted},
            recipient_list=['finalist@example.com'],
        )
        message = message_mock.call_args.args[1]
        self.assertIn('Shortlisted photos: 2', message)
        self.assertIn('Emails sent: 1', message)
        self.assertIn('Skipped without photographer email: 1', message)


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

    def test_raw_verification_fields_are_optional_by_default(self):
        competition = Competition.objects.create(name='RAW Check Event', slug='raw-check-event')
        photo = Photo.objects.create(
            competition=competition,
            title='Finalist image',
            photographer_name='Finalist',
            category='Open',
            image='competition_photos/placeholder.jpg',
        )

        self.assertFalse(photo.raw_file)
        self.assertFalse(photo.is_raw_verified)
        self.assertIsNone(photo.exif_warning_flag)


class RawFileUploadViewTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name='RAW Verification Event',
            slug='raw-verification-event',
            results_published=True,
        )
        self.photographer = User.objects.create_user(
            username='finalist',
            email='finalist@example.com',
            password='test-pass',
        )
        self.other_user = User.objects.create_user(
            username='other-finalist',
            email='other@example.com',
            password='test-pass',
        )

    def create_photo(self, status=Photo.Status.SHORTLISTED):
        return Photo.objects.create(
            competition=self.competition,
            title='Finalist image',
            photographer_name='Finalist',
            photographer_email='finalist@example.com',
            category='Open',
            image='competition_photos/placeholder.jpg',
            status=status,
        )

    def test_matching_photographer_can_upload_raw_file_for_shortlisted_photo(self):
        photo = self.create_photo()
        self.client.force_login(self.photographer)
        raw_file = SimpleUploadedFile(
            'finalist.CR2',
            b'raw file bytes',
            content_type='application/octet-stream',
        )

        with tempfile.TemporaryDirectory() as media_root:
            with self.settings(MEDIA_ROOT=media_root):
                response = self.client.post(
                    reverse('upload_raw_file', args=[self.competition.slug, photo.id]),
                    {'raw_file': raw_file},
                )

                self.assertRedirects(
                    response,
                    reverse('upload_raw_file', args=[self.competition.slug, photo.id]),
                )
                photo.refresh_from_db()
                self.assertTrue(photo.raw_file)
                self.assertIn('competition_raw_files/raw-verification-event', photo.raw_file.name)
                self.assertFalse(photo.is_raw_verified)
                self.assertEqual(photo.exif_warning_flag, '')

    def test_non_owner_cannot_upload_raw_file(self):
        photo = self.create_photo()
        self.client.force_login(self.other_user)

        response = self.client.post(
            reverse('upload_raw_file', args=[self.competition.slug, photo.id]),
            {'raw_file': SimpleUploadedFile('finalist.CR2', b'raw')},
        )

        self.assertEqual(response.status_code, 403)
        photo.refresh_from_db()
        self.assertFalse(photo.raw_file)

    def test_owner_cannot_upload_raw_file_before_shortlisted(self):
        photo = self.create_photo(status=Photo.Status.PENDING)
        self.client.force_login(self.photographer)

        response = self.client.post(
            reverse('upload_raw_file', args=[self.competition.slug, photo.id]),
            {'raw_file': SimpleUploadedFile('finalist.CR2', b'raw')},
        )

        self.assertEqual(response.status_code, 403)
        photo.refresh_from_db()
        self.assertFalse(photo.raw_file)

    def test_owner_cannot_upload_raw_file_before_results_are_published(self):
        self.competition.results_published = False
        self.competition.save(update_fields=['results_published'])
        photo = self.create_photo(status=Photo.Status.SHORTLISTED)
        self.client.force_login(self.photographer)

        response = self.client.post(
            reverse('upload_raw_file', args=[self.competition.slug, photo.id]),
            {'raw_file': SimpleUploadedFile('finalist.CR2', b'raw')},
        )

        self.assertEqual(response.status_code, 403)
        photo.refresh_from_db()
        self.assertFalse(photo.raw_file)


class RawExifComparisonTests(TestCase):
    def create_photo(self):
        competition = Competition.objects.create(name='RAW EXIF Event', slug='raw-exif-event')
        photo = Photo.objects.create(
            competition=competition,
            title='Finalist image',
            photographer_name='Finalist',
            category='Open',
            image='competition_photos/placeholder.jpg',
            raw_file='competition_raw_files/raw-exif-event/finalist.CR2',
        )
        return photo

    def test_compare_exif_data_verifies_matching_metadata(self):
        photo = self.create_photo()
        metadata = {
            'camera_model': 'Canon EOS R5',
            'original_datetime': '2026:05:01 18:30:00',
            'focal_length': '85/1',
            'exposure_time': '1/500',
        }

        with patch('judging_app.utils.read_file_field_bytes', side_effect=[b'jpeg', b'raw']):
            with patch('judging_app.utils.extract_exif_metadata', side_effect=[(metadata, ''), (metadata, '')]):
                result = compare_exif_data(photo)

        photo.refresh_from_db()
        self.assertTrue(result)
        self.assertTrue(photo.is_raw_verified)
        self.assertEqual(photo.exif_warning_flag, '')

    def test_compare_exif_data_logs_metadata_mismatch(self):
        photo = self.create_photo()
        original_metadata = {
            'camera_model': 'Canon EOS R5',
            'original_datetime': '2026:05:01 18:30:00',
            'focal_length': '85/1',
            'exposure_time': '1/500',
        }
        raw_metadata = {
            'camera_model': 'Canon EOS R5',
            'original_datetime': '2026:05:01 18:30:00',
            'focal_length': '50/1',
            'exposure_time': '1/500',
        }

        with patch('judging_app.utils.read_file_field_bytes', side_effect=[b'jpeg', b'raw']):
            with patch('judging_app.utils.extract_exif_metadata', side_effect=[(original_metadata, ''), (raw_metadata, '')]):
                result = compare_exif_data(photo)

        photo.refresh_from_db()
        self.assertFalse(result)
        self.assertFalse(photo.is_raw_verified)
        self.assertIn('focal length mismatch', photo.exif_warning_flag)

    def test_compare_exif_data_logs_missing_raw_file(self):
        photo = self.create_photo()
        photo.raw_file = ''
        photo.save(update_fields=['raw_file'])

        with patch('judging_app.utils.read_file_field_bytes', return_value=b'jpeg'):
            with patch('judging_app.utils.extract_exif_metadata', return_value=(
                {
                    'camera_model': 'Canon EOS R5',
                    'original_datetime': '2026:05:01 18:30:00',
                    'focal_length': '85/1',
                    'exposure_time': '1/500',
                },
                '',
            )):
                result = compare_exif_data(photo)

        photo.refresh_from_db()
        self.assertFalse(result)
        self.assertFalse(photo.is_raw_verified)
        self.assertIn('RAW file is missing', photo.exif_warning_flag)


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
