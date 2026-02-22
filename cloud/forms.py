from django import forms

from cloud.digitalocean import DO_REGIONS, DO_SIZES
from cloud.models import CloudAccount, CloudServer, ExternalServer

_INPUT = "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-400"
_SELECT = "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-gray-400"


class ExternalServerForm(forms.ModelForm):
    """
    Form for adding a PYOS server.
    Raw credential fields (private_key / password) are set on the model
    instance as _raw_* attributes; the model's save() encrypts them.
    Alpine.js toggles key vs password field based on auth_type.
    """

    private_key = forms.CharField(
        label="SSH Private Key",
        widget=forms.Textarea(attrs={"rows": 8, "class": f"font-mono text-xs {_INPUT}"}),
        required=False,
        help_text="Paste the full PEM-encoded private key (RSA or Ed25519).",
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(render_value=False, attrs={"class": _INPUT}),
        required=False,
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
        if auth_type == ExternalServer.AuthType.SSH_KEY and not cleaned.get("private_key"):
            self.add_error("private_key", "SSH private key is required for key-based auth.")
        if auth_type == ExternalServer.AuthType.PASSWORD and not cleaned.get("password"):
            self.add_error("password", "Password is required for password-based auth.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        pk = self.cleaned_data.get("private_key")
        pw = self.cleaned_data.get("password")
        if pk:
            instance._raw_private_key = pk
        if pw:
            instance._raw_password = pw
        if commit:
            instance.save()
        return instance


class CloudAccountForm(forms.ModelForm):
    """Form for adding a DigitalOcean cloud account."""

    api_token = forms.CharField(
        label="API Token",
        widget=forms.PasswordInput(render_value=False, attrs={"class": _INPUT}),
        help_text="Your DigitalOcean Personal Access Token (read + write).",
    )

    class Meta:
        model = CloudAccount
        fields = ["name", "provider"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _INPUT}),
            "provider": forms.Select(attrs={"class": _SELECT}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance._raw_api_token = self.cleaned_data["api_token"]
        if commit:
            instance.save()
        return instance


class ProvisionDropletForm(forms.ModelForm):
    """Form for provisioning a new DigitalOcean Droplet."""

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
        if organization:
            self.fields["cloud_account"].queryset = CloudAccount.objects.filter(
                organization=organization, is_verified=True
            )
        self.fields["cloud_account"].label = "Cloud Account (DO)"
        self.fields["region"].widget.attrs["class"] = _SELECT
        self.fields["size"].widget.attrs["class"] = _SELECT
