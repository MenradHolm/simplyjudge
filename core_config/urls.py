from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView
from judging_app import views

# =====================================================================
# CUSTOM ADMIN BRANDING
# =====================================================================
admin.site.site_header = "SimplyJudge Administration"
admin.site.site_title = "SimplyJudge Admin Portal"
admin.site.index_title = "Welcome to the SimplyJudge Control Room"

# =====================================================================
# URL ROUTING
# =====================================================================
urlpatterns = [
    # Admin Panel
    path('admin/', admin.site.urls),
    
    # Auth & Hub
    path('', views.home_hub, name='home_hub'),
    path('register/', views.register_user, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='judging_app/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    
    # Judging & Submission
    path('competition/<int:comp_id>/submit/', views.submit_photo, name='submit_photo'),
    path('competition/<int:comp_id>/panel/', views.judge_router, name='judge_router'),
    path('competition/<int:comp_id>/photo/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('competition/<int:comp_id>/leaderboard/', views.leaderboard, name='leaderboard'),
    path('competition/<int:comp_id>/report/', views.feedback_report, name='feedback_report'),
    path('competition/<int:comp_id>/upload-csv/', views.upload_spreadsheet, name='upload_spreadsheet'),
    path('competition/<int:comp_id>/results/', views.public_results, name='public_results'),
    path('competition/<int:comp_id>/upload-zip/', views.upload_photos_zip, name='upload_photos_zip'),
    
    # Legal Pages
    path('impressum/', TemplateView.as_view(template_name='judging_app/impressum.html'), name='impressum'),
    path('privacy/', TemplateView.as_view(template_name='judging_app/privacy.html'), name='privacy'),
    path('terms/', TemplateView.as_view(template_name='judging_app/terms.html'), name='terms'),
]