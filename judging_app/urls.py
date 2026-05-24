from django.urls import path
from . import views

urlpatterns = [
    # The base URL now hits the router
    path('', views.judge_router, name='judge_router'),
    # The specific photo URL
    path('photo/<int:photo_id>/', views.judge_photo, name='judge_photo'),
]