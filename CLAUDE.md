# SimplyJudge
Django photo-competition judging app (repo MenradHolm/simplyjudge, deploys to Render, media on Cloudinary).

## Run
- venv: `python -m venv venv && venv\Scripts\activate && pip install -r requirements.lock`
- `python manage.py runserver` (set DEBUG=True and a SECRET_KEY, see .env.example)
- Tests: `python manage.py test judging_app`. Security checks: `python tools/security_scan.py`, `python tools/verify_release.py`

## Layout
- `core_config/` settings, `judging_app/` models/views/admin/utils, `judging_app/remote_files.py` safe ZIP download
- `judging_app/offline/` + management commands `export_offline_judging_pack`, `import_offline_scores`: offline judge panels
- `docs/` organiser handbook, judge guide, security audit

## Rules
- Production needs strong SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS in env. Never reintroduce insecure defaults.
- Never commit generated judge packs (`*_panel.html`), score exports, `.env`, or sqlite files.
- Competition photos and CSVs live outside the repo (archive folder), not in git.
- Ask before committing, pushing (triggers Render deploy) or deleting.
