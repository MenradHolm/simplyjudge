from django.urls import path
from . import views

urlpatterns = [
    path('', views.judge_router, name='judge_router'),
    path('photo/<int:photo_id>/', views.judge_photo, name='judge_photo'),
    path('leaderboard/', views.leaderboard, name='leaderboard'),
    # Add this public route:
    path('submit/', views.submit_photo, name='submit_photo'),
]