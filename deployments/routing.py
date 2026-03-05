from django.urls import re_path

from deployments.consumers import DeploymentRunConsumer, OdooInstanceConsumer, OdooServerConsumer

websocket_urlpatterns = [
    re_path(r"ws/deployments/runs/(?P<run_id>\d+)/$", DeploymentRunConsumer.as_asgi()),
    re_path(r"ws/deployments/servers/(?P<server_id>\d+)/$", OdooServerConsumer.as_asgi()),
    re_path(r"ws/deployments/instances/(?P<instance_id>\d+)/$", OdooInstanceConsumer.as_asgi()),
]
