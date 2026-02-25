from django.urls import re_path

from deployments.consumers import DeploymentRunConsumer

websocket_urlpatterns = [
    re_path(r"ws/deployments/runs/(?P<run_id>\d+)/$", DeploymentRunConsumer.as_asgi()),
]
