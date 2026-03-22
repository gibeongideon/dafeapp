import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from cloud.forms import CloudAccountForm, PyOSSSHSettingsForm
from cloud.models import PyOSSSHSettings
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

    def test_pyos_settings_rejects_public_key_text(self):
        form = PyOSSSHSettingsForm(
            data={
                "default_ssh_key_path": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI public@example",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("default_ssh_key_path", form.errors)

    def test_pyos_settings_saves_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "id_ed25519"
            key_file.write_text("dummy-private-key")
            settings_obj = PyOSSSHSettings.get_or_create_settings()
            form = PyOSSSHSettingsForm(
                data={"default_ssh_key_path": str(key_file)},
                instance=settings_obj,
            )
            self.assertTrue(form.is_valid())
            saved = form.save()
            self.assertEqual(saved.default_ssh_key_path, str(key_file))
