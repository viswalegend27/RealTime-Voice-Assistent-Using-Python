import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'voiceproject.settings')

django_asgi_app = get_asgi_application()

# Import after settings/configuration so apps are ready
import voiceapp.routing  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(voiceapp.routing.websocket_urlpatterns)
    ),
})
