import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import Competition, CompetitionMembership, EntryOrder, Photo, ZipImportJob
from .remote_files import NoRedirects, validate_download_url
from tools.security_scan import findings, scan


class ProductionConfigurationTests(SimpleTestCase):
    def test_production_requires_strong_server_key(self):
        env = {key: value for key, value in os.environ.items() if key not in {'SECRET_KEY', 'DEBUG'}}
        env['DEBUG'] = 'False'
        for value in ('', 'django-insecure-example'):
            with self.subTest(value=value):
                env['SECRET_KEY'] = value
                result = subprocess.run(
                    [sys.executable, '-c', 'import core_config.settings'],
                    env=env, capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Set a strong, unique SECRET_KEY', result.stderr)

    def test_scanner_rejects_credentials_maps_and_compressed_secrets(self):
        credential = ('sk_live_' + 'A' * 32).encode()
        self.assertTrue(findings(Path('app.js'), credential, artifacts=True))
        self.assertTrue(findings(Path('app.js.map'), b'{}', artifacts=True))
        self.assertTrue(findings(Path('app.js.map.gz'), b'{}', artifacts=True))
        self.assertTrue(findings(Path('.env.production'), b'', artifacts=True))
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / 'app.js.gz'
            bundle.write_bytes(gzip.compress(credential))
            self.assertTrue(scan([bundle], artifacts=True))
        self.assertTrue(findings(Path('app.js'), b'runtime-canary', True, [b'runtime-canary']))

    def test_remote_files_require_trusted_public_https_origin(self):
        for url in ('http://trusted.example/a.zip', 'https://evil.example/a.zip',
                    'https://' + 'user:pass' + '@trusted.example/a.zip', 'https://trusted.example:444/a.zip'):
            with self.subTest(url=url), patch('judging_app.remote_files.socket.getaddrinfo') as dns:
                with self.assertRaises(ValueError):
                    validate_download_url(url, ['trusted.example'])
                dns.assert_not_called()
        for address in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '::1'):
            with self.subTest(address=address), patch('judging_app.remote_files.socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 443))]):
                with self.assertRaises(ValueError):
                    validate_download_url('https://trusted.example/a.zip', ['trusted.example'])
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, '', {}, 'https://evil.example'))


@override_settings(SECURE_SSL_REDIRECT=False)
class ClientExposureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('audit-entrant')
        self.competition = Competition.objects.create(name='Security audit', slug='security-audit')
        self.order = EntryOrder.objects.create(user=self.user, competition=self.competition, stripe_checkout_id='cs_audit', amount_paid='25.00')

    def test_webhook_without_server_secret_cannot_grant_access(self):
        with override_settings(STRIPE_WEBHOOK_SECRET=''):
            response = self.client.post(reverse('stripe_webhook'), '{}', content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_invalid_webhook_signature_never_mutates_orders(self):
        fake = SimpleNamespace(Webhook=SimpleNamespace(construct_event=Mock(side_effect=ValueError('private-service-detail'))))
        with override_settings(STRIPE_WEBHOOK_SECRET='test-webhook'), patch('judging_app.views.stripe', fake):
            response = self.client.post(reverse('stripe_webhook'), '{}', content_type='application/json', HTTP_STRIPE_SIGNATURE='invalid')
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, 'private-service-detail', status_code=400)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_real_sdk_signature_and_replay(self):
        import stripe
        event = {'type': 'checkout.session.completed', 'data': {'object': {'id': 'cs_audit', 'payment_status': 'paid'}}}
        payload = json.dumps(event)
        timestamp = int(time.time())
        secret = 'test-webhook'
        digest = hmac.new(secret.encode(), f'{timestamp}.{payload}'.encode(), hashlib.sha256).hexdigest()
        signature = f't={timestamp},v1={digest}'
        with override_settings(STRIPE_WEBHOOK_SECRET=secret), patch('judging_app.views.stripe', stripe):
            for _ in range(2):
                response = self.client.post(reverse('stripe_webhook'), payload, content_type='application/json', HTTP_STRIPE_SIGNATURE=signature)
                self.assertEqual(response.status_code, 200)
            response = self.client.post(reverse('stripe_webhook'), payload + ' ', content_type='application/json', HTTP_STRIPE_SIGNATURE=signature)
            self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.assertEqual(CompetitionMembership.objects.filter(user=self.user, role='ENTRANT').count(), 1)

    def test_unpaid_signed_event_does_not_fulfill(self):
        event = {'type': 'checkout.session.completed', 'data': {'object': {'id': 'cs_audit', 'payment_status': 'unpaid'}}}
        fake = SimpleNamespace(Webhook=SimpleNamespace(construct_event=Mock(return_value=event)))
        with override_settings(STRIPE_WEBHOOK_SECRET='test-webhook'), patch('judging_app.views.stripe', fake):
            self.client.post(reverse('stripe_webhook'), '{}', content_type='application/json', HTTP_STRIPE_SIGNATURE='mock')
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_public_results_require_publication_and_exclude_private_notes(self):
        Photo.objects.create(competition=self.competition, title='Entry', photographer_name='Entrant', organizer_notes='private-note-canary', rule_flags='private-rule-canary')
        url = reverse('public_results', args=[self.competition.slug])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.competition.results_published = True
        self.competition.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'private-note-canary')
        self.assertNotContains(response, 'private-rule-canary')

    def test_historical_import_errors_are_never_rendered(self):
        CompetitionMembership.objects.create(competition=self.competition, user=self.user, role='ORGANIZER')
        job = ZipImportJob.objects.create(competition=self.competition, uploaded_by=self.user, source_name='audit.zip', status='failed', error_message='private-service-canary')
        self.client.force_login(self.user)
        response = self.client.get(reverse('zip_import_status', args=[self.competition.slug, job.id]))
        self.assertNotContains(response, 'private-service-canary')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_server_secrets_are_absent_from_rendered_public_and_private_pages(self):
        CompetitionMembership.objects.create(competition=self.competition, user=self.user, role='ORGANIZER')
        canary = 'server-only-' + 'a' * 40
        with override_settings(SECRET_KEY=canary, STRIPE_SECRET_KEY=canary, EMAIL_HOST_PASSWORD=canary):
            self.client.force_login(self.user)
            for url in ('/', '/accounts/login/', reverse('upload_spreadsheet', args=[self.competition.slug]), reverse('competition_progress', args=[self.competition.slug])):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, canary)

    def test_admin_does_not_render_signed_import_urls_or_historical_errors(self):
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        job = ZipImportJob.objects.create(
            competition=self.competition, uploaded_by=self.user, source_name='audit.zip',
            source_url='https://trusted.example/a.zip?token=private-url-canary',
            temp_path='/private-path-canary', error_message='private-error-canary',
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin:judging_app_zipimportjob_change', args=[job.id]))
        self.assertEqual(response.status_code, 200)
        for canary in ('private-url-canary', 'private-path-canary', 'private-error-canary'):
            self.assertNotContains(response, canary)
