from django.contrib import admin
from django.urls import path, include
from chatapp import views  # or wherever your voice assistant view is
from django.conf import settings

urlpatterns = [
    path('admin/', admin.site.urls),
    path('chatapp/', include('chatapp.urls')),
    path('voice-assistant/', views.voice_assistant_view, name='voice_assistant'),  # Included URL path
]

if settings.DEBUG:
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns
    urlpatterns += staticfiles_urlpatterns()
