from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

User = get_user_model()


class OrgSignupForm(forms.Form):
    """One-shot form: creates User + Organization + SUPER_ADMIN membership."""

    # User fields
    first_name = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs={"placeholder": "First Name"})
    )
    last_name = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs={"placeholder": "Last Name"})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "you@company.com"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password"}),
        validators=[validate_password],
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm Password"}),
        label="Confirm Password",
    )

    # Organization field
    org_name = forms.CharField(
        max_length=255,
        label="Organization Name",
        widget=forms.TextInput(attrs={"placeholder": "Acme Corp"}),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            raise forms.ValidationError({"password_confirm": "Passwords do not match."})
        return cleaned


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]


class InviteAcceptForm(forms.Form):
    """Used when an invited user doesn't have an account yet."""
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    password = forms.CharField(
        widget=forms.PasswordInput(), validators=[validate_password]
    )
    password_confirm = forms.CharField(widget=forms.PasswordInput())

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            raise forms.ValidationError({"password_confirm": "Passwords do not match."})
        return cleaned
