from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from cloud.models import CloudAccount
from organizations.models import Organization

User = get_user_model()
KEY = "HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g="


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class AWSProviderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="aws@test.com", password="pass")
        cls.org = Organization.objects.create(name="AWS Org", owner=cls.user)

    def _account(self):
        account = CloudAccount(
            organization=self.org,
            provider=CloudAccount.Provider.AWS,
            name="AWS Test",
        )
        account._raw_aws_access_key_id = "AKIA_TEST"
        account._raw_aws_secret_access_key = "SECRET_TEST"
        account.save()
        return account

    @patch("cloud.aws.AWSProvider._boto3_client")
    def test_validate_credentials_success(self, mock_client):
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_client.return_value = sts

        from cloud.aws import AWSProvider

        provider = AWSProvider(self._account())
        ok, msg = provider.validate_credentials()
        self.assertTrue(ok)
        self.assertIn("123456789012", msg)

    @patch("cloud.aws.AWSProvider._boto3_client")
    def test_list_regions_fallback_on_error(self, mock_client):
        mock_client.side_effect = RuntimeError("boom")
        from cloud.aws import AWSProvider, AWS_REGIONS

        provider = AWSProvider(self._account())
        regions = provider.list_regions()
        self.assertEqual(regions, AWS_REGIONS)
