from django.urls import path
from . import views

urlpatterns = [
    # Global Hub & Authentication
    path('', views.home_hub, name='home_hub'),
    path('register/', views.register_user, name='register'),
    
    # Competition-Specific Routes (Using Slugs)
    path('competition/<slug:comp_slug>/', views.judge_router, name='judge_router'),
    path('competition/<slug:comp_slug>/judge/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('competition/<slug:comp_slug>/leaderboard/', views.leaderboard, name='leaderboard'),
    path('competition/<slug:comp_slug>/submit/', views.submit_photo, name='submit_photo'),
    path('competition/<slug:comp_slug>/ledger-report/', views.feedback_report, name='feedback_report'),
    
    # Upload Data Routes (Using Slugs)
    path('competition/<slug:comp_slug>/upload-csv/', views.upload_spreadsheet, name='upload_spreadsheet'),
    path('competition/<slug:comp_slug>/upload-zip/', views.upload_photos_zip, name='upload_photos_zip'),
    
    # Public Results Route
    path('competition/<slug:comp_slug>/public-results/', views.public_results, name='public_results'),
]