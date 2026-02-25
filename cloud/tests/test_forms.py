from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from cloud.forms import CloudAccountForm
from organizations.models import Organization

User = get_user_model()
KEY = "HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g="


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class CloudAccountFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="form@test.com", password="pass")
        cls.org = Organization.objects.create(name="Forms Org", owner=cls.user)

    def test_digitalocean_requires_token(self):
        form = CloudAccountForm(
            data={"name": "DO", "provider": "DIGITALOCEAN", "api_token": ""}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("api_token", form.errors)

    def test_aws_requires_access_and_secret(self):
        form = CloudAccountForm(
            data={
                "name": "AWS",
                "provider": "AWS",
                "aws_access_key_id": "",
                "aws_secret_access_key": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("aws_access_key_id", form.errors)
        self.assertIn("aws_secret_access_key", form.errors)
