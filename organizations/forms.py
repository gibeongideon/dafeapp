from django import forms

from .models import Organization, OrganizationInvite, OrganizationMembership


class CreateOrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={
                "placeholder": "My Company",
                "class": "w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500",
                "autofocus": True,
            })
        }


class InviteUserForm(forms.ModelForm):
    class Meta:
        model = OrganizationInvite
        fields = ["first_name", "email", "role"]
        widgets = {
            "first_name": forms.TextInput(attrs={"placeholder": "Jane (optional)"}),
            "email": forms.EmailInput(attrs={"placeholder": "colleague@company.com"}),
        }

    def __init__(self, *args, current_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = False
        # ADMINs can only invite USERs and MANAGERs, not SUPER_ADMINs
        if current_role == "ADMIN":
            self.fields["role"].choices = [
                (r, l)
                for r, l in OrganizationMembership.Role.choices
                if r not in ("SUPER_ADMIN",)
            ]


class MemberRoleForm(forms.ModelForm):
    class Meta:
        model = OrganizationMembership
        fields = ["role"]
