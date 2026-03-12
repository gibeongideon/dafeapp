import json

from channels.generic.websocket import AsyncWebsocketConsumer


class DeploymentRunConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.run_id = self.scope["url_route"]["kwargs"]["run_id"]
        self.group_name = f"deployments.run.{self.run_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def deployment_update(self, event):
        await self.send(text_data=json.dumps(event["payload"]))


class OdooServerConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.server_id = self.scope["url_route"]["kwargs"]["server_id"]
        self.group_name = f"odoo.server.{self.server_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def server_update(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    async def log_line(self, event):
        """Receive a single streamed Ansible/Terraform log line."""
        await self.send(text_data=json.dumps(event["payload"]))


class OdooInstanceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.instance_id = self.scope["url_route"]["kwargs"]["instance_id"]
        self.group_name = f"odoo.instance.{self.instance_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def instance_update(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    async def log_line(self, event):
        """Receive a single streamed Ansible log line."""
        await self.send(text_data=json.dumps(event["payload"]))
