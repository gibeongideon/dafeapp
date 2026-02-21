from rest_framework import serializers

from .models import Organization, OrganizationMembership


class OrganizationSerializer(serializers.ModelSerializer):
    member_count = serializers.ReadOnlyField()

    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "is_active", "created_at", "member_count"]
        read_only_fields = ["id", "slug", "created_at"]


class MembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.ReadOnlyField(source="user.email")
    user_name = serializers.ReadOnlyField(source="user.display_name")

    class Meta:
        model = OrganizationMembership
        fields = ["id", "user_email", "user_name", "role", "is_active", "joined_at"]
        read_only_fields = ["id", "user_email", "user_name", "joined_at"]
