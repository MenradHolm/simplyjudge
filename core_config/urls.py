from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# --- CUSTOM BRANDING ---
admin.site.site_header = "Simply Judge Administration"
admin.site.site_title = "Simply Judge Portal"
admin.site.index_title = "Welcome to Simply Judge"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('judging_app.urls')), 
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns = [
    path('admin/', admin.site.urls),
    # This points the root website domain to your judging app
    path('', include('judging_app.urls')), 
]

# This is required so your local server can display the uploaded image files
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)