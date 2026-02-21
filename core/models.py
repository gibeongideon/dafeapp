from django.db import models


class OrganizationScopedModel(models.Model):
    """
    Abstract base for every model that belongs to an Organization.
    Always filter querysets with .filter(organization=request.organization).
    Never use .all() on subclasses — that leaks cross-org data.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        db_index=True,
    )

    class Meta:
        abstract = True


class OrganizationScopedManager(models.Manager):
    """Manager mixin to automatically scope queries to an organization."""

    def for_org(self, organization):
        return self.get_queryset().filter(organization=organization)
