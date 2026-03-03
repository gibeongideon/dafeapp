"""
AWS provider integration (EC2 + Security Groups) using boto3.
"""

import os

from cloud.base import AbstractCloudProvider
from cloud.encryption import FieldEncryptor

AWS_REGIONS = [
    ("us-east-1", "US East (N. Virginia)"),
    ("us-east-2", "US East (Ohio)"),
    ("us-west-1", "US West (N. California)"),
    ("us-west-2", "US West (Oregon)"),
    ("ca-central-1", "Canada (Central)"),
    ("eu-west-1", "Europe (Ireland)"),
    ("eu-west-2", "Europe (London)"),
    ("eu-central-1", "Europe (Frankfurt)"),
    ("ap-southeast-1", "Asia Pacific (Singapore)"),
    ("ap-southeast-2", "Asia Pacific (Sydney)"),
    ("ap-south-1", "Asia Pacific (Mumbai)"),
]

AWS_SIZES = [
    ("t3.micro", "t3.micro (2 vCPU, 1 GiB)"),
    ("t3.small", "t3.small (2 vCPU, 2 GiB)"),
    ("t3.medium", "t3.medium (2 vCPU, 4 GiB)"),
    ("t3.large", "t3.large (2 vCPU, 8 GiB)"),
    ("t3.xlarge", "t3.xlarge (4 vCPU, 16 GiB)"),
    ("m6i.large", "m6i.large (2 vCPU, 8 GiB)"),
    ("m6i.xlarge", "m6i.xlarge (4 vCPU, 16 GiB)"),
    ("c6i.large", "c6i.large (2 vCPU, 4 GiB)"),
    ("c6i.xlarge", "c6i.xlarge (4 vCPU, 8 GiB)"),
]


class AWSProvider(AbstractCloudProvider):
    def __init__(self, cloud_account):
        self.account = cloud_account
        self.access_key = FieldEncryptor.decrypt(cloud_account.encrypted_aws_access_key_id)
        self.secret_key = FieldEncryptor.decrypt(cloud_account.encrypted_aws_secret_access_key)
        self.default_region = cloud_account.aws_default_region or "us-east-1"
        self._ec2_by_region = {}

    def _boto3_client(self, service: str, region: str | None = None):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for AWS provider support.") from exc

        return boto3.client(
            service,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=region or self.default_region,
        )

    def _ec2(self, region: str | None = None):
        key = region or self.default_region
        if key not in self._ec2_by_region:
            self._ec2_by_region[key] = self._boto3_client("ec2", region=key)
        return self._ec2_by_region[key]

    def validate_credentials(self) -> tuple[bool, str]:
        try:
            sts = self._boto3_client("sts")
            identity = sts.get_caller_identity()
            account_id = identity.get("Account", "unknown")
            return True, f"Credentials valid for AWS account {account_id}."
        except Exception as exc:
            return False, f"AWS credential validation failed: {exc}"

    def create_server(self, name: str, region: str, size: str, ssh_key_ids: list | None = None) -> dict:
        image_id = os.getenv("AWS_DEFAULT_AMI_ID", "ami-0fc5d935ebf8bc3bc")
        ec2 = self._ec2(region)
        resp = ec2.run_instances(
            ImageId=image_id,
            InstanceType=size,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": name}],
                }
            ],
        )
        instance = (resp.get("Instances") or [{}])[0]
        return {"id": instance.get("InstanceId", "")}

    def destroy_server(self, provider_server_id: str) -> bool:
        try:
            ec2 = self._ec2()
            ec2.terminate_instances(InstanceIds=[provider_server_id])
            return True
        except Exception:
            return False

    def create_firewall(self, provider_server_id: str) -> dict:
        ec2 = self._ec2()
        describe = ec2.describe_instances(InstanceIds=[provider_server_id])
        reservation = (describe.get("Reservations") or [{}])[0]
        instance = (reservation.get("Instances") or [{}])[0]
        vpc_id = instance.get("VpcId")
        if not vpc_id:
            return {}

        sg_name = f"dafeapp-fw-{provider_server_id[-8:]}"
        sg = ec2.create_security_group(
            GroupName=sg_name,
            Description="DafeApp managed firewall",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
            ],
        )
        ec2.modify_instance_attribute(
            InstanceId=provider_server_id,
            Groups=[sg_id],
        )
        return {"group_id": sg_id}

    def get_server_status(self, provider_server_id: str) -> str:
        try:
            ec2 = self._ec2()
            describe = ec2.describe_instances(InstanceIds=[provider_server_id])
            reservation = (describe.get("Reservations") or [{}])[0]
            instance = (reservation.get("Instances") or [{}])[0]
            state = (instance.get("State") or {}).get("Name", "unknown")
            return state
        except Exception:
            return "unknown"

    def get_server_ip(self, provider_server_id: str) -> str:
        try:
            ec2 = self._ec2()
            describe = ec2.describe_instances(InstanceIds=[provider_server_id])
            reservation = (describe.get("Reservations") or [{}])[0]
            instance = (reservation.get("Instances") or [{}])[0]
            return instance.get("PublicIpAddress", "") or ""
        except Exception:
            return ""

    def list_regions(self) -> list[tuple[str, str]]:
        try:
            ec2 = self._ec2("us-east-1")
            resp = ec2.describe_regions(AllRegions=False)
            regions = []
            for region in resp.get("Regions", []):
                name = region.get("RegionName")
                if name:
                    regions.append((name, name))
            return regions or AWS_REGIONS
        except Exception:
            return AWS_REGIONS

    def list_sizes(self, region: str = "") -> list[tuple[str, str]]:
        return AWS_SIZES
