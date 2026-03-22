from django import forms

from cloud.digitalocean import DO_REGIONS, DO_SIZES
from cloud.pyos import looks_like_public_key_text
from cloud.models import CloudAccount, CloudServer, ExternalServer, PyOSSSHSettings
from cloud.providers import get_provider

_INPUT = "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-400"
_SELECT = "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-gray-400"


class ExternalServerForm(forms.ModelForm):
    """
    Form for adding a PYOS server.
    Raw credential field (password) is set on the model instance as _raw_password;
    the model's save() encrypts it. Alpine.js toggles credential field based on auth_type.
    """

    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(render_value=False, attrs={"class": _INPUT}),
        required=False,
    )
    ssh_key_path = forms.CharField(
        label="SSH Key Path",
        widget=forms.TextInput(attrs={"class": _INPUT, "placeholder": "~/.ssh/id_ed25519"}),
        required=False,
        help_text="Optional. Use a private key path on the server running DafeApp.",
    )

    class Meta:
        model = ExternalServer
        fields = ["name", "host", "port", "username", "auth_type"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _INPUT}),
            "host": forms.TextInput(attrs={"class": _INPUT}),
            "port": forms.NumberInput(attrs={"min": 1, "max": 65535, "class": _INPUT}),
            "username": forms.TextInput(attrs={"class": _INPUT}),
            "auth_type": forms.RadioSelect(),
        }

    def clean(self):
        cleaned = super().clean()
        auth_type = cleaned.get("auth_type")
        if auth_type == ExternalServer.AuthType.PASSWORD and not cleaned.get("password"):
            self.add_error("password", "Password is required for password-based auth.")
        ssh_key_path = (cleaned.get("ssh_key_path") or "").strip()
        if ssh_key_path:
            if looks_like_public_key_text(ssh_key_path):
                self.add_error(
                    "ssh_key_path",
                    "SSH key path must be a file path on the machine running DafeApp, not pasted public key text.",
                )
            else:
                cleaned["ssh_key_path"] = ssh_key_path
        # DAFEAPP_KEY: no credential needed — DafeApp's own keypair is used
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        pw = self.cleaned_data.get("password")
        instance.ssh_key_path = (self.cleaned_data.get("ssh_key_path") or "").strip()
        if pw:
            instance._raw_password = pw
        if commit:
            instance.save()
        return instance


class PyOSSSHSettingsForm(forms.ModelForm):
    default_ssh_key_path = forms.CharField(
        label="Default SSH Key Path",
        widget=forms.TextInput(attrs={"class": _INPUT, "placeholder": "/home/rock/.ssh/id_ed25519"}),
        required=False,
        help_text="Optional. Used for PYOS servers when no per-server path is provided.",
    )

    class Meta:
        model = PyOSSSHSettings
        fields = ["default_ssh_key_path"]

    def clean(self):
        cleaned = super().clean()
        key_path = (cleaned.get("default_ssh_key_path") or "").strip()
        if key_path and looks_like_public_key_text(key_path):
            self.add_error(
                "default_ssh_key_path",
                "SSH key path must be a file path on the machine running DafeApp, not pasted public key text.",
            )
        else:
            cleaned["default_ssh_key_path"] = key_path
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.default_ssh_key_path = (self.cleaned_data.get("default_ssh_key_path") or "").strip()
        if commit:
            instance.save()
        return instance


class CloudAccountForm(forms.ModelForm):
    """Form for adding a cloud account (DigitalOcean or AWS)."""

    api_token = forms.CharField(
        label="DigitalOcean API Token",
        widget=forms.PasswordInput(render_value=False, attrs={"class": _INPUT}),
        help_text="Your DigitalOcean Personal Access Token (read + write).",
        required=False,
    )
    aws_access_key_id = forms.CharField(
        label="AWS Access Key ID",
        widget=forms.TextInput(attrs={"class": _INPUT, "autocomplete": "off"}),
        required=False,
    )
    aws_secret_access_key = forms.CharField(
        label="AWS Secret Access Key",
        widget=forms.PasswordInput(render_value=False, attrs={"class": _INPUT}),
        required=False,
    )
    aws_default_region = forms.CharField(
        label="AWS Default Region",
        widget=forms.TextInput(attrs={"class": _INPUT, "placeholder": "us-east-1"}),
        required=False,
    )

    class Meta:
        model = CloudAccount
        fields = ["name", "provider", "aws_default_region"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _INPUT}),
            "provider": forms.Select(attrs={"class": _SELECT}),
            "aws_default_region": forms.TextInput(attrs={"class": _INPUT}),
        }

    def clean(self):
        cleaned = super().clean()
        provider = cleaned.get("provider")
        if provider == CloudAccount.Provider.DIGITALOCEAN:
            if not cleaned.get("api_token"):
                self.add_error("api_token", "DigitalOcean API token is required.")
        elif provider == CloudAccount.Provider.AWS:
            if not cleaned.get("aws_access_key_id"):
                self.add_error("aws_access_key_id", "AWS access key ID is required.")
            if not cleaned.get("aws_secret_access_key"):
                self.add_error("aws_secret_access_key", "AWS secret access key is required.")
            if not cleaned.get("aws_default_region"):
                cleaned["aws_default_region"] = "us-east-1"
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        provider = self.cleaned_data["provider"]
        if provider == CloudAccount.Provider.DIGITALOCEAN:
            instance._raw_api_token = self.cleaned_data["api_token"]
            instance.encrypted_aws_access_key_id = ""
            instance.encrypted_aws_secret_access_key = ""
            instance.aws_default_region = ""
        elif provider == CloudAccount.Provider.AWS:
            instance._raw_aws_access_key_id = self.cleaned_data["aws_access_key_id"]
            instance._raw_aws_secret_access_key = self.cleaned_data["aws_secret_access_key"]
            instance.aws_default_region = self.cleaned_data.get("aws_default_region") or "us-east-1"
            instance.encrypted_api_token = ""
        if commit:
            instance.save()
        return instance


class ProvisionDropletForm(forms.ModelForm):
    """Form for provisioning a managed cloud server."""

    region = forms.ChoiceField(choices=DO_REGIONS, label="Region")
    size = forms.ChoiceField(choices=DO_SIZES, label="Size")

    class Meta:
        model = CloudServer
        fields = ["name", "cloud_account", "region", "size"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _INPUT}),
            "cloud_account": forms.Select(attrs={"class": _SELECT}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._organization = organization
        if organization:
            self.fields["cloud_account"].queryset = CloudAccount.objects.filter(
                organization=organization, is_verified=True
            )
        self.fields["cloud_account"].label = "Cloud Account"
        self.fields["region"].widget.attrs["class"] = _SELECT
        self.fields["size"].widget.attrs["class"] = _SELECT

        selected_id = None
        if self.is_bound:
            selected_id = self.data.get("cloud_account")
        elif self.initial.get("cloud_account"):
            selected_id = str(self.initial.get("cloud_account"))
        else:
            first = self.fields["cloud_account"].queryset.first()
            selected_id = str(first.pk) if first else None
        self._set_dynamic_choices(selected_id)

    def _set_dynamic_choices(self, cloud_account_id):
        default_regions = DO_REGIONS
        default_sizes = DO_SIZES
        if not cloud_account_id:
            self.fields["region"].choices = default_regions
            self.fields["size"].choices = default_sizes
            return
        try:
            account = CloudAccount.objects.get(
                pk=cloud_account_id,
                organization=self._organization,
                is_verified=True,
            )
            provider = get_provider(account)
            regions = provider.list_regions()
            sizes = provider.list_sizes()
            self.fields["region"].choices = regions or default_regions
            self.fields["size"].choices = sizes or default_sizes
        except Exception:
            self.fields["region"].choices = default_regions
            self.fields["size"].choices = default_sizes
