from deployments.routing import websocket_urlpatterns as deployments_ws

websocket_urlpatterns = [
    *deployments_ws,
]
