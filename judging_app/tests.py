from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Competition, Photo
from .views import decode_csv_bytes


class PhotoStatusWorkflowTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(name='Youth POTY', slug='youth-poty')
        self.guest_judge = User.objects.create_user(username='judge', password='test-pass')
        self.staff = User.objects.create_user(username='staff', password='test-pass', is_staff=True)
        self.competition.judges.add(self.guest_judge)

    def create_photo(self, title, status):
        return Photo.objects.create(
            competition=self.competition,
            title=title,
            photographer_name='Hidden Entrant',
            category='General',
            image='competition_photos/placeholder.jpg',
            status=status,
        )

    def test_guest_judge_router_only_serves_shortlisted_photos(self):
        self.create_photo('Pending image', Photo.Status.PENDING)
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

    def test_staff_elimination_mode_updates_photo_status(self):
        photo = self.create_photo('Pending image', Photo.Status.PENDING)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('elimination_mode', args=[self.competition.slug]),
            {'photo_id': photo.id, 'decision': 'shortlist'},
        )

        self.assertRedirects(response, reverse('elimination_mode', args=[self.competition.slug]))
        photo.refresh_from_db()
        self.assertEqual(photo.status, Photo.Status.SHORTLISTED)


class CsvEncodingTests(TestCase):
    def test_decode_csv_bytes_accepts_windows_1252_smart_quotes(self):
        csv_bytes = b'Criterion Name,Description,Weight\r\nComposition,\x93Strong frame\x94,1.0\r\n'

        decoded = decode_csv_bytes(csv_bytes)

        self.assertIn('“Strong frame”', decoded)
