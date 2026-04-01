from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    class Action(models.TextChoices):
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        REGISTER = "register", "Register"
        EMAIL_VERIFY = "email_verify", "Email Verified"
        PASSWORD_CHANGE = "password_change", "Password Changed"
        PROFILE_UPDATE = "profile_update", "Profile Updated"
        USER_CREATE = "user_create", "User Created"
        USER_UPDATE = "user_update", "User Updated"
        USER_DELETE = "user_delete", "User Deleted"
        ROLE_CHANGE = "role_change", "Role Changed"
        INVITE_SENT = "invite_sent", "Invite Sent"
        INVITE_ACCEPTED = "invite_accepted", "Invite Accepted"
        ORG_CREATED = "org_created", "Organization Created"
        LOGIN_FAILED = "login_failed", "Login Failed"
        # Cloud infrastructure actions
        SERVER_ADD = "server_add", "Server Added"
        SERVER_VERIFY = "server_verify", "Server Verified"
        SERVER_PREPARE = "server_prepare", "Server Prepared"
        CLOUD_ACCT_ADD = "cloud_acct_add", "Cloud Account Added"
        CLOUD_ACCT_VERIFY = "cloud_acct_verify", "Cloud Account Verified"
        DROPLET_PROVISION = "droplet_provision", "Droplet Provisioned"
        DROPLET_DESTROY = "droplet_destroy", "Droplet Destroyed"
        # Odoo instance maintenance actions
        ODOO_UPDATE_MODULES = "odoo_update_modules", "Odoo Update Modules"
        ODOO_RESTART_INSTANCE = "odoo_restart_instance", "Odoo Restart Instance"
        # VCS / social auth actions
        SOCIAL_LOGIN = "social_login", "Social Login"
        VCS_CONNECT = "vcs_connect", "VCS Account Connected"
        VCS_DISCONNECT = "vcs_disconnect", "VCS Account Disconnected"
        OTHER = "other", "Other"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=50, choices=Action.choices)
    description = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["-timestamp"]),
            models.Index(fields=["organization", "-timestamp"]),
            models.Index(fields=["user", "-timestamp"]),
            models.Index(fields=["action"]),
        ]

    def __str__(self):
        who = self.user.email if self.user else "Anonymous"
        org = self.organization.name if self.organization else "—"
        return f"{who} [{org}] – {self.action} @ {self.timestamp:%Y-%m-%d %H:%M}"
