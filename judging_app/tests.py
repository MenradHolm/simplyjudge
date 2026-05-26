from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Competition, Photo, PhotoStatusVote, RoundOneScore
from .views import decode_csv_bytes


class PhotoStatusWorkflowTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(name='Youth POTY', slug='youth-poty')
        self.guest_judge = User.objects.create_user(username='judge', password='test-pass')
        self.staff = User.objects.create_user(username='staff', password='test-pass', is_staff=True)
        self.competition.judges.add(self.guest_judge)

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

    def test_single_staff_vote_finalizes_photo_status(self):
        photo = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )

        self.assertRedirects(response, reverse('elimination_mode', args=[self.competition.slug]))
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.ROUND_1)
        self.assertEqual(photo.status_votes.get(voter=self.staff).decision, PhotoStatusVote.Decision.ROUND_1)

    def test_staff_majority_required_when_multiple_staff_users_exist(self):
        second_staff = User.objects.create_user(username='staff-two', password='test-pass', is_staff=True)
        User.objects.create_user(username='staff-three', password='test-pass', is_staff=True)
        photo = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.staff)
        self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.PENDING)

        self.client.force_login(second_staff)
        self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'round_1'},
        )
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.ROUND_1)

    def test_staff_does_not_see_photos_they_already_voted_on(self):
        photo = self.create_photo('Pending image', Photo.Status.PENDING)
        PhotoStatusVote.objects.create(photo=photo, voter=self.staff, decision=PhotoStatusVote.Decision.REJECT)

        self.client.force_login(self.staff)
        response = self.client.get(reverse('elimination_mode', args=[self.competition.slug]))

        self.assertContains(response, 'No pending photos left for you.')

    def test_staff_round_1_review_displays_full_context_and_records_score(self):
        photo = self.create_photo(
            'Context image',
            Photo.Status.ROUND_1,
            category='Portrait',
            description='A full story for the photo.',
            camera_settings='50mm, f/2.8, ISO 400',
        )

        self.client.force_login(self.staff)
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
        self.assertEqual(photo.round_1_scores.get(judge=self.staff).score, 8)

    def test_finalize_shortlist_uses_top_ten_percent_of_round_1_scores(self):
        photos = [
            self.create_photo(f'Round 1 image {index}', Photo.Status.ROUND_1)
            for index in range(10)
        ]
        for index, photo in enumerate(photos):
            RoundOneScore.objects.create(photo=photo, judge=self.staff, score=index + 1)

        self.client.force_login(self.staff)
        response = self.client.post(reverse('finalize_shortlist', args=[self.competition.slug]))

        self.assertRedirects(response, reverse('home_hub'))
        statuses = {photo.id: Photo.objects.get(id=photo.id).status for photo in photos}
        self.assertEqual(statuses[photos[-1].id], Photo.Status.SHORTLISTED)
        self.assertEqual(
            sum(1 for status in statuses.values() if status == Photo.Status.SHORTLISTED),
            1,
        )


class CsvEncodingTests(TestCase):
    def test_decode_csv_bytes_accepts_windows_1252_smart_quotes(self):
        csv_bytes = b'Criterion Name,Description,Weight\r\nComposition,\x93Strong frame\x94,1.0\r\n'

        decoded = decode_csv_bytes(csv_bytes)

        self.assertIn('\u201cStrong frame\u201d', decoded)
