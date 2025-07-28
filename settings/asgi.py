import os

from channels.auth import AuthMiddlewareStack  # Для аутентификации в WebSockets
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

import applications.article.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')


django_asgi_app = get_asgi_application() # Приложение Django для обычных HTTP запросов


application = ProtocolTypeRouter({
    "http": django_asgi_app, # Обычные HTTP запросы обрабатываются Django
    "websocket": AuthMiddlewareStack( # WebSocket запросы с аутентификацией Django
        URLRouter(
            applications.article.routing.websocket_urlpatterns # Маршруты для WebSocket
        )
    ),
})