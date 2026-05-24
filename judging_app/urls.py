from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Hub Homepage
    path('', views.home_hub, name='home_hub'),

    # Auth Routes
    path('register/', views.register_user, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='judging_app/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/login/'), name='logout'),

    # App Routes
    path('panel/', views.judge_router, name='judge_router'), # <-- Moved to /panel/
    path('photo/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('leaderboard/', views.leaderboard, name='leaderboard'),
    path('submit/', views.submit_photo, name='submit_photo'),
]