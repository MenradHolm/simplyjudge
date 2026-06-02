from django.urls import path
from django.views.generic import TemplateView
from . import views

urlpatterns = [
    path('', views.home_hub, name='home_hub'),
    path('register/', views.register_user, name='register'),
    path('impressum/', TemplateView.as_view(template_name='judging_app/impressum.html'), name='impressum'),
    path('privacy/', TemplateView.as_view(template_name='judging_app/privacy.html'), name='privacy'),
    path('terms/', TemplateView.as_view(template_name='judging_app/terms.html'), name='terms'),
    path('stripe/webhook/', views.stripe_webhook, name='stripe_webhook'),
    path('judge-invite/<uuid:token>/', views.accept_judge_invite, name='accept_judge_invite'),
    
    # Competition-Specific Routes (Using Slugs)
    path('competition/<slug:comp_slug>/', views.judge_router, name='judge_router'),
    path('competition/<slug:comp_slug>/my-scores/', views.judge_review, name='judge_review'),
    path('competition/<slug:comp_slug>/eliminate/', views.elimination_mode, name='elimination_mode'),
    path('competition/<slug:comp_slug>/round-1-review/', views.round_1_review, name='round_1_review'),
    path('competition/<slug:comp_slug>/finalize-shortlist/', views.finalize_shortlist, name='finalize_shortlist'),
    path('competition/<slug:comp_slug>/judge/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('competition/<slug:comp_slug>/judge/<int:photo_id>/autosave/', views.autosave_judge_score, name='autosave_judge_score'),
    path('competition/<slug:comp_slug>/leaderboard/', views.leaderboard, name='leaderboard'),
    path('competition/<slug:comp_slug>/submit/', views.submit_photo, name='submit_photo'),
    path('competition/<slug:comp_slug>/photo/<int:photo_id>/upload-raw/', views.upload_raw_file, name='upload_raw_file'),
    path('competition/<slug:comp_slug>/checkout/', views.create_checkout_session, name='create_checkout_session'),
    path('competition/<slug:comp_slug>/ledger-report/', views.feedback_report, name='feedback_report'),
    path('competition/<slug:comp_slug>/results-csv/', views.export_competition_results_csv, name='export_competition_results_csv'),
    
    # Upload Data Routes (Using Slugs)
    path('competition/<slug:comp_slug>/upload-csv/', views.upload_spreadsheet, name='upload_spreadsheet'),
    path('competition/<slug:comp_slug>/upload-zip/', views.upload_photos_zip, name='upload_photos_zip'),
    path('competition/<slug:comp_slug>/upload-photos-only/', views.upload_photos_only_zip, name='upload_photos_only_zip'),
    path('competition/<slug:comp_slug>/upload-zip-chunk/', views.upload_zip_chunk, name='upload_zip_chunk'),
    path('competition/<slug:comp_slug>/zip-import/<int:job_id>/', views.zip_import_status, name='zip_import_status'),
    
    # Public Results Route
    path('competition/<slug:comp_slug>/public-results/', views.public_results, name='public_results'),
]
