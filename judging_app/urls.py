from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # 1. GLOBAL PUBLIC PATHS (These stay the same)
    path('', views.home_hub, name='home_hub'),
    path('register/', views.register_user, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='judging_app/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/login/'), name='logout'),

    # 2. COMPETITION ECOSYSTEMS (These completely replace the old individual paths)
    path('competition/<int:comp_id>/submit/', views.submit_photo, name='submit_photo'),
    path('competition/<int:comp_id>/panel/', views.judge_router, name='judge_router'),
    path('competition/<int:comp_id>/photo/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('competition/<int:comp_id>/leaderboard/', views.leaderboard, name='leaderboard'),
]