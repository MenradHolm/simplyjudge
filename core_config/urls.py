from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # The default Django Admin portal
    path('admin/', admin.site.urls),
    
    # Restores the built-in login, logout, and password reset pages
    path('accounts/', include('django.contrib.auth.urls')),
    
    # Connects all the URLs from your judging_app
    path('', include('judging_app.urls')), 
]

# This ensures image files load correctly during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)