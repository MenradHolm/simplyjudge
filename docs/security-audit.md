# Credential and client exposure audit

Audit date: 27 October 2026. Scope: repository source and local Git history,
Django settings, templates and inline JavaScript, HTTP views, storage/payment/email
integrations, dependency pins, generated static assets including gzip variants,
and the new build verification workflow. Pre-existing user edits were preserved.

This is a server-rendered Django application, with no application JavaScript
bundler, frontend environment substitution, or existing deployment workflow in
the checkout. Client assets consist of template HTML/inline JavaScript and
collected dependency static files. Provider credentials belong only in the
server environment. No provider SDK or shared service credential was found in
application browser JavaScript.

## Findings and changes

| Severity | Finding | Resolution |
| --- | --- | --- |
| Critical | A Django signing key was hardcoded as the fallback, including in Git history. | Removed. Production startup requires a strong environment key; development and tests can generate ephemeral keys. Rotate any deployment using the old fallback. |
| Critical | Missing Stripe webhook secret enabled unsigned JSON payment fulfillment. | Missing configuration returns 503; signature validation is mandatory, invalid signatures return 400, and only paid events fulfill orders. Real SDK signatures, tampering, unpaid events, and replay are covered by tests. |
| High | Raw import/storage exceptions could reach browser messages and persisted job errors. | Generic browser errors; historical job errors are no longer rendered. Worker failure logs use job ID and exception type without exception text. |
| High | Remote ZIP URLs could reach internal services. | Remote ZIP downloads default to disabled. Exact trusted HTTPS host configuration, public-address validation, rejection of URL credentials/custom ports, and disabled redirects/proxies restrict server downloads. Media metadata downloads use the same controls. |
| High | Public results bypassed publication and rendered organizer notes/rule flags. | Results require publication. Private notes and flags render only for organizers, including in public feedback reports. |
| High | Old dependencies had published security advisories. | Updated direct dependencies and vulnerable transitive libraries; added a complete runtime lockfile. Before: 111 advisory records in three direct pins. Final requirements and installed runtime checks report no known vulnerabilities. |
| Medium | Broad hosting-domain trust, optional HTTP, public response caching, deprecated storage configuration. | Narrow host/origin defaults, production HTTPS defaults, secure cookies, same-origin referrer policy (no-referrer broke CSRF origin checks on login), no-store for authenticated/account/invitation responses, modern STORAGES, and production static finder fallback disabled. |
| Medium | No artifact or dependency release gate; local backups/exports could be accidentally committed. | Added CI and a reusable release gate, source and artifact scanners, map exclusion/rejection, and ignores for environment variants, key files, databases/backups, exports, and generated assets. |

## Verification and release procedure

Install `requirements.lock` and `requirements-security.txt` in the server build
environment; upgrade the build environment's pip. `requirements.txt` records
direct dependencies; regenerate the lockfile when changing them and audit the
complete resolved set. CI uses Python 3.12; local verification uses Python 3.14.

Run:

```text
python manage.py test
python tools/verify_release.py
```

The release gate fails on source credential patterns, published dependency
advisories, Django deployment warnings, collection errors, or sensitive public
artifacts. It clears/rebuilds `staticfiles`, omits source maps, and scans the final
files and decompressed gzip contents. Unexpected Brotli files are rejected
because this scanner does not decode them. Only this verified `staticfiles`
directory may be exposed by the static web server. Never publish the repository
root, virtual environment, databases, exports, environment files, or backups.

The scanner checks provider key formats, private keys, credential-bearing URLs,
JWTs, literal secret assignments, and current server environment secret values
(also URL/base64 encodings) in public artifacts. It reports paths/categories,
never secret matches. Tests inject synthetic server secrets into rendered public
and organizer pages and assert that responses omit them. It is a heuristic gate,
not proof that every possible sensitive value or encoding is absent.

History scanning flagged two revisions: the old signing-key fallback in
`core_config/settings.py`, and a synthetic test password in `judging_app/tests.py`.
History was not rewritten. `python tools/security_scan.py --history` intentionally
returns failure while historical findings remain.

Local verification: the full 122-test suite passed, followed by the security
suite after adding admin redaction coverage. The release gate passed with
synthetic server secret canaries: 141 collected files plus 141 gzip variants
(282 files) scanned cleanly, with no source-map files. Both the complete locked
dependency set and installed runtime reported no known vulnerabilities. Django
deployment checks passed with explicit HTTPS/HSTS settings, and migration checks
reported no model changes. Signed import URLs, server paths, and historical raw
job errors are also excluded from the admin browser interface.

## Required production actions and limits

1. Set a newly generated server SECRET_KEY before release if the deployed key
   could be the former fallback. Do not retain that compromised value in signing
   key fallbacks. Rotation invalidates existing signed sessions/reset links.
2. Configure Stripe API and webhook secrets, database/Cloudinary credentials,
   and SMTP authentication in the hosting secret manager. `.env.example` contains
   names and non-secret defaults only; this application does not load it.
3. Configure exact production hosts/origins and HTTPS at the trusted reverse
   proxy. The proxy must overwrite forwarded-protocol headers. Enable HSTS
   subdomain/preload settings only after confirming the entire domain supports
   them; deployment warnings intentionally block the release gate otherwise.
4. Allow remote ZIP hosts only when their DNS and content are controlled by a
   trusted administrator. Address validation is not DNS pinning; enforce an
   outbound firewall excluding internal/metadata networks as defense in depth.
5. Use the gate in the actual deployment build command and require successful CI
   before release. The workflow does not itself configure branch protection or
   the Render deployment service.

Live hosting environment values, previously deployed assets/caches, cloud storage
ACLs, provider dashboards, external access logs, and credential rotation could
not be inspected or changed from this checkout. Local private data and backups
were preserved. Public photo storage can contain EXIF/location metadata; RAW
storage privacy and existing uploaded assets require a separate provider-side
ACL/data review. Feedback-portal reports remain intentionally public; organizer
notes and rule flags are now excluded. Per-user CSRF/reset/invitation tokens are
intentional browser-facing capabilities, distinct from shared provider secrets.

Configuration and payment verification follow the official
[Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)
and [Stripe webhook verification guidance](https://docs.stripe.com/webhooks/signature).
