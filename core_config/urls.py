from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # The default Django Admin portal
    path('admin/', admin.site.urls),
    
    # Connects all the URLs from your judging_app
    path('', include('judging_app.urls')), 
]

# This ensures image files (like your POTY photos) load correctly during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)