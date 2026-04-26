import logging
import csv
import json
import re
import shlex
import hashlib
import hmac
import tarfile
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlparse

import requests
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from backups.models import OdooInstanceBackup
from cloud.forms import PyOSSSHSettingsForm
from cloud.models import CloudAccount, PyOSSSHSettings
from cloud.providers import get_provider
from cloud.pyos import looks_like_public_key_text
from dns.models import DomainAssignment, DnsZone, normalize_domain_name
from deployments.models import (
    DeploymentJob,
    DockerCleanupRun,
    EnterpriseSource,
    GitHubWebhookEvent,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    ServerSSHKey,
    StagingEnvironment,
    TerraformRun,
)
from deployments.domain_utils import (
    build_platform_domain_label,
    is_platform_domain_label_valid,
    normalize_platform_domain_label,
    platform_base_domain,
    platform_domain_for_label,
    platform_domains_enabled,
    platform_dns_default_proxied,
    platform_dns_is_configured,
    platform_dns_provider_service,
)
from deployments.serializers import (
    DeploymentJobSerializer,
    DockerCleanupRunSerializer,
    EnterpriseSourceSerializer,
    GitHubWebhookEventSerializer,
    GitRepositoryCredentialSerializer,
    InfrastructureSerializer,
    InstanceSerializer,
    OdooInstanceGitRepoSerializer,
    OdooInstanceHistorySerializer,
    OdooInstanceSerializer,
    OdooServerHistorySerializer,
    OdooServerSerializer,
    StagingEnvironmentSerializer,
    TerraformRunSerializer,
)
from deployments.tasks import (
    activate_enterprise_for_instance,
    checkout_instance_repo_branch,
    cleanup_deleted_instance,
    clone_instance_repo,
    configure_docker_host,
    configure_odoo_server,
    create_odoo_instance,
    detach_instance_domain,
    delete_odoo_instance,
    delete_odoo_server,
    deploy_server_ssh_key,
    provision_instance_domain,
    provision_odoo_server,
    refresh_instance_addons,
    remove_instance_repo,
    swap_instance_repo,
    create_staging_instance,
    restart_odoo_instance,
    stop_odoo_instance,
    rollback_odoo_instance,
    rollback_instance_repo,
    sync_instance_repo_status,
    terraform_apply_instance,
    update_instance_modules_all,
    update_instance_repo,
    collect_docker_cleanup_preview,
    execute_docker_cleanup,
    _docker_cleanup_age_days,
    _docker_cleanup_labels,
    _format_bytes_human,
    _normalized_docker_cleanup_types,
)
from subscriptions.exceptions import SubscriptionError, SubscriptionLimitError
from subscriptions.services import SubscriptionEnforcer
from users.models import VCSAccount

logger = logging.getLogger(__name__)


def _dispatch(task, *args):
    """Try async Celery dispatch; fall back to synchronous execution in dev."""
    try:
        task.delay(*args)
    except Exception:
        logger.warning("Celery broker unavailable; running task synchronously.", exc_info=True)
        task(*args)


def _timeline_actor_label(user) -> str:
    if not user:
        return "System"
    full_name = user.get_full_name().strip()
    return full_name or getattr(user, "email", "") or getattr(user, "username", "") or "System"


def _timeline_last_log_line(log: str) -> str:
    for line in reversed((log or "").splitlines()):
        line = line.strip()
        if line:
            return line[:220]
    return ""


def _timeline_job_title(job: DeploymentJob) -> str:
    labels = {
        DeploymentJob.JobType.RESTORE_INSTANCE: "Restore",
        DeploymentJob.JobType.ROLLBACK_INSTANCE: "Rollback",
        DeploymentJob.JobType.UPDATE_INSTANCE_REPO: "GitHub Sync",
        DeploymentJob.JobType.ROLLBACK_INSTANCE_REPO: "Repo Rollback",
        DeploymentJob.JobType.CHECKOUT_INSTANCE_REPO_BRANCH: "Branch Switch",
        DeploymentJob.JobType.CLONE_INSTANCE_REPO: "Repo Clone",
        DeploymentJob.JobType.AUTO_SYNC_INSTANCE_REPOS: "GitHub Auto Sync",
        DeploymentJob.JobType.BACKUP_INSTANCE: "Backup Job",
    }
    return labels.get(job.job_type, job.get_job_type_display())


def _request_data(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


def _docker_cleanup_row_payload(run: DockerCleanupRun) -> dict:
    data = DockerCleanupRunSerializer(run).data
    labels = _docker_cleanup_labels(run.cleanup_types or [])
    data["cleanup_types_label"] = ", ".join(labels)
    data["cleanup_types_summary"] = f"{len(labels)} cleanup type{'s' if len(labels) != 1 else ''}"
    data["space_freed_display"] = _format_bytes_human(run.space_freed_bytes)
    data["duration_display"] = f"{run.duration_seconds}s" if run.duration_seconds else "-"
    return data


def _get_docker_cleanup_server(request, server_id):
    org = getattr(request, "organization", None)
    if not org:
        return None, JsonResponse({"error": "No active organization."}, status=400)
    if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
        return None, JsonResponse({"error": "Permission denied."}, status=403)
    server = get_object_or_404(
        OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
        pk=server_id,
        organization=org,
        is_active=True,
    )
    if server.deployment_mode != OdooServer.DeploymentMode.DOCKER:
        return None, JsonResponse({"error": "Docker cleanup is only available for Docker servers."}, status=400)
    return server, None


def _enterprise_archive_root(*, scope: str = EnterpriseSource.Scope.PLATFORM, owner_id: int | None = None) -> Path:
    base_root = Path(getattr(settings, "ODOO_ENTERPRISE_ARCHIVE_ROOT", Path(settings.BASE_DIR) / "var" / "enterprise" / "archives"))
    if scope == EnterpriseSource.Scope.USER:
        if owner_id is None:
            raise ValueError("User Enterprise storage requires an owner.")
        return base_root / "users" / str(owner_id)
    return base_root


def _enterprise_extract_root(*, scope: str = EnterpriseSource.Scope.PLATFORM, owner_id: int | None = None) -> Path:
    base_root = Path(getattr(settings, "ODOO_ENTERPRISE_EXTRACT_ROOT", Path(settings.BASE_DIR) / "var" / "enterprise" / "sources"))
    if scope == EnterpriseSource.Scope.USER:
        if owner_id is None:
            raise ValueError("User Enterprise storage requires an owner.")
        return base_root / "users" / str(owner_id)
    return base_root


def _normalize_odoo_version(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _infer_odoo_version_from_filename(filename: str) -> str:
    text = str(filename or "").strip().lower()
    marker = "odoo_"
    if marker not in text:
        return ""
    tail = text.split(marker, 1)[1]
    digits = []
    for ch in tail:
        if ch.isdigit():
            digits.append(ch)
            continue
        if ch in {".", "_", "+", "-"} and digits:
            break
        if digits:
            break
    return "".join(digits)


def _infer_enterprise_release_code(filename: str) -> str:
    text = str(filename or "").strip().lower()
    match = re.search(r"odoo[_-]?(\d+)(?:\.0)?\+e\.(\d{8})\.tar(?:\.gz)?$", text)
    return match.group(2) if match else ""


def _enterprise_release_code_for_source(source: EnterpriseSource) -> str:
    return _infer_enterprise_release_code(source.archive_filename or source.package_name)


def _detect_enterprise_addons_source(extract_dir: Path) -> Path | None:
    candidates: list[tuple[int, int, Path]] = []
    for path in [extract_dir, *extract_dir.rglob("*")]:
        if not path.is_dir():
            continue
        manifest_count = len(list(path.glob("*/__manifest__.py"))) + len(list(path.glob("*/__openerp__.py")))
        if manifest_count > 0:
            depth = len(path.relative_to(extract_dir).parts)
            candidates.append((manifest_count, -depth, path))
        addons_dir = path / "addons"
        if addons_dir.is_dir():
            manifest_count = len(list(addons_dir.glob("*/__manifest__.py"))) + len(list(addons_dir.glob("*/__openerp__.py")))
            if manifest_count > 0:
                depth = len(addons_dir.relative_to(extract_dir).parts)
                candidates.append((manifest_count + 1, -depth, addons_dir))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _save_and_extract_enterprise_archive(*, archive_file, odoo_version: str, uploaded_by, scope: str = EnterpriseSource.Scope.PLATFORM) -> EnterpriseSource:
    odoo_version = _normalize_odoo_version(odoo_version) or _infer_odoo_version_from_filename(archive_file.name)
    if not odoo_version:
        raise ValueError("Could not determine the Odoo version for this Enterprise archive.")
    release_code = _infer_enterprise_release_code(archive_file.name)
    if not release_code:
        raise ValueError("The Enterprise archive name must include a release date code like odoo_19.0+e.20260327.tar.gz.")

    owner = uploaded_by if scope == EnterpriseSource.Scope.USER else None
    archive_root = _enterprise_archive_root(scope=scope, owner_id=owner.id if owner else None) / odoo_version
    extract_root = _enterprise_extract_root(scope=scope, owner_id=owner.id if owner else None) / odoo_version
    archive_root.mkdir(parents=True, exist_ok=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    existing_sources = list(
        EnterpriseSource.objects.filter(
            odoo_version=odoo_version,
            source_scope=scope,
            owner=owner,
        ).order_by("-is_active", "-updated_at", "-id")
    )
    existing_release_codes = [_enterprise_release_code_for_source(source) for source in existing_sources]
    newest_existing_code = max((code for code in existing_release_codes if code), default="")
    if newest_existing_code and release_code < newest_existing_code:
        raise ValueError(
            f"A newer Enterprise source already exists for Odoo {odoo_version} ({newest_existing_code}). "
            "Upload the latest release only."
        )

    original_name = Path(archive_file.name).name
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
    staged_root = Path(tempfile.mkdtemp(prefix=f"enterprise-{odoo_version}-"))
    staged_archive_path = staged_root / f"{timestamp}-{original_name}"
    staged_extract_dir = staged_root / staged_archive_path.name.removesuffix(".tar.gz").removesuffix(".tgz")
    with staged_archive_path.open("wb") as handle:
        for chunk in archive_file.chunks():
            handle.write(chunk)

    staged_extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(staged_archive_path, "r:gz") as tar:
            tar.extractall(staged_extract_dir)
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(staged_root, ignore_errors=True)
        raise ValueError("The uploaded Enterprise package must be a valid .tar.gz archive.") from exc

    addons_source = _detect_enterprise_addons_source(staged_extract_dir)
    if addons_source is None:
        shutil.rmtree(staged_root, ignore_errors=True)
        raise ValueError("Could not detect an Enterprise addons directory inside the uploaded archive.")
    addons_relative = addons_source.relative_to(staged_extract_dir)

    shutil.rmtree(archive_root, ignore_errors=True)
    shutil.rmtree(extract_root, ignore_errors=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    archive_path = archive_root / staged_archive_path.name
    extract_dir = extract_root / staged_extract_dir.name
    shutil.move(str(staged_archive_path), archive_path)
    shutil.move(str(staged_extract_dir), extract_dir)
    shutil.rmtree(staged_root, ignore_errors=True)

    source = existing_sources[0] if existing_sources else EnterpriseSource(odoo_version=odoo_version, source_scope=scope, owner=owner)
    extra_source_ids = [row.id for row in existing_sources[1:]]
    with transaction.atomic():
        if extra_source_ids:
            OdooInstance.objects.filter(enterprise_source_id__in=extra_source_ids).update(enterprise_source=source)
            EnterpriseSource.objects.filter(id__in=extra_source_ids).delete()
        if scope == EnterpriseSource.Scope.PLATFORM:
            EnterpriseSource.objects.filter(
                odoo_version=odoo_version,
                source_scope=scope,
                owner=owner,
                is_active=True,
            ).exclude(pk=source.pk).update(is_active=False)
        source.package_name = archive_path.stem
        source.release_code = release_code
        source.archive_filename = original_name
        source.archive_path = str(archive_path)
        source.extract_path = str(extract_dir)
        source.addons_source_path = str(extract_dir / addons_relative)
        source.is_active = True
        source.status = EnterpriseSource.Status.READY
        source.last_error = ""
        source.uploaded_by = uploaded_by
        source.owner = owner
        source.source_scope = scope
        source.save()
    return source


def _enterprise_source_for_instance(*, instance: OdooInstance, user, source_mode: str | None = None) -> EnterpriseSource | None:
    mode = (source_mode or instance.enterprise_source_mode or OdooInstance.EnterpriseSourceMode.PLATFORM).strip().upper()
    if mode == OdooInstance.EnterpriseSourceMode.USER:
        return EnterpriseSource.latest_user_for_version(user, instance.server.odoo_version)
    return EnterpriseSource.active_for_version(instance.server.odoo_version, scope=EnterpriseSource.Scope.PLATFORM)


def _repo_permission_denied(request):
    return request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER")


def _parse_bool(value, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _default_tls_mode() -> str:
    value = getattr(settings, "TRAEFIK_DEFAULT_TLS_MODE", OdooServer.TLSMode.LETS_ENCRYPT)
    valid = {choice for choice, _ in OdooServer.TLSMode.choices}
    return value if value in valid else OdooServer.TLSMode.LETS_ENCRYPT


def _instance_runtime_log_command(instance: OdooInstance) -> tuple[str, str]:
    server = instance.server
    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        container_name = (instance.container_name or f"odoo-{instance.db_name.replace('_', '-')}").strip()
        return (
            "docker",
            f"""
container={shlex.quote(container_name)}
if docker ps -a --format '{{{{.Names}}}}' | grep -Fx -- "$container" >/dev/null 2>&1; then
  docker logs --tail 120 "$container" 2>&1 | tail -n 120
  exit 0
fi
echo "Waiting for container logs..."
""".strip(),
        )

    service_name = (instance.systemd_service or f"odoo-{instance.db_name}").strip()
    log_paths = []
    summary_match = re.search(r"^\s*Logs\s*:\s*(.+)$", instance.installation_summary_text or "", flags=re.MULTILINE)
    if summary_match:
        log_paths.append(summary_match.group(1).strip())
    log_paths.extend(
        [
            f"/opt/odoo{server.odoo_version}/logs/{instance.db_name}.log",
            f"/odoo/instances/{instance.db_name}/logs/{instance.db_name}.log",
        ]
    )
    unique_log_paths = []
    for path in log_paths:
        normalized = str(path or "").strip()
        if normalized and normalized not in unique_log_paths:
            unique_log_paths.append(normalized)

    fallback_reads = "\n".join(
        f"""
if [ -f {shlex.quote(path)} ]; then
  tail -n 120 {shlex.quote(path)} 2>&1
  exit 0
fi
""".strip()
        for path in unique_log_paths
    )
    return (
        "systemd",
        f"""
service={shlex.quote(service_name)}
journal_output=$(journalctl -u "$service" -n 120 --no-pager -o short-iso 2>&1 | tail -n 120)
if [ -n "$journal_output" ] && ! printf '%s' "$journal_output" | grep -qiE 'No entries|-- No entries --|Unit .* could not be found'; then
  printf '%s\n' "$journal_output"
  exit 0
fi
{fallback_reads}
echo "No runtime logs found for $service."
""".strip(),
    )


def _server_mutation_lock_reason(server: OdooServer) -> str:
    if server.status in (
        OdooServer.Status.CONNECTING,
        OdooServer.Status.PROVISIONING,
        OdooServer.Status.CONFIGURING,
    ):
        return "Server provisioning is still in progress."
    return ""


def _instance_mutation_lock_reason(instance: OdooInstance, *, include_jobs: bool = True) -> str:
    server_reason = _server_mutation_lock_reason(instance.server)
    if server_reason:
        return server_reason

    if instance.status in (OdooInstance.Status.PENDING, OdooInstance.Status.CONFIGURING):
        return "Instance provisioning is still in progress."

    if instance.domain_status == OdooInstance.DomainStatus.PENDING and not instance.domain:
        return "Domain provisioning is still in progress for this instance."

    if instance.enterprise_status == OdooInstance.EnterpriseStatus.PENDING:
        return "Enterprise activation is still in progress for this instance."

    if instance.addons_sync_status == OdooInstance.AddonsSyncStatus.PENDING:
        return "Addons synchronization is still in progress for this instance."

    if include_jobs and instance.jobs.filter(
        status__in=[DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING]
    ).exists():
        return "Another deployment job is still running for this instance."

    return ""


def _resolve_managed_dns_zone(org, zone_id):
    if not zone_id:
        return None
    return get_object_or_404(DnsZone.objects.select_related("provider_account"), pk=zone_id, organization=org)


def _active_assignment_for_instance(instance: OdooInstance):
    return instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).order_by("-is_primary", "-created_at", "-id").first()

def _domain_in_use(_org, domain: str, *, exclude_instance_id: int | None = None) -> bool:
    normalized = normalize_domain_name(domain)
    if not normalized:
        return False

    assignments = DomainAssignment.objects.filter(
        domain=normalized,
    ).exclude(status=DomainAssignment.Status.DELETED)
    if exclude_instance_id is not None:
        assignments = assignments.exclude(instance_id=exclude_instance_id)
    if assignments.exists():
        return True

    instances = OdooInstance.objects.filter(
        domain=normalized,
    ).exclude(status=OdooInstance.Status.DELETED)
    if exclude_instance_id is not None:
        instances = instances.exclude(pk=exclude_instance_id)
    return instances.exists()


def _generate_platform_domain(_instance_name: str = "") -> str:
    if not platform_domains_enabled():
        return ""
    for attempt in range(0, 200):
        label = build_platform_domain_label("", attempt)
        domain = platform_domain_for_label(label)
        if domain and not _domain_in_use(None, domain):
            return domain
    fallback = build_platform_domain_label("", 999)
    return platform_domain_for_label(fallback)


def _sync_instance_domain_assignment(
    instance: OdooInstance,
    domain: str,
    *,
    source: str = DomainAssignment.Source.CUSTOM,
    is_primary: bool = False,
) -> DomainAssignment:
    normalized = normalize_domain_name(domain)
    preferred_zone = instance.server.managed_dns_zone if instance.server_id and source == DomainAssignment.Source.CUSTOM else None
    zone = DnsZone.match_for_domain(instance.organization, normalized, preferred_zone=preferred_zone)

    assignment = instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).filter(domain=normalized).first()
    if assignment is None and is_primary:
        assignment = instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).filter(is_primary=True).first()
    if assignment and assignment.domain != normalized:
        assignment.status = DomainAssignment.Status.DELETED
        assignment.instance = None
        assignment.last_error = ""
        assignment.last_synced_at = timezone.now()
        assignment.save(update_fields=["status", "instance", "last_error", "last_synced_at", "updated_at"])
        assignment = None

    if assignment is None:
        assignment = DomainAssignment(
            organization=instance.organization,
            instance=instance,
        )

    assignment.zone = zone
    assignment.domain = normalized
    assignment.source = source
    assignment.is_primary = is_primary
    assignment.hostname = zone.hostname_for_domain(normalized) if zone else normalized
    assignment.proxied = bool(zone.default_proxied) if zone else False
    assignment.is_managed = bool(
        zone
        and instance.server.managed_dns_enabled
        and (instance.server.managed_dns_zone_id is None or instance.server.managed_dns_zone_id == zone.id)
    )
    if source == DomainAssignment.Source.PLATFORM:
        assignment.zone = None
        assignment.hostname = normalized
        assignment.proxied = False
        assignment.is_managed = True
    assignment.status = DomainAssignment.Status.PENDING
    assignment.last_error = ""
    assignment.instance = instance
    assignment.save()

    if is_primary:
        instance.domain_assignments.exclude(pk=assignment.pk).filter(status__in=[
            DomainAssignment.Status.PENDING,
            DomainAssignment.Status.ACTIVE,
            DomainAssignment.Status.FAILED,
        ]).update(is_primary=False)
    return assignment


def _repo_job(org, *, job_type: str, instance: OdooInstance, user):
    return DeploymentJob.objects.create(
        organization=org,
        job_type=job_type,
        odoo_instance=instance,
        created_by=user,
    )


def _credential_for_github_publish_actor(*, org, user, actor, payload, repo_name: str):
    if actor.auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
        return _ensure_github_oauth_credential(
            org=org,
            user=user,
            account=actor.account,
        )
    if getattr(actor, "credential", None) is not None:
        return actor.credential
    token_payload = {
        "credential_name": payload.get("credential_name")
        or f"github-upload-{repo_name}-{timezone.now().strftime('%Y%m%d%H%M%S')}",
        "git_username": payload.get("git_username") or actor.username or "oauth2",
        "access_token": actor.access_token,
    }
    return _resolve_git_credential(
        org=org,
        user=user,
        payload=token_payload,
        auth_type=OdooInstanceGitRepo.AuthType.TOKEN,
    )


def _register_instance_github_repo(
    *,
    org,
    user,
    instance,
    repo_name: str,
    git_url: str,
    branch: str,
    credential,
    auth_type: str,
):
    existing = instance.git_repos.filter(repo_name=repo_name).first()
    if existing:
        return existing, False

    repo = OdooInstanceGitRepo.objects.create(
        instance=instance,
        credential=credential,
        repo_name=repo_name,
        git_url=git_url,
        branch=branch,
        auth_type=auth_type,
        local_path=_build_repo_local_path(instance, repo_name),
        display_order=instance.git_repos.count(),
        default_branch=branch,
        status=OdooInstanceGitRepo.Status.DISCONNECTED,
        last_error="Repository created on GitHub. Upload a zip or sync content to finish linking it to this instance.",
        created_by=user,
    )
    return repo, True


def _derive_repo_name(repo_name: str, git_url: str) -> str:
    repo_name = (repo_name or "").strip()
    if repo_name:
        return repo_name
    parsed = urlparse(git_url)
    tail = parsed.path.rsplit("/", 1)[-1] if parsed.path else git_url.rsplit("/", 1)[-1]
    tail = tail.removesuffix(".git")
    return tail or "repo"


def _build_repo_local_path(instance: OdooInstance, repo_name: str) -> str:
    base = instance.addons_root_path or f"/odoo/instances/{instance.db_name}/addons"
    slug = "".join(ch.lower() if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in repo_name).strip("-._")
    slug = slug or f"repo-{instance.id}"
    return f"{base.rstrip('/')}/{slug}"


def _active_github_account(*, user, account_id=None):
    qs = VCSAccount.objects.filter(
        user=user,
        provider=VCSAccount.Provider.GITHUB,
        is_active=True,
    )
    if account_id:
        return get_object_or_404(qs, pk=account_id)
    account = qs.order_by("id").first()
    if not account:
        raise ValueError("Connect a GitHub account first.")
    return account


def _ensure_github_oauth_credential(*, org, user, account):
    credential = (
        GitRepositoryCredential.objects.filter(
            organization=org,
            auth_type=GitRepositoryCredential.AuthType.GITHUB_OAUTH,
            github_account=account,
        )
        .order_by("id")
        .first()
    )
    if credential:
        if credential.git_username != (account.username or credential.git_username):
            credential.git_username = account.username or credential.git_username
            credential.save(update_fields=["git_username", "updated_at"])
        return credential

    base_name = f"github-{account.username or account.id}"
    name = base_name
    suffix = 2
    while GitRepositoryCredential.objects.filter(organization=org, name=name).exists():
        name = f"{base_name}-{suffix}"
        suffix += 1

    return GitRepositoryCredential.objects.create(
        organization=org,
        name=name,
        auth_type=GitRepositoryCredential.AuthType.GITHUB_OAUTH,
        github_account=account,
        git_username=account.username or "",
        created_by=user,
    )


def _create_instance_repo_and_dispatch(
    *,
    org,
    user,
    instance,
    repo_name: str,
    git_url: str,
    branch: str,
    auth_type: str,
    credential,
    auto_update: bool = False,
    install_requirements_on_update: bool = False,
    auto_upgrade_modules_on_update: bool = True,
    is_enabled: bool = True,
    display_order=None,
):
    repo_name = _derive_repo_name(repo_name, git_url)

    # Auto-replace: collect any existing linked repo so we can swap it out
    existing_repos = list(instance.git_repos.all())
    old_repo = existing_repos[0] if existing_repos else None

    if display_order in ("", None):
        display_order = 0

    repo = OdooInstanceGitRepo.objects.create(
        instance=instance,
        credential=credential,
        repo_name=repo_name,
        git_url=git_url,
        branch=(branch or "main").strip() or "main",
        auth_type=auth_type,
        local_path=_build_repo_local_path(instance, repo_name),
        auto_update=auto_update,
        install_requirements_on_update=install_requirements_on_update,
        auto_upgrade_modules_on_update=auto_upgrade_modules_on_update,
        is_enabled=is_enabled,
        display_order=int(display_order or 0),
        default_branch=(branch or "main").strip() or "main",
        created_by=user,
    )

    if old_repo is not None:
        # Swap: remove old repo dir + DB record, then clone new repo — all under one lock
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.CLONE_INSTANCE_REPO,
            instance=instance,
            user=user,
        )
        _dispatch(swap_instance_repo, old_repo.id, repo.id, job.id)
    else:
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.CLONE_INSTANCE_REPO,
            instance=instance,
            user=user,
        )
        _dispatch(clone_instance_repo, repo.id, job.id)

    return repo, job


def _github_api_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _friendly_github_access_error(detail: str, response=None) -> str:
    message = (detail or "").strip()
    oauth_scopes = ""
    accepted_scopes = ""
    status_code = None
    if response is not None:
        oauth_scopes = (response.headers.get("X-OAuth-Scopes") or "").strip()
        accepted_scopes = (response.headers.get("X-Accepted-OAuth-Scopes") or "").strip()
        status_code = response.status_code

    if "Resource not accessible by integration" in message:
        return (
            "GitHub denied repository creation for this connection. Disconnect and reconnect GitHub "
            "from Connections so DafeApp gets repository write access, then try again."
        )

    if status_code == 403 and oauth_scopes and "repo" not in {scope.strip() for scope in oauth_scopes.split(",") if scope.strip()}:
        accepted_note = f" GitHub expected scopes like: {accepted_scopes}." if accepted_scopes else ""
        return (
            "GitHub denied repository creation because this connection does not include the `repo` scope."
            f"{accepted_note} Disconnect and reconnect GitHub from Connections, approve repository access, and try again."
        )

    return message


def _is_github_publish_permission_error(message: str) -> bool:
    text = (message or "").strip().lower()
    return (
        "resource not accessible by integration" in text
        or "denied repository creation for this connection" in text
        or "does not include the `repo` scope" in text
    )


def _zip_extract_root(extract_dir: Path) -> Path:
    candidates = [path for path in extract_dir.iterdir() if path.name != "__MACOSX"]
    if len(candidates) == 1 and candidates[0].is_dir():
        return candidates[0]
    return extract_dir


def _run_local_git(args, *, cwd: Path):
    result = subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        raise RuntimeError(output.strip() or f"Git command failed: {' '.join(args)}")
    return result


def _github_full_name_from_git_url(git_url: str) -> str:
    text = (git_url or "").strip()
    if not text:
        return ""

    if text.startswith("git@github.com:"):
        path = text.split("git@github.com:", 1)[1]
    else:
        parsed = urlparse(text)
        if (parsed.hostname or "").strip().lower() != "github.com":
            return ""
        path = parsed.path.lstrip("/")

    parts = [segment for segment in path.removesuffix(".git").split("/") if segment]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}"


def _github_clone_url_for_full_name(full_name: str) -> str:
    normalized = (full_name or "").strip().strip("/")
    return f"https://github.com/{normalized}.git" if normalized else ""


def _github_branch_from_ref(ref: str) -> str:
    text = (ref or "").strip()
    prefix = "refs/heads/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _github_webhook_signature_valid(request) -> bool:
    secret = str(getattr(settings, "GITHUB_WEBHOOK_SECRET", "") or "").strip()
    if not secret:
        return True
    signature = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), request.body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _github_webhook_target_url(request) -> str:
    return request.build_absolute_uri(reverse("deployments:github-webhook"))


def _github_webhook_actor_from_repo(repo: OdooInstanceGitRepo):
    credential = repo.credential
    if repo.auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
        if credential is None or not credential.github_account_id or not credential.github_account.is_active:
            raise ValueError("Connect an active GitHub account to enable instant auto updates.")
        return SimpleNamespace(access_token=credential.access_token)
    if repo.auth_type == OdooInstanceGitRepo.AuthType.TOKEN:
        if credential is None or not credential.access_token:
            raise ValueError("Save a personal access token to enable instant auto updates.")
        return SimpleNamespace(access_token=credential.access_token)
    raise ValueError("Instant auto updates require GitHub OAuth or a personal access token.")


def _ensure_github_push_webhook(*, repo: OdooInstanceGitRepo, request):
    full_name = _github_full_name_from_git_url(repo.git_url)
    if not full_name or not repo.auto_update:
        return

    actor = _github_webhook_actor_from_repo(repo)
    target_url = _github_webhook_target_url(request)
    hooks_url = f"https://api.github.com/repos/{full_name}/hooks"
    secret = str(getattr(settings, "GITHUB_WEBHOOK_SECRET", "") or "").strip()
    config = {
        "url": target_url,
        "content_type": "json",
        "insecure_ssl": "0",
    }
    if secret:
        config["secret"] = secret

    try:
        hooks_response = requests.get(
            hooks_url,
            headers=_github_api_headers(actor.access_token),
            timeout=20,
        )
        hooks_response.raise_for_status()
        existing_hook = next(
            (
                item
                for item in hooks_response.json()
                if ((item.get("config") or {}).get("url") or "").strip() == target_url
            ),
            None,
        )
        if existing_hook:
            hook_id = existing_hook.get("id")
            existing_events = sorted(existing_hook.get("events") or [])
            needs_patch = (not existing_hook.get("active", True)) or existing_events != ["push"]
            if needs_patch and hook_id:
                patch_response = requests.patch(
                    f"{hooks_url}/{hook_id}",
                    headers=_github_api_headers(actor.access_token),
                    json={"active": True, "events": ["push"], "config": config},
                    timeout=20,
                )
                patch_response.raise_for_status()
            return

        create_response = requests.post(
            hooks_url,
            headers=_github_api_headers(actor.access_token),
            json={
                "name": "web",
                "active": True,
                "events": ["push"],
                "config": config,
            },
            timeout=20,
        )
        create_response.raise_for_status()
    except requests.RequestException as exc:
        detail = ""
        if getattr(exc, "response", None) is not None:
            try:
                detail = (exc.response.json() or {}).get("message", "")
            except ValueError:
                detail = exc.response.text
        raise RuntimeError(detail or f"GitHub webhook setup failed: {exc}") from exc


def _github_publish_actor_from_linked_repo(linked_repo: OdooInstanceGitRepo):
    credential = linked_repo.credential
    if linked_repo.auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
        if credential is None or not credential.github_account_id or not credential.github_account.is_active:
            raise ValueError("The linked GitHub repository is missing an active connected GitHub account.")
        return SimpleNamespace(
            auth_type=linked_repo.auth_type,
            access_token=credential.access_token,
            username=(credential.git_username or credential.github_account.username or "").strip(),
            account=credential.github_account,
            credential=credential,
        )

    if linked_repo.auth_type == OdooInstanceGitRepo.AuthType.TOKEN:
        if credential is None:
            raise ValueError("The linked GitHub repository is missing its saved personal access token credential.")
        return SimpleNamespace(
            auth_type=linked_repo.auth_type,
            access_token=credential.access_token,
            username=(credential.git_username or "").strip(),
            account=None,
            credential=credential,
        )

    raise ValueError("Only GitHub OAuth and personal access token repositories support zip publishing.")


def _github_publish_actor_from_payload(*, user, payload):
    auth_type = (payload.get("auth_type") or payload.get("upload_auth_type") or OdooInstanceGitRepo.AuthType.GITHUB_OAUTH).strip()
    if auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
        account = _active_github_account(
            user=user,
            account_id=payload.get("github_account_id"),
        )
        return SimpleNamespace(
            auth_type=auth_type,
            access_token=account.access_token,
            username=account.username or "",
            account=account,
        )

    if auth_type == OdooInstanceGitRepo.AuthType.TOKEN:
        access_token = (payload.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("access_token is required for personal access token auth.")
        return SimpleNamespace(
            auth_type=auth_type,
            access_token=access_token,
            username=(payload.get("git_username") or "").strip(),
            account=None,
        )

    raise ValueError("Unsupported auth_type for GitHub publishing.")


def _latest_saved_pat_credential_for_username(org, username: str):
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    return (
        GitRepositoryCredential.objects.filter(
            organization=org,
            auth_type=GitRepositoryCredential.AuthType.TOKEN,
            git_username__iexact=normalized,
        )
        .order_by("-last_used_at", "-updated_at", "-id")
        .first()
    )


def _github_publish_actor_from_token_credential(credential: GitRepositoryCredential):
    return SimpleNamespace(
        auth_type=OdooInstanceGitRepo.AuthType.TOKEN,
        access_token=credential.access_token,
        username=(credential.git_username or "").strip(),
        account=None,
        credential=credential,
    )


def _run_github_publish_with_saved_pat_fallback(org, actor, operation):
    try:
        return operation(actor), actor
    except RuntimeError as exc:
        if actor.auth_type != OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
            raise
        if not _is_github_publish_permission_error(str(exc)):
            raise

        fallback_credential = _latest_saved_pat_credential_for_username(org, actor.username)
        if not fallback_credential:
            raise

        fallback_actor = _github_publish_actor_from_token_credential(fallback_credential)
        logger.info(
            "GitHub OAuth publish fell back to saved PAT credential %s for username %s.",
            fallback_credential.id,
            fallback_actor.username,
        )
        return operation(fallback_actor), fallback_actor


def _create_github_repository(*, actor, repo_name: str, private: bool = True):
    try:
        create_response = requests.post(
            "https://api.github.com/user/repos",
            headers=_github_api_headers(actor.access_token),
            json={
                "name": repo_name,
                "private": private,
                "auto_init": False,
            },
            timeout=20,
        )
        create_response.raise_for_status()
    except requests.RequestException as exc:
        detail = ""
        if getattr(exc, "response", None) is not None:
            try:
                detail = (exc.response.json() or {}).get("message", "")
            except ValueError:
                detail = exc.response.text
        friendly_detail = _friendly_github_access_error(detail or f"GitHub repository creation failed: {exc}", getattr(exc, "response", None))
        raise RuntimeError(friendly_detail) from exc

    return create_response.json()


def _push_zip_to_github_repo(*, actor, user, full_name: str, zip_file, branch: str = "main"):
    with tempfile.TemporaryDirectory(prefix="dafeapp-github-upload-") as tmpdir:
        temp_dir = Path(tmpdir)
        archive_path = temp_dir / "upload.zip"
        with archive_path.open("wb") as handle:
            for chunk in zip_file.chunks():
                handle.write(chunk)

        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(temp_dir / "src")
        except zipfile.BadZipFile as exc:
            raise RuntimeError("The uploaded file must be a valid .zip archive.") from exc

        repo_root = _zip_extract_root(temp_dir / "src")
        if not repo_root.exists():
            raise RuntimeError("The uploaded zip archive is empty.")

        git_dir = repo_root / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)

        has_files = any(path.name != "__MACOSX" for path in repo_root.rglob("*"))
        if not has_files:
            raise RuntimeError("The uploaded zip archive does not contain any files to publish.")

        branch = (branch or "main").strip() or "main"
        git_username = (actor.username or "oauth2").strip() or "oauth2"
        remote_url = f"https://{quote(git_username, safe='')}:{quote(actor.access_token, safe='')}@github.com/{full_name}.git"
        git_author = actor.username or user.get_full_name() or user.email.split("@")[0]
        git_email = user.email or f"{git_author}@users.noreply.github.com"

        _run_local_git(["git", "init", "-b", branch], cwd=repo_root)
        _run_local_git(["git", "config", "user.name", git_author], cwd=repo_root)
        _run_local_git(["git", "config", "user.email", git_email], cwd=repo_root)
        _run_local_git(["git", "add", "."], cwd=repo_root)
        _run_local_git(["git", "commit", "-m", "Initial import from DafeApp"], cwd=repo_root)
        _run_local_git(["git", "remote", "add", "origin", remote_url], cwd=repo_root)
        _run_local_git(["git", "push", "-u", "origin", branch], cwd=repo_root)


def _resolve_git_credential(*, org, user, payload, auth_type: str):
    auth_type = (auth_type or OdooInstanceGitRepo.AuthType.PUBLIC).strip()
    if auth_type == OdooInstanceGitRepo.AuthType.PUBLIC:
        return None

    credential_id = payload.get("credential_id")
    if credential_id:
        credential = get_object_or_404(GitRepositoryCredential, pk=credential_id, organization=org)
        if credential.auth_type == GitRepositoryCredential.AuthType.GITHUB_OAUTH:
            if not credential.github_account_id or not credential.github_account.is_active:
                raise ValueError("The selected GitHub credential is not linked to an active VCSAccount.")
        return credential

    credential_name = (payload.get("credential_name") or "").strip()
    if not credential_name:
        credential_name = f"{auth_type.lower()}-{timezone.now().strftime('%Y%m%d%H%M%S')}"

    if auth_type == OdooInstanceGitRepo.AuthType.GITHUB_OAUTH:
        account_id = payload.get("github_account_id")
        if not account_id:
            raise ValueError("github_account_id is required for GitHub OAuth auth.")
        account = get_object_or_404(
            VCSAccount.objects.filter(user=user, provider=VCSAccount.Provider.GITHUB, is_active=True),
            pk=account_id,
        )
        credential, _ = GitRepositoryCredential.objects.get_or_create(
            organization=org,
            name=credential_name,
            defaults={
                "auth_type": GitRepositoryCredential.AuthType.GITHUB_OAUTH,
                "github_account": account,
                "created_by": user,
            },
        )
        if credential.github_account_id != account.id or credential.auth_type != GitRepositoryCredential.AuthType.GITHUB_OAUTH:
            credential.github_account = account
            credential.auth_type = GitRepositoryCredential.AuthType.GITHUB_OAUTH
            credential.git_username = account.username or credential.git_username
            credential.save(update_fields=["github_account", "auth_type", "git_username", "updated_at"])
        return credential

    if auth_type == OdooInstanceGitRepo.AuthType.TOKEN:
        access_token = (payload.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("access_token is required for token auth.")
        if GitRepositoryCredential.objects.filter(organization=org, name=credential_name).exists():
            raise ValueError("A credential with that name already exists.")
        credential = GitRepositoryCredential(
            organization=org,
            name=credential_name,
            auth_type=GitRepositoryCredential.AuthType.TOKEN,
            git_username=(payload.get("git_username") or "").strip(),
            created_by=user,
        )
        credential._raw_access_token = access_token
        credential.save()
        return credential

    if auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
        private_key = (payload.get("ssh_private_key") or "").strip()
        if not private_key:
            raise ValueError("ssh_private_key is required for SSH auth.")
        if GitRepositoryCredential.objects.filter(organization=org, name=credential_name).exists():
            raise ValueError("A credential with that name already exists.")
        credential = GitRepositoryCredential(
            organization=org,
            name=credential_name,
            auth_type=GitRepositoryCredential.AuthType.SSH_KEY,
            git_username=(payload.get("git_username") or "git").strip(),
            ssh_public_key=(payload.get("ssh_public_key") or "").strip(),
            created_by=user,
        )
        credential._raw_ssh_private_key = private_key
        credential._raw_ssh_key_passphrase = (payload.get("ssh_key_passphrase") or "").strip()
        credential.save()
        return credential

    raise ValueError("Unsupported auth_type.")


def _broadcast_server_event(server_id: int, payload: dict):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server_id}",
            {"type": "server.update", "payload": payload},
        )
    except Exception:
        logger.warning("Server broadcast skipped for server %s", server_id, exc_info=True)


def _broadcast_server_snapshot(server: OdooServer):
    """Push a full server snapshot to open websocket listeners."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server.id}",
            {
                "type": "server.update",
                "payload": {
                    "type": "snapshot",
                    "server": OdooServerSerializer(server).data,
                },
            },
        )
    except Exception:
        logger.warning("Server snapshot broadcast skipped for server %s", server.id, exc_info=True)


def _create_pyos_infrastructure(
    org,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str,
    ssh_key_path: str,
    created_by,
    *,
    is_verified: bool = False,
    is_reachable: bool = False,
    verification_error: str = "Reachability has not been verified yet.",
    checked_at=None,
):
    """Create the ExternalServer + Infrastructure records for a direct PYOS server."""
    from cloud.models import ExternalServer

    ext = ExternalServer(
        organization=org,
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        ssh_key_path=ssh_key_path.strip(),
        is_verified=is_verified,
        is_reachable=is_reachable,
        last_checked_at=checked_at,
        last_verified_at=checked_at,
        verification_error=verification_error,
    )
    if auth_type == "PASSWORD":
        ext._raw_password = password.strip()
    ext.save()

    infra_name = name
    if Infrastructure.objects.filter(organization=org, name=infra_name).exists():
        infra_name = f"{name}-{ext.id}"
    infra = Infrastructure.objects.create(
        organization=org,
        name=infra_name,
        infra_type=Infrastructure.InfraType.PYOS,
        external_server=ext,
        is_connected=True,
        validation_log="Created via inline deployment form.",
        created_by=created_by,
    )
    return infra, ext


def _validate_pyos_connection(
    *,
    org,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str,
    ssh_key_path: str,
) -> tuple[bool, str]:
    """
    Run the first PYOS SSH validation before creating any records so the modal
    can stay open on failure.
    """
    from cloud.models import ExternalServer
    from cloud.pyos import PyOSService

    candidate = ExternalServer(
        organization=org,
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        ssh_key_path=ssh_key_path.strip(),
    )
    if auth_type == "PASSWORD":
        from cloud.encryption import FieldEncryptor

        candidate.encrypted_password = FieldEncryptor.encrypt(password.strip())
    return PyOSService(candidate).validate()


def _active_instances_for_server(server: OdooServer):
    return server.instances.exclude(status=OdooInstance.Status.DELETED)


def _remote_used_ports(server: OdooServer) -> set[int]:
    if not server.ip_address:
        return set()

    from deployments.tasks import _ssh_run

    command = f"""
for port in $(seq {int(server.min_port)} {int(server.max_port)}); do
  if ss -ltn "( sport = :$port )" 2>/dev/null | tail -n +2 | grep -q .; then
    echo "$port"
  fi
done
""".strip()
    try:
        code, output = _ssh_run(server, command, timeout=60)
    except Exception:
        logger.warning("Remote port scan failed for server %s", server.id, exc_info=True)
        return set()

    if code != 0:
        logger.warning("Remote port scan returned %s for server %s: %s", code, server.id, output)
        return set()

    used: set[int] = set()
    for line in (output or "").splitlines():
        candidate = (line or "").strip()
        if candidate.isdigit():
            used.add(int(candidate))
    return used


def _next_available_port(server: OdooServer) -> int | None:
    used = set(_active_instances_for_server(server).values_list("http_port", flat=True))
    used.update(_remote_used_ports(server))
    for port in range(server.min_port, server.max_port + 1):
        if port not in used:
            return port
    return None


def _capacity_check(server: OdooServer, cpu: int, ram_mb: int) -> tuple[bool, str]:
    active = _active_instances_for_server(server)
    count = active.count()
    if count >= server.max_instances:
        return False, f"Max instances per server reached ({count}/{server.max_instances})."
    used_cpu = sum(active.values_list("requested_cpu_cores", flat=True))
    used_ram = sum(active.values_list("requested_ram_mb", flat=True))
    if used_cpu + cpu > server.capacity_cpu_cores:
        return False, f"CPU capacity exceeded ({used_cpu + cpu}/{server.capacity_cpu_cores} cores)."
    if used_ram + ram_mb > server.capacity_ram_mb:
        return False, f"RAM capacity exceeded ({used_ram + ram_mb}/{server.capacity_ram_mb} MB)."
    return True, ""


class DeploymentCreateView(LoginRequiredMixin, TemplateView):
    template_name = "deployments/create_instance.html"
    INSTANCE_PAGE_SIZE = 12

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not getattr(request, "organization", None):
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            messages.error(request, "You do not have permission to create instances.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        section = (self.request.GET.get("section") or "servers").strip()
        if section == "enterprise" and not self.request.user.is_platform_admin:
            section = "servers"
        server_id = (self.request.GET.get("server_id") or "").strip()
        server_tab = (self.request.GET.get("server_tab") or "").strip().lower()
        if server_tab not in {"dashboard", "monitoring", "instances", "postgres", "cleanup", "ssh", "settings"}:
            server_tab = "dashboard"
        # Default to list view on the all-instances page; kanban elsewhere.
        _default_view = "list" if (section == "instances" and not server_id) else "kanban"
        instance_view_mode = (self.request.GET.get("instance_view") or _default_view).strip().lower()
        if instance_view_mode not in {"kanban", "list"}:
            instance_view_mode = _default_view
        instance_page_number = (self.request.GET.get("page") or "1").strip()
        accounts = CloudAccount.objects.filter(organization=org, is_verified=True)
        ctx["accounts"] = accounts
        ctx["show_dns_view"] = section == "dns"
        ctx["show_instances_view"] = section == "instances"
        ctx["show_enterprise_view"] = section == "enterprise"
        ctx["instance_view_mode"] = instance_view_mode
        ctx["default_tls_mode"] = _default_tls_mode()
        ctx["PLATFORM_BASE_DOMAIN"] = platform_base_domain()
        ctx["platform_domains_enabled"] = platform_domains_enabled()
        ctx["platform_dns_configured"] = platform_dns_is_configured()
        ctx["platform_dns_proxied"] = platform_dns_default_proxied()
        ctx["enterprise_sources"] = EnterpriseSource.objects.all().order_by("-created_at")[:20] if self.request.user.is_platform_admin else []
        ctx["enterprise_active_sources"] = {
            row.odoo_version: row
            for row in EnterpriseSource.objects.filter(is_active=True, status=EnterpriseSource.Status.READY)
        } if self.request.user.is_platform_admin else {}
        ctx["enterprise_archive_root"] = str(_enterprise_archive_root())
        ctx["enterprise_extract_root"] = str(_enterprise_extract_root())
        ctx["pyos_ssh_settings_form"] = PyOSSSHSettingsForm(
            instance=PyOSSSHSettings.get_or_create_settings()
        )
        from cloud.models import ExternalServer

        ctx["external_servers"] = ExternalServer.objects.filter(
            organization=org, is_verified=True
        ).order_by("-created_at")
        ctx["infrastructures"] = Infrastructure.objects.filter(
            organization=org
        ).select_related("cloud_account", "external_server")[:100]
        ctx["odoo_servers"] = (
            OdooServer.objects.filter(organization=org)
            .filter(is_active=True)
            .select_related("infrastructure", "infrastructure__external_server", "cloud_account")
            .order_by("-created_at")[:100]
        )
        ctx["archived_servers"] = (
            OdooServer.objects.filter(organization=org, is_active=False, status=OdooServer.Status.ARCHIVED)
            .select_related("infrastructure", "infrastructure__external_server", "cloud_account")
            .order_by("-updated_at")[:50]
        )
        instance_queryset = (
            OdooInstance.objects.filter(organization=org, server__is_active=True)
            .exclude(status=OdooInstance.Status.DELETED)
            .select_related("server", "staging_environment")
            .order_by("-created_at")
        )
        ctx["selected_server"] = None
        filtered_instances = instance_queryset
        ctx["server_id"] = server_id
        if server_id:
            selected_server = get_object_or_404(
                OdooServer.objects.select_related("infrastructure", "infrastructure__external_server", "cloud_account"),
                pk=server_id,
                organization=org,
                is_active=True,
            )
            if server_tab == "cleanup" and selected_server.deployment_mode != OdooServer.DeploymentMode.DOCKER:
                server_tab = "dashboard"
            ctx["selected_server"] = selected_server
            filtered_instances = filtered_instances.filter(server=selected_server)
        ctx["server_dashboard_tab"] = server_tab
        instance_paginator = Paginator(filtered_instances, self.INSTANCE_PAGE_SIZE)
        instance_page_obj = instance_paginator.get_page(instance_page_number)
        visible_instances = list(instance_page_obj.object_list)
        ctx["odoo_instances"] = visible_instances
        ctx["selected_instances"] = visible_instances if ctx["selected_server"] else []
        ctx["visible_instances"] = visible_instances
        ctx["instance_page_obj"] = instance_page_obj
        ctx["instance_page_numbers"] = list(
            range(
                max(1, instance_page_obj.number - 2),
                min(instance_paginator.num_pages, instance_page_obj.number + 2) + 1,
            )
        )
        ctx["instance_total_count"] = instance_queryset.count()
        ctx["instance_filter_total"] = instance_paginator.count
        ctx["instance_page_size"] = self.INSTANCE_PAGE_SIZE
        ctx["instance_page_start"] = instance_page_obj.start_index() if instance_paginator.count else 0
        ctx["instance_page_end"] = instance_page_obj.end_index() if instance_paginator.count else 0
        ctx["all_instances_url"] = self._build_instances_url(instance_view=instance_view_mode)
        ctx["instance_current_base_url"] = self._build_instances_url(
            server_id=server_id,
            server_tab=server_tab if server_id else "",
            instance_view=instance_view_mode,
        )
        ctx["instance_prev_url"] = self._build_instances_url(
            server_id=server_id,
            server_tab=server_tab if server_id else "",
            page=instance_page_obj.previous_page_number(),
            instance_view=instance_view_mode,
        ) if instance_page_obj.has_previous() else ""
        ctx["instance_next_url"] = self._build_instances_url(
            server_id=server_id,
            server_tab=server_tab if server_id else "",
            page=instance_page_obj.next_page_number(),
            instance_view=instance_view_mode,
        ) if instance_page_obj.has_next() else ""
        ctx["recent_runs"] = TerraformRun.objects.filter(
            instance__organization=org
        ).select_related("instance")[:15]
        ctx["enforcer"] = getattr(self.request, "subscription_enforcer", SubscriptionEnforcer(org))
        from cloud.models import SystemSSHKey
        ctx["dafeapp_public_key"] = SystemSSHKey.get_or_create_keypair().public_key
        ctx["pyos_default_ssh_key_path"] = PyOSSSHSettings.get_or_create_settings().default_ssh_key_path
        ctx["platform_account_available"] = CloudAccount.get_platform_account() is not None
        return ctx

    def _build_instances_url(
        self,
        *,
        server_id: str = "",
        server_tab: str = "",
        page: int | None = None,
        instance_view: str = "kanban",
    ) -> str:
        params = {"section": "instances"}
        if server_id:
            params["server_id"] = server_id
        if server_tab:
            params["server_tab"] = server_tab
        if page and int(page) > 1:
            params["page"] = page
        if instance_view and instance_view != "kanban":
            params["instance_view"] = instance_view
        return f"{reverse('deployments:create-instance')}?{urlencode(params)}#instances"

    def post(self, request):
        if (
            request.user.is_platform_admin
            and (request.POST.get("action") or "").strip() == "save_pyos_ssh_settings"
        ):
            settings_obj = PyOSSSHSettings.get_or_create_settings()
            form = PyOSSSHSettingsForm(request.POST, instance=settings_obj)
            if form.is_valid():
                form.save()
                messages.success(request, "Default SSH key path saved.")
                return redirect(f"{reverse('deployments:create-instance')}?section=enterprise#enterprise-settings")

            context = self.get_context_data()
            context["pyos_ssh_settings_form"] = form
            context["show_enterprise_view"] = True
            return self.render_to_response(context)

        org = request.organization
        enforcer = getattr(request, "subscription_enforcer", SubscriptionEnforcer(org))
        try:
            enforcer.ensure_active()
            enforcer.check_instance_limit()
        except (SubscriptionError, SubscriptionLimitError) as exc:
            messages.error(request, str(exc))
            return redirect("deployments:create-instance")

        account = get_object_or_404(
            CloudAccount,
            pk=request.POST.get("cloud_account"),
            organization=org,
            is_verified=True,
        )
        name = (request.POST.get("name") or "").strip()
        region = (request.POST.get("region") or "").strip()
        size = (request.POST.get("size") or "").strip()
        if not name or not region or not size:
            messages.error(request, "Name, region and size are required.")
            return redirect("deployments:create-instance")

        instance = Instance.objects.create(
            organization=org,
            cloud_account=account,
            name=name,
            region=region,
            size=size,
            status=Instance.Status.PENDING,
            created_by=request.user,
        )
        run = TerraformRun.objects.create(instance=instance, status=TerraformRun.Status.QUEUED)
        _dispatch(terraform_apply_instance, run.id)
        messages.success(request, f"Instance '{name}' queued. Run #{run.id} started.")
        return redirect("deployments:create-instance")


class CloudAccountOptionsAPIView(LoginRequiredMixin, View):
    """Provider-aware regions/sizes options for deployment form."""

    def get(self, request, account_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        account = get_object_or_404(CloudAccount, pk=account_id, organization=org, is_verified=True)
        provider = get_provider(account)
        return JsonResponse(
            {
                "regions": provider.list_regions(),
                "sizes": provider.list_sizes(),
            }
        )


class InstanceDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(Instance, pk=instance_id, organization=org)
        return JsonResponse(InstanceSerializer(instance).data)


class TerraformRunDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, run_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        run = get_object_or_404(TerraformRun.objects.select_related("instance"), pk=run_id, instance__organization=org)
        return JsonResponse(TerraformRunSerializer(run).data)


class OdooServerCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        payload = _request_data(request)

        odoo_version = (payload.get("odoo_version") or "").strip()
        if odoo_version not in ("17", "18", "19"):
            return JsonResponse({"error": "odoo_version must be '17', '18', or '19'."}, status=400)

        name = (payload.get("name") or "").strip()
        deployment_mode = (payload.get("deployment_mode") or "").strip()
        if deployment_mode not in (OdooServer.DeploymentMode.BARE_METAL, OdooServer.DeploymentMode.DOCKER):
            deployment_mode = OdooServer.DeploymentMode.DOCKER
        dns_domain = platform_base_domain() if platform_domains_enabled() else ""
        managed_zone = None
        managed_dns_enabled = False
        domain_routing_enabled = deployment_mode == OdooServer.DeploymentMode.DOCKER or platform_domains_enabled()
        tls_mode = _default_tls_mode()
        if tls_mode not in {choice for choice, _ in OdooServer.TLSMode.choices}:
            tls_mode = _default_tls_mode()
        if deployment_mode == OdooServer.DeploymentMode.DOCKER:
            domain_routing_enabled = True

        # Direct PYOS provisioning: one request creates the external server
        # connection, the infrastructure wrapper, and the OdooServer record.
        host = (payload.get("host") or "").strip()
        if host:
            port_raw = payload.get("port") or "22"
            username = (payload.get("username") or "root").strip()
            auth_type = (payload.get("auth_type") or "DAFEAPP_KEY").strip()
            password = payload.get("password") or ""
            ssh_key_path = (payload.get("ssh_key_path") or "").strip()
            logger.info(
                "Inline PYOS server create requested by %s: name=%s host=%s port=%s user=%s auth=%s",
                request.user,
                name,
                host,
                port_raw,
                username,
                auth_type,
            )
            if not name:
                return JsonResponse({"error": "name is required."}, status=400)
            if auth_type not in ("DAFEAPP_KEY", "PASSWORD"):
                return JsonResponse({"error": "auth_type must be DAFEAPP_KEY or PASSWORD."}, status=400)
            if auth_type == "PASSWORD" and not password.strip():
                return JsonResponse({"error": "Password is required for password auth."}, status=400)
            if ssh_key_path and looks_like_public_key_text(ssh_key_path):
                return JsonResponse(
                    {
                        "error": (
                            "SSH key path must be a file path on the machine running DafeApp, "
                            "not pasted public key text."
                        )
                    },
                    status=400,
                )
            try:
                port = int(port_raw)
                if not (1 <= port <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                return JsonResponse({"error": "Port must be a number between 1 and 65535."}, status=400)

            logger.info(
                "Inline PYOS preflight SSH validation started by %s for %s@%s:%s",
                request.user,
                username,
                host,
                port,
            )
            reachable, validation_message = _validate_pyos_connection(
                org=org,
                name=name,
                host=host,
                port=port,
                username=username,
                auth_type=auth_type,
                password=password,
                ssh_key_path=ssh_key_path,
            )
            if not reachable:
                logger.warning(
                    "Inline PYOS preflight SSH validation failed for %s@%s:%s: %s",
                    username,
                    host,
                    port,
                    validation_message,
                )
                return JsonResponse(
                    {
                        "error": validation_message or "Could not connect to the server over SSH.",
                        "connectivity_status": "disconnected",
                    },
                    status=400,
                )

            checked_at = timezone.now()
            infrastructure, _ = _create_pyos_infrastructure(
                org,
                name=name,
                host=host,
                port=port,
                username=username,
                auth_type=auth_type,
                password=password,
                ssh_key_path=ssh_key_path,
                created_by=request.user,
                is_verified=True,
                is_reachable=True,
                verification_error="",
                checked_at=checked_at,
            )
            server = OdooServer.objects.create(
                organization=org,
                infrastructure=infrastructure,
                cloud_account=None,
                name=name,
                odoo_version=odoo_version,
                region="pyos",
                size="existing-server",
                ip_address=host,
                dns_domain=dns_domain,
                managed_dns_enabled=managed_dns_enabled,
                managed_dns_zone=managed_zone,
                domain_routing_enabled=domain_routing_enabled,
                tls_mode=tls_mode,
                deployment_mode=deployment_mode,
                status=OdooServer.Status.CONFIGURING,
                is_reachable=True,
                last_checked_at=checked_at,
                firewall_configured=True,
                provisioning_log="Using PYOS infrastructure connection.\nConnection verified.",
                created_by=request.user,
            )
            logger.info(
                "Inline PYOS server record created: id=%s infra=%s host=%s version=%s",
                server.id,
                infrastructure.id,
                host,
                odoo_version,
            )
            # Optionally create a platform subdomain (e.g. myserver.dafeapp.com) for this server.
            # IP is immediately available for PYOS servers, so the CF record can be created now.
            pyos_domain_label = normalize_platform_domain_label(payload.get("platform_domain_label") or "")
            if pyos_domain_label and is_platform_domain_label_valid(pyos_domain_label) and platform_dns_is_configured():
                pdomain = platform_domain_for_label(pyos_domain_label)
                if pdomain and not OdooServer.objects.filter(platform_domain=pdomain).exclude(pk=server.pk).exists():
                    try:
                        _prov = platform_dns_provider_service()
                        _pres = _prov.upsert_record(
                            getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip(),
                            record_type="A",
                            name=pdomain,
                            content=str(server.ip_address),
                            proxied=platform_dns_default_proxied(),
                            ttl=1,
                        )
                        server.platform_domain = pdomain
                        server.platform_domain_record_id = str(_pres.get("id") or "")
                        server.save(update_fields=["platform_domain", "platform_domain_record_id", "updated_at"])
                        logger.info("PYOS server %s platform domain set to %s", server.id, pdomain)
                    except Exception as _pexc:
                        logger.warning("Failed to create platform domain for PYOS server %s: %s", server.id, _pexc)
            if deployment_mode == OdooServer.DeploymentMode.DOCKER:
                _dispatch(configure_docker_host, server.id)
            else:
                _dispatch(configure_odoo_server, server.id)
            return JsonResponse(OdooServerSerializer(server).data, status=201)

        # ── DafeApp Platform VPS ─────────────────────────────────────────────
        if payload.get("use_platform_account") in (True, "true", "True", "1"):
            from cloud.models import CloudAccount as _CloudAccount
            region = (payload.get("region") or "").strip()
            size   = (payload.get("size") or "").strip()
            if not name or not region or not size:
                return JsonResponse({"error": "name, region and size are required."}, status=400)
            platform_account = _CloudAccount.get_platform_account()
            if not platform_account:
                return JsonResponse(
                    {"error": "DafeApp VPS is not configured on this platform. Contact support."},
                    status=400,
                )
            infrastructure = Infrastructure.objects.create(
                organization=org,
                infra_type=Infrastructure.InfraType.MANAGED,
                cloud_account=platform_account,
                name=f"platform-vps-{org.id}-{name}",
                is_connected=True,
                validation_log="Auto-created via DafeApp VPS platform account.",
                created_by=request.user,
            )
            server = OdooServer.objects.create(
                organization=org,
                infrastructure=infrastructure,
                cloud_account=platform_account,
                name=name,
                odoo_version=odoo_version,
                region=region,
                size=size,
                dns_domain=dns_domain,
                managed_dns_enabled=managed_dns_enabled,
                managed_dns_zone=managed_zone,
                domain_routing_enabled=domain_routing_enabled,
                tls_mode=tls_mode,
                deployment_mode=deployment_mode,
                created_by=request.user,
            )
            logger.info(
                "DafeApp platform VPS create: org=%s user=%s id=%s name=%s version=%s mode=%s region=%s size=%s",
                org.id, request.user, server.id, name, odoo_version, deployment_mode, region, size,
            )
            server.status = OdooServer.Status.CONNECTING
            server.save(update_fields=["status", "updated_at"])
            _dispatch(provision_odoo_server, server.id)
            return JsonResponse(OdooServerSerializer(server).data, status=201)

        region = (payload.get("region") or "").strip()
        size = (payload.get("size") or "").strip()
        if not name or not region or not size:
            return JsonResponse({"error": "name, region and size are required."}, status=400)

        # Resolve infrastructure — accept either an existing infra id or a
        # bare cloud_account_id (auto-creates or reuses a MANAGED infrastructure).
        infra_id = str(payload.get("infrastructure_id") or "").strip()
        account_id = str(payload.get("cloud_account_id") or "").strip()

        if infra_id:
            infrastructure = get_object_or_404(Infrastructure, pk=infra_id, organization=org)
            ok, err = infrastructure.validate_connection_target()
            if not ok:
                return JsonResponse({"error": err}, status=400)
            account = infrastructure.managed_account
        elif account_id:
            account = get_object_or_404(CloudAccount, pk=account_id, organization=org, is_verified=True)
            # Reuse or create a MANAGED infrastructure for this account.
            infrastructure, _ = Infrastructure.objects.get_or_create(
                organization=org,
                infra_type=Infrastructure.InfraType.MANAGED,
                cloud_account=account,
                defaults={
                    "name": f"managed-{account_id}",
                    "is_connected": True,
                    "validation_log": "Auto-created by server provisioning.",
                    "created_by": request.user,
                },
            )
        else:
            return JsonResponse({"error": "Provide infrastructure_id or cloud_account_id."}, status=400)

        server = OdooServer.objects.create(
            organization=org,
            infrastructure=infrastructure,
            cloud_account=account,
            name=name,
            odoo_version=odoo_version,
            region=region,
            size=size,
            dns_domain=dns_domain,
            managed_dns_enabled=managed_dns_enabled,
            managed_dns_zone=managed_zone,
            domain_routing_enabled=domain_routing_enabled,
            tls_mode=tls_mode,
            deployment_mode=deployment_mode,
            created_by=request.user,
        )
        logger.info(
            "Server create requested by %s: id=%s name=%s version=%s mode=%s infra=%s region=%s size=%s",
            request.user,
            server.id,
            name,
            odoo_version,
            deployment_mode,
            getattr(infrastructure, "id", None),
            region,
            size,
        )
        # Set CONNECTING before dispatching so the response already reflects the
        # active state — the UI polls only for live statuses, so PENDING would
        # cause the card to sit frozen until a manual refresh.
        server.status = OdooServer.Status.CONNECTING
        server.save(update_fields=["status", "updated_at"])
        _dispatch(provision_odoo_server, server.id)
        return JsonResponse(OdooServerSerializer(server).data, status=201)


class OdooServerListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        version = (request.GET.get("odoo_version") or "").strip()
        qs = OdooServer.objects.filter(organization=org, is_active=True)
        if version in ("17", "18", "19"):
            qs = qs.filter(odoo_version=version)
        data = OdooServerSerializer(qs[:100], many=True).data
        return JsonResponse({"results": data})


class OdooServerDetailAPIView(LoginRequiredMixin, View):
    """GET /odoo/servers/<server_id>/ — poll status and provisioning_log."""

    def get(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org, is_active=True)
        return JsonResponse(OdooServerSerializer(server).data)


class OdooInstanceRuntimeLogsAPIView(LoginRequiredMixin, View):
    """GET /odoo/instances/<instance_id>/runtime-logs/ — fetch current Odoo runtime logs."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        instance = get_object_or_404(
            OdooInstance.objects.select_related(
                "server",
                "server__infrastructure",
                "server__infrastructure__external_server",
            ),
            pk=instance_id,
            organization=org,
        )
        server = instance.server
        if server.status != OdooServer.Status.PROVISIONED:
            return JsonResponse(
                {
                    "logs": "",
                    "source": "",
                    "checked_at": timezone.now().isoformat(),
                    "message": "Server is not provisioned yet.",
                }
            )

        from deployments.tasks import _ssh_run

        source, command = _instance_runtime_log_command(instance)
        try:
            code, output = _ssh_run(server, command, timeout=60)
        except Exception as exc:
            logger.warning("Runtime log fetch failed for instance %s", instance.id, exc_info=True)
            return JsonResponse(
                {
                    "logs": "",
                    "source": source,
                    "checked_at": timezone.now().isoformat(),
                    "error": str(exc) or "Could not fetch live runtime logs.",
                },
                status=502,
            )

        payload = {
            "logs": (output or "").strip(),
            "source": source,
            "checked_at": timezone.now().isoformat(),
        }
        if code != 0:
            payload["error"] = output or "Could not fetch live runtime logs."
            return JsonResponse(payload, status=502)
        return JsonResponse(payload)


class PyosVpsCreateAPIView(LoginRequiredMixin, View):
    """POST — create ExternalServer + Infrastructure inline from the deployment modal."""

    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        name = (request.POST.get("name") or "").strip()
        host = (request.POST.get("host") or "").strip()
        username = (request.POST.get("username") or "root").strip()
        auth_type = (request.POST.get("auth_type") or "DAFEAPP_KEY").strip()
        password = request.POST.get("password") or ""
        port_raw = request.POST.get("port") or "22"

        if not name or not host:
            return JsonResponse({"error": "Name and host IP are required."}, status=400)
        if auth_type not in ("DAFEAPP_KEY", "PASSWORD"):
            return JsonResponse({"error": "auth_type must be DAFEAPP_KEY or PASSWORD."}, status=400)
        if auth_type == "PASSWORD" and not password.strip():
            return JsonResponse({"error": "Password is required for password auth."}, status=400)
        ssh_key_path = (request.POST.get("ssh_key_path") or "").strip()
        if ssh_key_path and looks_like_public_key_text(ssh_key_path):
            return JsonResponse(
                {
                    "error": (
                        "SSH key path must be a file path on the machine running DafeApp, "
                        "not pasted public key text."
                    )
                },
                status=400,
            )
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return JsonResponse({"error": "Port must be a number between 1 and 65535."}, status=400)

        logger.info(
            "PYOS infrastructure create requested by %s: name=%s host=%s port=%s user=%s auth=%s",
            request.user,
            name,
            host,
            port,
            username,
            auth_type,
        )

        infra, ext = _create_pyos_infrastructure(
            org,
            name=name,
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            ssh_key_path=ssh_key_path,
            created_by=request.user,
        )
        return JsonResponse({"infrastructure_id": infra.id, "external_server_id": ext.id}, status=201)


class OdooServerCheckConnectivityView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/check/ — probe reachability by IP/port."""

    def post(self, request, server_id):
        from django.utils import timezone

        from deployments.tasks import _odoo_server_ssh_target, _persist_server_reachability, _probe_server_ssh

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
            pk=server_id,
            organization=org,
            is_active=True,
        )
        lock_reason = _server_mutation_lock_reason(server)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        logger.info("Manual reachability check requested by %s for server %s", request.user, server.id)

        host, port = _odoo_server_ssh_target(server)
        reachable, message = _probe_server_ssh(server)

        now = timezone.now()
        if host and server.ip_address != host:
            server.ip_address = host
            server.save(update_fields=["ip_address", "updated_at"])
        _persist_server_reachability(server, reachable=reachable, message=message, checked_at=now)

        payload = {
            "is_reachable": reachable,
            "last_checked_at": now.isoformat(),
            "connectivity_status": "connected" if reachable else "disconnected",
        }
        if message:
            payload["message"] = message
        logger.info(
            "Manual reachability check finished for server %s: %s (%s:%s)",
            server.id,
            "connected" if reachable else "disconnected",
            host,
            port,
        )
        return JsonResponse(payload)


from deployments.tasks import _METRICS_CMD


class OdooServerMetricsAPIView(LoginRequiredMixin, View):
    """GET /odoo/servers/<server_id>/metrics/ — collect live CPU/memory/disk/connections via SSH."""

    def get(self, request, server_id):
        from django.core.cache import cache
        from deployments.tasks import _connect_ssh_client

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
            pk=server_id,
            organization=org,
            is_active=True,
        )

        if server.status != OdooServer.Status.PROVISIONED:
            return JsonResponse({"error": "Server is not provisioned."}, status=409)

        cache_key = f"server_metrics_{server_id}"
        cached = cache.get(cache_key)
        if cached:
            return JsonResponse(cached)

        client = tmp_key = None
        try:
            client, tmp_key = _connect_ssh_client(server)
            _, stdout, stderr = client.exec_command(_METRICS_CMD, timeout=10)
            out = stdout.read().decode().strip()
            parts = out.split(",")
            if len(parts) != 4:
                raise ValueError(f"Unexpected output: {out!r}")
            result = {
                "cpu": float(parts[0]),
                "memory": float(parts[1]),
                "disk": int(parts[2]),
                "connections": int(parts[3]),
            }
        except Exception as exc:
            logger.warning("Server %s metrics collection failed: %s", server_id, exc)
            return JsonResponse({"error": str(exc)}, status=502)
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            if tmp_key:
                try:
                    import os
                    os.unlink(tmp_key)
                except Exception:
                    pass

        cache.set(cache_key, result, timeout=30)
        return JsonResponse(result)


class OdooServerReprovisionAPIView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/reprovision/ — re-run server configuration on a FAILED server."""

    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
            pk=server_id,
            organization=org,
            is_active=True,
        )

        if server.status not in (OdooServer.Status.FAILED, OdooServer.Status.PROVISIONED):
            return JsonResponse(
                {"error": f"Server must be in FAILED or PROVISIONED state to re-provision (current: {server.status})."},
                status=409,
            )
        if not server.ip_address and server.infrastructure and server.infrastructure.infra_type != "PYOS":
            return JsonResponse({"error": "Server has no IP address; cannot re-provision."}, status=400)

        # For PYOS servers without an IP (connectivity never confirmed): run the full provision flow
        # which includes the connectivity check. For servers that already have an IP, go straight
        # to configure (skip Terraform re-run).
        from deployments.tasks import provision_odoo_server
        if not server.ip_address:
            server.status = OdooServer.Status.CONNECTING
            server.provisioning_log = ""
            server.celery_task_id = ""
            server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
            job = _dispatch(provision_odoo_server, server.id)
        else:
            server.status = OdooServer.Status.CONNECTING
            server.provisioning_log = ""
            server.save(update_fields=["status", "provisioning_log", "updated_at"])

            if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
                job = _dispatch(configure_docker_host, server.id)
            else:
                job = _dispatch(configure_odoo_server, server.id)

        logger.info(
            "Re-provision triggered for server %s (mode=%s) by user %s",
            server.id, server.deployment_mode, request.user,
        )
        return JsonResponse({"ok": True, "message": "Re-provisioning started.", "task_id": str(getattr(job, "id", ""))})


class OdooServerRefreshTraefikView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/refresh-traefik/ — force-refresh the Traefik gateway on a bare-metal server."""

    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer,
            pk=server_id,
            organization=org,
            is_active=True,
        )

        if server.deployment_mode != OdooServer.DeploymentMode.BARE_METAL:
            return JsonResponse({"error": "Traefik refresh is only available for bare-metal servers."}, status=400)
        if server.status not in (OdooServer.Status.PROVISIONED, OdooServer.Status.FAILED):
            return JsonResponse(
                {"error": f"Server must be PROVISIONED or FAILED (current: {server.status})."},
                status=409,
            )

        from deployments.tasks import refresh_traefik_gateway
        job = refresh_traefik_gateway.delay(server.id)
        return JsonResponse({"ok": True, "message": "Traefik gateway refresh started.", "task_id": str(job.id)})


class OdooServerPlatformDomainAPIView(LoginRequiredMixin, View):
    """POST   /odoo/servers/<server_id>/platform-domain/ — set or change the server's DafeApp subdomain.
       DELETE /odoo/servers/<server_id>/platform-domain/ — remove it and delete the Cloudflare record."""

    def _get_server(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return None, JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return None, JsonResponse({"error": "Permission denied."}, status=403)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org, is_active=True)
        return server, None

    def post(self, request, server_id):
        server, err = self._get_server(request, server_id)
        if err:
            return err

        if not platform_dns_is_configured():
            return JsonResponse({"error": "Platform DNS is not configured."}, status=400)
        if not server.ip_address:
            return JsonResponse(
                {"error": "Server has no IP address yet. Wait for provisioning to complete before setting a domain."},
                status=400,
            )

        payload = _request_data(request)
        label = normalize_platform_domain_label(payload.get("label") or "")
        if not label or not is_platform_domain_label_valid(label):
            return JsonResponse(
                {
                    "error": (
                        "label must be 6-63 characters, use only lowercase letters, numbers, or hyphens, "
                        "and cannot start or end with a hyphen."
                    )
                },
                status=400,
            )

        new_domain = platform_domain_for_label(label)
        if not new_domain:
            return JsonResponse({"error": "Platform base domain is not configured."}, status=400)

        # Reject if another server already owns this domain
        if OdooServer.objects.filter(platform_domain=new_domain).exclude(pk=server.pk).exists():
            return JsonResponse({"error": "That domain is already in use by another server."}, status=400)

        zone_id = getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip()
        provider = platform_dns_provider_service()

        # Delete the previous Cloudflare record if one exists
        if server.platform_domain_record_id:
            try:
                provider.delete_record(zone_id, server.platform_domain_record_id)
                logger.info("Deleted old platform domain CF record %s for server %s", server.platform_domain_record_id, server.id)
            except Exception as exc:
                logger.warning("Could not delete old platform domain record for server %s: %s", server.id, exc)

        # Create the new A record
        try:
            result = provider.upsert_record(
                zone_id,
                record_type="A",
                name=new_domain,
                content=str(server.ip_address),
                proxied=platform_dns_default_proxied(),
                ttl=1,
            )
        except Exception as exc:
            return JsonResponse({"error": f"Cloudflare error: {exc}"}, status=502)

        server.platform_domain = new_domain
        server.platform_domain_record_id = str(result.get("id") or "")
        server.save(update_fields=["platform_domain", "platform_domain_record_id", "updated_at"])
        logger.info(
            "Server %s platform domain set to %s (CF record id: %s)",
            server.id, new_domain, server.platform_domain_record_id,
        )
        return JsonResponse(OdooServerSerializer(server).data)

    def delete(self, request, server_id):
        server, err = self._get_server(request, server_id)
        if err:
            return err

        if not server.platform_domain:
            return JsonResponse({"error": "This server has no platform domain set."}, status=400)

        if server.platform_domain_record_id and platform_dns_is_configured():
            zone_id = getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip()
            try:
                provider = platform_dns_provider_service()
                provider.delete_record(zone_id, server.platform_domain_record_id)
                logger.info("Deleted platform domain CF record %s for server %s", server.platform_domain_record_id, server.id)
            except Exception as exc:
                logger.warning("Could not delete platform domain CF record for server %s: %s", server.id, exc)

        old_domain = server.platform_domain
        server.platform_domain = ""
        server.platform_domain_record_id = ""
        server.save(update_fields=["platform_domain", "platform_domain_record_id", "updated_at"])
        logger.info("Server %s platform domain %s removed", server.id, old_domain)
        return JsonResponse({"ok": True, "message": f"Platform domain {old_domain} removed."})


class OdooServerCancelProvisionAPIView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/cancel/ — cancel a running CONNECTING/PROVISIONING task."""

    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer,
            pk=server_id,
            organization=org,
            is_active=True,
        )

        cancellable = {OdooServer.Status.CONNECTING, OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING}
        if server.status not in cancellable:
            return JsonResponse(
                {"error": f"Server is not in a cancellable state (current: {server.status})."},
                status=409,
            )

        task_id = server.celery_task_id
        if task_id:
            try:
                from dafeapp.celery import app as celery_app
                celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
            except Exception as exc:
                logger.warning("Could not revoke celery task %s for server %s: %s", task_id, server_id, exc)

        server.status = OdooServer.Status.FAILED
        server.celery_task_id = ""
        server.provisioning_log = (server.provisioning_log or "") + "\n[Cancelled by user]"
        server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])

        from deployments.tasks import _broadcast_server
        _broadcast_server(server.id, "Provisioning cancelled by user.", server.status, done=True)

        logger.info("Server %s provisioning cancelled by user %s", server.id, request.user)
        return JsonResponse({"ok": True, "message": "Provisioning cancelled."})


class OdooInstanceCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        enforcer = getattr(request, "subscription_enforcer", SubscriptionEnforcer(org))
        try:
            enforcer.ensure_active()
            enforcer.check_instance_limit()
        except (SubscriptionError, SubscriptionLimitError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        payload = _request_data(request)
        server = get_object_or_404(
            OdooServer,
            pk=payload.get("server_id"),
            organization=org,
        )
        if not server.is_active:
            return JsonResponse({"error": "Server is archived."}, status=400)
        if server.status != OdooServer.Status.PROVISIONED:
            return JsonResponse({"error": "Server is not PROVISIONED yet."}, status=400)
        if server.last_checked_at is not None and not server.is_reachable:
            return JsonResponse(
                {"error": "Server SSH is unreachable. Click Check on the server card before creating instances."},
                status=400,
            )

        name = (payload.get("name") or "").strip()
        db_name = (payload.get("db_name") or "").strip()
        custom_domain = normalize_domain_name(payload.get("custom_domain") or payload.get("domain") or "")
        requested_platform_label = normalize_platform_domain_label(
            payload.get("platform_domain_label") or payload.get("platform_label") or ""
        )
        req_cpu = int(payload.get("requested_cpu_cores") or 1)
        req_ram = int(payload.get("requested_ram_mb") or 1024)
        port_raw = payload.get("http_port")
        if not name or not db_name:
            return JsonResponse({"error": "name and db_name are required."}, status=400)
        if custom_domain and _domain_in_use(org, custom_domain):
            return JsonResponse({"error": "Custom domain is already used by another instance."}, status=400)

        remote_used_ports: set[int] = set()
        if port_raw:
            port = int(port_raw)
            remote_used_ports = _remote_used_ports(server)
        else:
            port = _next_available_port(server)
        if port is None:
            return JsonResponse({"error": "No available port on this server."}, status=400)
        if port < server.min_port or port > server.max_port:
            return JsonResponse({"error": f"Port must be within {server.min_port}-{server.max_port}."}, status=400)
        if _active_instances_for_server(server).filter(http_port=port).exists():
            return JsonResponse({"error": "Selected port is already in use on this server."}, status=400)
        if port in remote_used_ports:
            return JsonResponse({"error": f"Selected port {port} is already listening on the target server."}, status=400)

        ok, capacity_msg = _capacity_check(server, req_cpu, req_ram)
        if not ok:
            return JsonResponse({"error": capacity_msg}, status=400)

        if requested_platform_label and not is_platform_domain_label_valid(requested_platform_label):
            return JsonResponse(
                {"error": "DafeApp domain prefix must be 6-63 characters, use only lowercase letters, numbers, or hyphens, and cannot start or end with a hyphen."},
                status=400,
            )

        platform_domain = platform_domain_for_label(requested_platform_label) if requested_platform_label else _generate_platform_domain(name)
        if not platform_domain:
            return JsonResponse({"error": f"Platform base domain is not configured. Set {platform_base_domain() or 'PLATFORM_BASE_DOMAIN'} first."}, status=400)
        if _domain_in_use(org, platform_domain):
            return JsonResponse({"error": "That DafeApp domain prefix is already used. Choose another one or regenerate."}, status=400)

        logger.info(
            "Instance create requested by %s: server=%s name=%s db_name=%s mode=%s port=%s platform_domain=%s custom_domain=%s",
            request.user,
            server.id,
            name,
            db_name,
            server.deployment_mode,
            port,
            platform_domain,
            custom_domain or "",
        )

        inst = OdooInstance.objects.create(
            organization=org,
            server=server,
            name=name,
            db_name=db_name,
            domain=platform_domain,
            http_port=port,
            domain_status=OdooInstance.DomainStatus.PENDING if platform_domain else OdooInstance.DomainStatus.NOT_CONFIGURED,
            ssl_status=(
                OdooInstance.SSLStatus.NOT_CONFIGURED
                if not platform_domain or server.tls_mode == OdooServer.TLSMode.DISABLED
                else OdooInstance.SSLStatus.PENDING
            ),
            requested_cpu_cores=req_cpu,
            requested_ram_mb=req_ram,
            created_by=request.user,
        )
        _sync_instance_domain_assignment(
            inst,
            platform_domain,
            source=DomainAssignment.Source.PLATFORM,
            is_primary=True,
        )
        if custom_domain:
            _sync_instance_domain_assignment(
                inst,
                custom_domain,
                source=DomainAssignment.Source.CUSTOM,
                is_primary=False,
            )
        _dispatch(create_odoo_instance, inst.id)
        return JsonResponse(OdooInstanceSerializer(inst).data, status=201)


class OdooInstanceDomainAttachAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"error": "Instance is deleted."}, status=400)
        lock_reason = _instance_mutation_lock_reason(instance, include_jobs=False)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        payload = _request_data(request)
        domain = normalize_domain_name(payload.get("domain") or "")
        if not domain:
            return JsonResponse({"error": "domain is required."}, status=400)
        if _domain_in_use(org, domain, exclude_instance_id=instance.id):
            return JsonResponse({"error": "Domain is already used by another instance."}, status=400)
        if domain == instance.domain:
            return JsonResponse({"ok": True, "message": "That domain is already the primary DafeApp hostname."})

        _sync_instance_domain_assignment(
            instance,
            domain,
            source=DomainAssignment.Source.CUSTOM,
            is_primary=False,
        )
        _dispatch(provision_instance_domain, instance.id)
        return JsonResponse(OdooInstanceSerializer(instance).data)


class OdooInstanceDomainDetachAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance, include_jobs=False)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        payload = _request_data(request)
        domain = normalize_domain_name(payload.get("domain") or "")
        if not domain:
            return JsonResponse({"error": "domain is required to detach a custom alias."}, status=400)
        if domain == instance.domain:
            return JsonResponse({"error": "The primary DafeApp domain cannot be detached."}, status=400)
        assignment = instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).filter(domain=domain).first()
        if assignment is None:
            return JsonResponse({"ok": True, "message": "That custom domain is not attached to this instance."})
        _dispatch(detach_instance_domain, instance.id, domain)
        return JsonResponse({"ok": True, "message": "Custom domain detach queued."})


class OdooInstanceDomainRetryAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance, include_jobs=False)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        if not instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).exists():
            return JsonResponse({"error": "This instance has no domain to reprovision."}, status=400)

        instance.domain_status = OdooInstance.DomainStatus.PENDING
        instance.domain_last_checked_at = None
        if instance.server.tls_mode != OdooServer.TLSMode.DISABLED:
            instance.ssl_status = OdooInstance.SSLStatus.PENDING
        instance.ssl_error = ""
        instance.save(
            update_fields=[
                "domain_status",
                "domain_last_checked_at",
                "ssl_status",
                "ssl_error",
                "updated_at",
            ]
        )
        _dispatch(provision_instance_domain, instance.id)
        return JsonResponse({"ok": True, "message": "Domain reprovision queued."})


class OdooInstanceListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server_id = request.GET.get("server_id")
        try:
            page_size = int(request.GET.get("page_size") or DeploymentCreateView.INSTANCE_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = DeploymentCreateView.INSTANCE_PAGE_SIZE
        page_size = max(1, min(page_size, 48))
        page_number = (request.GET.get("page") or "1").strip()
        qs = OdooInstance.objects.filter(
            organization=org,
            server__is_active=True,
            server__status__in=[
                OdooServer.Status.PENDING,
                OdooServer.Status.PROVISIONING,
                OdooServer.Status.CONFIGURING,
                OdooServer.Status.PROVISIONED,
                OdooServer.Status.FAILED,
            ],
        ).exclude(status=OdooInstance.Status.DELETED).select_related("server")
        if server_id:
            qs = qs.filter(server_id=server_id)
        qs = qs.order_by("-created_at")
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page_number)
        data = OdooInstanceSerializer(page_obj.object_list, many=True).data
        return JsonResponse(
            {
                "results": data,
                "count": paginator.count,
                "page": page_obj.number,
                "num_pages": paginator.num_pages,
                "page_size": page_size,
                "has_previous": page_obj.has_previous(),
                "has_next": page_obj.has_next(),
            }
        )


class OdooInstanceGitRepoListAPIView(LoginRequiredMixin, View):
    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER", "USER"):
            return JsonResponse({"error": "Permission denied."}, status=403)
        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        repos = instance.git_repos.select_related("credential").all()
        data = OdooInstanceGitRepoSerializer(repos, many=True).data
        return JsonResponse({"results": data})

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        payload = _request_data(request)
        repo_name = _derive_repo_name(payload.get("repo_name"), payload.get("git_url", ""))
        git_url = (payload.get("git_url") or "").strip()
        branch = (payload.get("branch") or "main").strip()
        auth_type = (payload.get("auth_type") or OdooInstanceGitRepo.AuthType.PUBLIC).strip()
        if not git_url:
            return JsonResponse({"error": "git_url is required."}, status=400)
        if auth_type not in OdooInstanceGitRepo.AuthType.values:
            return JsonResponse({"error": "Unsupported auth_type."}, status=400)
        if instance.git_repos.filter(repo_name=repo_name).exists():
            return JsonResponse({"error": "A repo with that name already exists on this instance."}, status=400)

        try:
            credential = _resolve_git_credential(
                org=org,
                user=request.user,
                payload=payload,
                auth_type=auth_type,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        try:
            repo, job = _create_instance_repo_and_dispatch(
                org=org,
                user=request.user,
                instance=instance,
                repo_name=repo_name,
                git_url=git_url,
                branch=branch,
                auth_type=auth_type,
                credential=credential,
                auto_update=str(payload.get("auto_update", "")).lower() in ("1", "true", "yes", "on"),
                install_requirements_on_update=str(payload.get("install_requirements_on_update", "")).lower() in ("1", "true", "yes", "on"),
                auto_upgrade_modules_on_update=str(payload.get("auto_upgrade_modules_on_update", "true")).lower() not in ("0", "false", "no", "off"),
                is_enabled=str(payload.get("is_enabled", "true")).lower() not in ("0", "false", "no", "off"),
                display_order=payload.get("display_order"),
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if repo.auto_update:
            try:
                _ensure_github_push_webhook(repo=repo, request=request)
            except (ValueError, RuntimeError) as exc:
                logger.warning("GitHub webhook setup skipped for repo %s: %s", repo.id, exc)
        data = OdooInstanceGitRepoSerializer(repo).data
        data["job_id"] = job.id
        return JsonResponse(data, status=201)


class OdooInstanceGitRepoDetailAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        repo = get_object_or_404(
            OdooInstanceGitRepo.objects.select_related("instance", "instance__server", "credential"),
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(repo.instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        payload = _request_data(request)

        branch_changed = False
        resync_needed = False
        refresh_needed = False

        if "repo_name" in payload:
            next_repo_name = _derive_repo_name(payload.get("repo_name"), repo.git_url)
            if next_repo_name != repo.repo_name:
                repo.repo_name = next_repo_name
                resync_needed = True
        if "git_url" in payload and payload.get("git_url"):
            next_git_url = payload.get("git_url").strip()
            if next_git_url != repo.git_url:
                repo.git_url = next_git_url
                resync_needed = True
        if "branch" in payload and payload.get("branch") and payload.get("branch").strip() != repo.branch:
            repo.branch = payload.get("branch").strip()
            branch_changed = True
            repo.pinned_commit = ""
        if "auto_update" in payload:
            repo.auto_update = str(payload.get("auto_update")).lower() in ("1", "true", "yes", "on")
        if "install_requirements_on_update" in payload:
            repo.install_requirements_on_update = str(payload.get("install_requirements_on_update")).lower() in ("1", "true", "yes", "on")
        if "auto_upgrade_modules_on_update" in payload:
            repo.auto_upgrade_modules_on_update = str(payload.get("auto_upgrade_modules_on_update")).lower() in ("1", "true", "yes", "on")
        if "is_enabled" in payload:
            new_enabled = str(payload.get("is_enabled")).lower() in ("1", "true", "yes", "on")
            if new_enabled != repo.is_enabled:
                repo.is_enabled = new_enabled
                refresh_needed = True
        if "display_order" in payload and payload.get("display_order") not in ("", None):
            repo.display_order = int(payload.get("display_order"))
            refresh_needed = True
        if "pinned_commit" in payload:
            repo.pinned_commit = (payload.get("pinned_commit") or "").strip()
        if "auth_type" in payload:
            auth_type = payload.get("auth_type").strip()
            if auth_type not in OdooInstanceGitRepo.AuthType.values:
                return JsonResponse({"error": "Unsupported auth_type."}, status=400)
            if auth_type != repo.auth_type:
                repo.auth_type = auth_type
                resync_needed = True
                repo.pinned_commit = ""
            try:
                next_credential = _resolve_git_credential(
                    org=org,
                    user=request.user,
                    payload=payload,
                    auth_type=auth_type,
                )
            except ValueError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            if next_credential != repo.credential:
                repo.credential = next_credential
                resync_needed = True
                repo.pinned_commit = ""

        repo.local_path = _build_repo_local_path(repo.instance, repo.repo_name)
        repo.save()

        job = None
        if branch_changed:
            job = _repo_job(
                org,
                job_type=DeploymentJob.JobType.CHECKOUT_INSTANCE_REPO_BRANCH,
                instance=repo.instance,
                user=request.user,
            )
            _dispatch(checkout_instance_repo_branch, repo.id, repo.branch, job.id)
        elif resync_needed:
            job = _repo_job(
                org,
                job_type=DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
                instance=repo.instance,
                user=request.user,
            )
            _dispatch(update_instance_repo, repo.id, job.id)
        elif refresh_needed:
            job = _repo_job(
                org,
                job_type=DeploymentJob.JobType.REFRESH_INSTANCE_ADDONS,
                instance=repo.instance,
                user=request.user,
            )
            _dispatch(refresh_instance_addons, repo.instance_id, job.id)

        try:
            if repo.auto_update:
                _ensure_github_push_webhook(repo=repo, request=request)
        except (ValueError, RuntimeError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        data = OdooInstanceGitRepoSerializer(repo).data
        if job:
            data["job_id"] = job.id
        return JsonResponse(data)


class OdooInstanceGitRepoSyncAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        repo = get_object_or_404(
            OdooInstanceGitRepo,
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(repo.instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
            instance=repo.instance,
            user=request.user,
        )
        _dispatch(update_instance_repo, repo.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id})


class OdooInstanceGitRepoStatusAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        repo = get_object_or_404(
            OdooInstanceGitRepo,
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(repo.instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        try:
            sync_instance_repo_status(repo.id)
        except Exception as exc:
            logger.exception("Repo status refresh failed for repo %s", repo.id)
            return JsonResponse({"error": str(exc)}, status=400)

        repo.refresh_from_db()
        return JsonResponse(OdooInstanceGitRepoSerializer(repo).data)


class OdooInstanceGitRepoRollbackAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        repo = get_object_or_404(
            OdooInstanceGitRepo,
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(repo.instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        payload = _request_data(request)
        target_commit = (payload.get("target_commit") or repo.previous_commit or "").strip()
        if not target_commit:
            return JsonResponse({"error": "No rollback target is available."}, status=400)
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.ROLLBACK_INSTANCE_REPO,
            instance=repo.instance,
            user=request.user,
        )
        _dispatch(rollback_instance_repo, repo.id, target_commit, job.id)
        return JsonResponse({"ok": True, "job_id": job.id, "target_commit": target_commit})


class OdooInstanceGitRepoDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        repo = get_object_or_404(
            OdooInstanceGitRepo,
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(repo.instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.REMOVE_INSTANCE_REPO,
            instance=repo.instance,
            user=request.user,
        )
        _dispatch(remove_instance_repo, repo.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id})


class OdooInstanceToggleCoreAutoUpdateAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/toggle-core-auto-update/"""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        instance.auto_update_core = not instance.auto_update_core
        instance.save(update_fields=["auto_update_core", "updated_at"])
        return JsonResponse({"auto_update_core": instance.auto_update_core})


class OdooInstanceRepoPendingCommitsAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/instances/<id>/repos/<repo_id>/pending-commits/
    Returns commits on the tracked branch that haven't been pulled yet.
    """

    def get(self, request, instance_id, repo_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        repo = get_object_or_404(
            OdooInstanceGitRepo,
            pk=repo_id,
            instance_id=instance_id,
            instance__organization=org,
        )
        if not repo.local_path:
            return JsonResponse({"commits": [], "count": 0})

        server = repo.instance.server
        from deployments.tasks import _ssh_run  # noqa: PLC0415

        base_commit = repo.last_pulled_commit or "HEAD"
        fmt = r"%H|%s|%an|%aI"
        cmd = (
            f"git -C {repo.local_path} fetch --quiet 2>/dev/null; "
            f"git -C {repo.local_path} log {base_commit}..origin/{repo.branch}"
            f" --format='{fmt}' 2>/dev/null || true"
        )
        try:
            rc, stdout = _ssh_run(server, cmd, timeout=30)
        except Exception as exc:
            return JsonResponse({"error": f"Could not retrieve git log: {exc}"}, status=502)

        commits = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "sha": parts[0][:40],
                    "message": parts[1][:200],
                    "author": parts[2][:120],
                    "timestamp": parts[3],
                })
        return JsonResponse({"commits": commits, "count": len(commits)})


class GitRepositoryCredentialListCreateAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        credentials = GitRepositoryCredential.objects.filter(organization=org)
        github_accounts = VCSAccount.objects.filter(
            user=request.user,
            provider=VCSAccount.Provider.GITHUB,
            is_active=True,
        ).values("id", "username", "provider", "connected_at")
        return JsonResponse(
            {
                "results": GitRepositoryCredentialSerializer(credentials, many=True).data,
                "github_accounts": list(github_accounts),
            }
        )

    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        payload = _request_data(request)
        auth_type = (payload.get("auth_type") or "").strip()
        try:
            credential = _resolve_git_credential(
                org=org,
                user=request.user,
                payload=payload,
                auth_type=auth_type,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(GitRepositoryCredentialSerializer(credential).data, status=201)


class EnterpriseSourceListCreateAPIView(LoginRequiredMixin, View):
    def get(self, request):
        rows = EnterpriseSource.objects.all()
        if not request.user.is_platform_admin:
            rows = rows.filter(
                Q(source_scope=EnterpriseSource.Scope.PLATFORM, is_active=True)
                | Q(source_scope=EnterpriseSource.Scope.USER, owner=request.user)
            )
        rows = rows.order_by("-created_at")
        return JsonResponse({"results": EnterpriseSourceSerializer(rows, many=True).data})

    def post(self, request):
        archive = request.FILES.get("archive")
        if not archive:
            return JsonResponse({"error": "archive is required."}, status=400)
        requested_scope = (request.POST.get("source_scope") or "").strip().upper()
        if request.user.is_platform_admin and requested_scope != EnterpriseSource.Scope.USER:
            scope = EnterpriseSource.Scope.PLATFORM
        else:
            scope = EnterpriseSource.Scope.USER
        try:
            source = _save_and_extract_enterprise_archive(
                archive_file=archive,
                odoo_version=request.POST.get("odoo_version") or "",
                uploaded_by=request.user,
                scope=scope,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(EnterpriseSourceSerializer(source).data, status=201)


class EnterpriseSourceActivateAPIView(LoginRequiredMixin, View):
    def post(self, request, source_id):
        if not request.user.is_platform_admin:
            return JsonResponse({"error": "Platform admin permission required."}, status=403)
        source = get_object_or_404(EnterpriseSource, pk=source_id)
        if source.source_scope != EnterpriseSource.Scope.PLATFORM:
            return JsonResponse({"error": "Only platform Enterprise sources can be set active globally."}, status=400)
        if source.status != EnterpriseSource.Status.READY:
            return JsonResponse({"error": "Only ready Enterprise sources can be activated."}, status=400)
        EnterpriseSource.objects.filter(
            odoo_version=source.odoo_version,
            source_scope=EnterpriseSource.Scope.PLATFORM,
            is_active=True,
        ).exclude(pk=source.pk).update(is_active=False)
        source.is_active = True
        source.save(update_fields=["is_active", "updated_at"])
        return JsonResponse({"ok": True, "source": EnterpriseSourceSerializer(source).data})


class GitHubRepositoryListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        credential_id = request.GET.get("credential_id")
        account = None
        if credential_id:
            credential = get_object_or_404(
                GitRepositoryCredential,
                pk=credential_id,
                organization=org,
            )
            token = credential.access_token
        else:
            try:
                account = _active_github_account(
                    user=request.user,
                    account_id=request.GET.get("github_account_id"),
                )
            except ValueError as exc:
                return JsonResponse(
                    {
                        "error": str(exc),
                        "connect_url": reverse("socialaccount_connections"),
                    },
                    status=400,
                )
            token = account.access_token

        try:
            response = requests.get(
                "https://api.github.com/user/repos",
                headers=_github_api_headers(token),
                params={"per_page": 100, "sort": "updated"},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return JsonResponse({"error": f"GitHub request failed: {exc}"}, status=502)

        repos = [
            {
                "id": item.get("id"),
                "full_name": item.get("full_name"),
                "name": item.get("name"),
                "default_branch": item.get("default_branch"),
                "private": item.get("private", False),
                "clone_url": item.get("clone_url"),
                "ssh_url": item.get("ssh_url"),
            }
            for item in response.json()
        ]
        return JsonResponse({"results": repos})


class GitHubBranchListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        full_name = (request.GET.get("full_name") or "").strip()
        credential_id = request.GET.get("credential_id")
        if not full_name:
            return JsonResponse({"error": "full_name is required."}, status=400)

        if credential_id:
            credential = get_object_or_404(
                GitRepositoryCredential,
                pk=credential_id,
                organization=org,
            )
            token = credential.access_token
        else:
            try:
                account = _active_github_account(
                    user=request.user,
                    account_id=request.GET.get("github_account_id"),
                )
            except ValueError as exc:
                return JsonResponse(
                    {
                        "error": str(exc),
                        "connect_url": reverse("socialaccount_connections"),
                    },
                    status=400,
                )
            token = account.access_token

        try:
            response = requests.get(
                f"https://api.github.com/repos/{full_name}/branches",
                headers=_github_api_headers(token),
                params={"per_page": 100},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return JsonResponse({"error": f"GitHub request failed: {exc}"}, status=502)

        branches = [
            {
                "name": item.get("name"),
                "protected": item.get("protected", False),
                "commit": (item.get("commit") or {}).get("sha", ""),
            }
            for item in response.json()
        ]
        return JsonResponse({"results": branches})


class OdooInstanceGitRepoCreateGitHubAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        payload = _request_data(request)
        repo_name = (payload.get("repo_name") or "").strip()
        if not repo_name:
            return JsonResponse({"error": "repo_name is required."}, status=400)

        try:
            actor = _github_publish_actor_from_payload(
                user=request.user,
                payload=payload,
            )
            repo_data, actor = _run_github_publish_with_saved_pat_fallback(
                org,
                actor,
                lambda publish_actor: _create_github_repository(actor=publish_actor, repo_name=repo_name),
            )
        except ValueError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "connect_url": reverse("socialaccount_connections"),
                },
                status=400,
            )
        except RuntimeError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "reconnect_url": reverse("socialaccount_connections"),
                },
                status=400,
            )

        try:
            credential = _credential_for_github_publish_actor(
                org=org,
                user=request.user,
                actor=actor,
                payload=payload,
                repo_name=repo_name,
            )
            linked_repo, _ = _register_instance_github_repo(
                org=org,
                user=request.user,
                instance=instance,
                repo_name=repo_data.get("name") or repo_name,
                git_url=repo_data.get("clone_url") or f"https://github.com/{repo_data.get('full_name')}.git",
                branch=repo_data.get("default_branch") or "main",
                credential=credential,
                auth_type=actor.auth_type,
            )
            if linked_repo.auto_update:
                _ensure_github_push_webhook(repo=linked_repo, request=request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        return JsonResponse(
            {
                "github_repo": {
                    "name": repo_data.get("name"),
                    "full_name": repo_data.get("full_name"),
                    "clone_url": repo_data.get("clone_url"),
                    "default_branch": repo_data.get("default_branch") or "main",
                    "html_url": repo_data.get("html_url"),
                    "private": repo_data.get("private", True),
                },
                "linked_repo": OdooInstanceGitRepoSerializer(linked_repo).data,
            },
            status=201,
        )


class OdooInstanceGitRepoUploadToGitHubAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        branch = (request.POST.get("branch") or "main").strip() or "main"
        zip_file = request.FILES.get("zip_file")
        full_name = (request.POST.get("full_name") or "").strip()
        clone_url = (request.POST.get("clone_url") or "").strip()
        repo_id = request.POST.get("repo_id")
        linked_repo = None
        if repo_id:
            linked_repo = get_object_or_404(
                OdooInstanceGitRepo.objects.select_related("credential"),
                pk=repo_id,
                instance=instance,
            )

        if linked_repo is not None:
            full_name = full_name or _github_full_name_from_git_url(linked_repo.git_url)
            clone_url = clone_url or linked_repo.git_url or _github_clone_url_for_full_name(full_name)

        repo_name = _derive_repo_name(
            request.POST.get("repo_name") or (linked_repo.repo_name if linked_repo is not None else ""),
            clone_url or full_name,
        )
        if not full_name:
            return JsonResponse({"error": "full_name is required."}, status=400)
        if not zip_file:
            return JsonResponse({"error": "zip_file is required."}, status=400)

        try:
            has_explicit_auth = any(
                (request.POST.get(key) or "").strip()
                for key in ("auth_type", "github_account_id", "access_token")
            )
            if linked_repo is not None and not has_explicit_auth:
                actor = _github_publish_actor_from_linked_repo(linked_repo)
            else:
                actor = _github_publish_actor_from_payload(
                    user=request.user,
                    payload=request.POST,
                )
        except ValueError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "connect_url": reverse("socialaccount_connections"),
                },
                status=400,
            )

        try:
            _, actor = _run_github_publish_with_saved_pat_fallback(
                org,
                actor,
                lambda publish_actor: _push_zip_to_github_repo(
                    actor=publish_actor,
                    user=request.user,
                    full_name=full_name,
                    zip_file=zip_file,
                    branch=branch,
                ),
            )
            if linked_repo is not None:
                credential = _credential_for_github_publish_actor(
                    org=org,
                    user=request.user,
                    actor=actor,
                    payload=request.POST,
                    repo_name=repo_name,
                )
                linked_repo.credential = credential
                linked_repo.repo_name = repo_name
                linked_repo.git_url = clone_url or f"https://github.com/{full_name}.git"
                linked_repo.branch = branch
                linked_repo.default_branch = branch
                linked_repo.auth_type = actor.auth_type
                linked_repo.local_path = _build_repo_local_path(instance, repo_name)
                linked_repo.last_error = ""
                linked_repo.save(
                    update_fields=[
                        "credential",
                        "repo_name",
                        "git_url",
                        "branch",
                        "default_branch",
                        "auth_type",
                        "local_path",
                        "last_error",
                        "updated_at",
                    ]
                )
                repo = linked_repo
                job = _repo_job(
                    org,
                    job_type=DeploymentJob.JobType.CLONE_INSTANCE_REPO,
                    instance=instance,
                    user=request.user,
                )
                _dispatch(clone_instance_repo, repo.id, job.id)
            elif getattr(actor, "credential", None) is not None:
                credential = actor.credential
                repo, job = _create_instance_repo_and_dispatch(
                    org=org,
                    user=request.user,
                    instance=instance,
                    repo_name=repo_name,
                    git_url=clone_url or _github_clone_url_for_full_name(full_name),
                    branch=branch,
                    auth_type=actor.auth_type,
                    credential=credential,
                )
            else:
                credential = _credential_for_github_publish_actor(
                    org=org,
                    user=request.user,
                    actor=actor,
                    payload=request.POST,
                    repo_name=repo_name,
                )
                repo, job = _create_instance_repo_and_dispatch(
                    org=org,
                    user=request.user,
                    instance=instance,
                    repo_name=repo_name,
                    git_url=clone_url or _github_clone_url_for_full_name(full_name),
                    branch=branch,
                    auth_type=actor.auth_type,
                    credential=credential,
                )
            if repo.auto_update:
                _ensure_github_push_webhook(repo=repo, request=request)
        except RuntimeError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "reconnect_url": reverse("socialaccount_connections"),
                },
                status=400,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        data = OdooInstanceGitRepoSerializer(repo).data
        data["job_id"] = job.id
        data["github_repo"] = {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
        }
        return JsonResponse(data, status=201)


@method_decorator(csrf_exempt, name="dispatch")
class GitHubWebhookAPIView(View):
    def post(self, request):
        if not _github_webhook_signature_valid(request):
            return JsonResponse({"error": "Invalid GitHub webhook signature."}, status=403)

        event = (request.headers.get("X-GitHub-Event") or "").strip().lower()
        payload = _request_data(request)
        if event == "ping":
            GitHubWebhookEvent.objects.create(
                repository="",
                status=GitHubWebhookEvent.Status.IGNORED,
                ignore_reason="ping",
            )
            return JsonResponse({"ok": True, "event": "ping"})
        if event != "push":
            GitHubWebhookEvent.objects.create(
                repository="",
                status=GitHubWebhookEvent.Status.IGNORED,
                ignore_reason=f"event={event or 'unknown'}",
            )
            return JsonResponse({"ok": True, "ignored": True, "event": event or "unknown"})

        full_name = ((payload.get("repository") or {}).get("full_name") or "").strip()
        branch = _github_branch_from_ref(payload.get("ref") or "")
        if not full_name or not branch:
            GitHubWebhookEvent.objects.create(
                repository=full_name,
                branch=branch,
                status=GitHubWebhookEvent.Status.ERROR,
                ignore_reason="Missing repository full_name or branch ref.",
            )
            return JsonResponse({"error": "Missing repository full_name or branch ref."}, status=400)

        head_commit = payload.get("head_commit") or {}
        commit_sha = (head_commit.get("id") or "")[:64]
        commit_message = (head_commit.get("message") or "")[:500]
        pusher_name = ((payload.get("pusher") or {}).get("name") or "")[:255]

        # Collect all pushed commits for history display
        raw_commits = payload.get("commits") or []
        commits_data = [
            {
                "sha": (c.get("id") or "")[:64],
                "message": (c.get("message") or "").split("\n")[0][:200],
                "author": ((c.get("author") or {}).get("name") or "")[:120],
                "timestamp": c.get("timestamp") or "",
            }
            for c in raw_commits[:50]
        ]

        repos = (
            OdooInstanceGitRepo.objects.select_related("instance", "instance__organization")
            .filter(
                auto_update=True,
                is_enabled=True,
                instance__status=OdooInstance.Status.RUNNING,
            )
            .exclude(status=OdooInstanceGitRepo.Status.CLONING)
        )

        matched_repo_ids: list[int] = []
        queued_repo_ids: list[int] = []
        for repo in repos:
            if repo.pinned_commit:
                continue
            if _github_full_name_from_git_url(repo.git_url).lower() != full_name.lower():
                continue
            if (repo.branch or "").strip() != branch:
                continue
            matched_repo_ids.append(repo.id)
            if repo.status == OdooInstanceGitRepo.Status.UPDATING:
                continue
            job = DeploymentJob.objects.create(
                organization=repo.instance.organization,
                job_type=DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
                odoo_instance=repo.instance,
                created_by=repo.created_by,
            )
            _dispatch(update_instance_repo, repo.id, job.id)
            queued_repo_ids.append(repo.id)

        GitHubWebhookEvent.objects.create(
            repository=full_name,
            branch=branch,
            head_commit_sha=commit_sha,
            head_commit_message=commit_message,
            pusher_name=pusher_name,
            status=GitHubWebhookEvent.Status.PROCESSED,
            matched_repo_ids=matched_repo_ids,
            queued_repo_ids=queued_repo_ids,
            commits_data=commits_data,
        )

        # Refresh staging TTL for any staging instance whose repo was just queued for update
        if queued_repo_ids:
            now = timezone.now()
            staging_envs = StagingEnvironment.objects.filter(
                staging_instance__git_repos__id__in=queued_repo_ids
            ).distinct()
            staging_envs.update(last_activity_at=now, updated_at=now)

        return JsonResponse(
            {
                "ok": True,
                "event": "push",
                "repository": full_name,
                "branch": branch,
                "matched_repo_ids": matched_repo_ids,
                "queued_repo_ids": queued_repo_ids,
            }
        )


class GitHubWebhookEventListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        qs = GitHubWebhookEvent.objects.all()
        repository = (request.GET.get("repository") or "").strip()
        branch = (request.GET.get("branch") or "").strip()
        if repository:
            qs = qs.filter(repository__iexact=repository)
        if branch:
            qs = qs.filter(branch=branch)
        events = qs[:100]
        data = GitHubWebhookEventSerializer(events, many=True).data
        return JsonResponse({"results": data})


class OdooInstanceConsoleView(LoginRequiredMixin, TemplateView):
    template_name = "deployments/odoo_instance_console.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not getattr(request, "organization", None):
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER", "USER"):
            messages.error(request, "You do not have permission to open this instance.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        instance = get_object_or_404(
            OdooInstance.objects.select_related("server", "enterprise_source"),
            pk=self.kwargs["instance_id"],
            organization=org,
        )
        ctx["odoo_instance"] = instance
        ctx["odoo_server"] = instance.server
        ctx["enterprise_active_source"] = EnterpriseSource.active_for_version(
            instance.server.odoo_version,
            scope=EnterpriseSource.Scope.PLATFORM,
        )
        ctx["enterprise_user_source"] = EnterpriseSource.latest_user_for_version(self.request.user, instance.server.odoo_version)
        repos = list(instance.git_repos.select_related("credential").all())
        ctx["instance_git_repos"] = repos
        ctx["instance_git_repos_json"] = json.dumps(
            OdooInstanceGitRepoSerializer(repos, many=True).data
        )
        ctx["installation_summary_text"] = instance.installation_summary_text or instance.server.installation_summary_text
        ctx["odoo_admin_password"] = instance.odoo_admin_password or ""
        staging_envs = (
            StagingEnvironment.objects.filter(source_instance=instance)
            .select_related("staging_instance", "staging_instance__server", "source_repo")
            .order_by("-created_at")
        )
        ctx["staging_envs_json"] = json.dumps(
            StagingEnvironmentSerializer(staging_envs, many=True).data,
            default=str,
        )
        ctx["is_staging"] = hasattr(instance, "staging_environment")
        ctx["staging_source"] = getattr(getattr(instance, "staging_environment", None), "source_instance", None)
        ctx["is_docker_server"] = instance.server.deployment_mode == OdooServer.DeploymentMode.DOCKER
        ctx["env_sections"] = ["Production", "Staging", "Development"]
        ctx["tool_tabs"] = [
            "GitHistory",
            "Shell",
            "Monitor",
            "logs",
            "backups",
            "Upgrade",
            "tools",
            "setting",
        ]
        return ctx


class OdooInstanceEnterpriseActivateAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if _repo_permission_denied(request):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        payload = _request_data(request)
        source_mode = (payload.get("source_mode") or instance.enterprise_source_mode or OdooInstance.EnterpriseSourceMode.PLATFORM).strip().upper()
        if source_mode not in OdooInstance.EnterpriseSourceMode.values:
            return JsonResponse({"error": "Unsupported Enterprise source mode."}, status=400)
        source = _enterprise_source_for_instance(instance=instance, user=request.user, source_mode=source_mode)
        if source is None:
            if source_mode == OdooInstance.EnterpriseSourceMode.USER:
                return JsonResponse({"error": f"No private Enterprise upload is available for Odoo {instance.server.odoo_version}."}, status=400)
            return JsonResponse({"error": f"No active Enterprise source is available for Odoo {instance.server.odoo_version}."}, status=400)

        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.ACTIVATE_ENTERPRISE,
            instance=instance,
            user=request.user,
        )
        _dispatch(activate_enterprise_for_instance, instance.id, source.id, job.id)
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.id,
                "enterprise_source_name": source.package_name,
                "enterprise_source_mode": source_mode,
                "enterprise_version": source.release_code,
                "enterprise_available_version": source.release_code,
                "enterprise_status": OdooInstance.EnterpriseStatus.PENDING,
            }
        )


class InfrastructureCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        name = (request.POST.get("name") or "").strip()
        infra_type = (request.POST.get("infra_type") or "").strip()
        if infra_type not in (Infrastructure.InfraType.PYOS, Infrastructure.InfraType.MANAGED):
            return JsonResponse({"error": "infra_type must be PYOS or MANAGED."}, status=400)
        if not name:
            return JsonResponse({"error": "name is required."}, status=400)

        ext = None
        account = None
        if infra_type == Infrastructure.InfraType.PYOS:
            from cloud.models import ExternalServer
            ext = get_object_or_404(ExternalServer, pk=request.POST.get("external_server_id"), organization=org)
        else:
            account = get_object_or_404(CloudAccount, pk=request.POST.get("cloud_account_id"), organization=org, is_verified=True)

        infra = Infrastructure.objects.create(
            organization=org,
            name=name,
            infra_type=infra_type,
            external_server=ext,
            cloud_account=account,
            is_connected=True,
            validation_log="Validated at creation.",
            created_by=request.user,
        )
        ok, err = infra.validate_connection_target()
        if not ok:
            infra.delete()
            return JsonResponse({"error": err}, status=400)
        return JsonResponse(InfrastructureSerializer(infra).data, status=201)


class InfrastructureListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        data = InfrastructureSerializer(
            Infrastructure.objects.filter(organization=org)[:100], many=True
        ).data
        return JsonResponse({"results": data})


class OdooInstanceDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"ok": True, "message": "Instance already deleted."})
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        # Capture everything needed for remote cleanup BEFORE deleting the DB record.
        # DomainAssignment rows survive the instance delete (SET_NULL FK) so we only
        # need their PKs — the Celery task fetches the full rows from DB.
        server = instance.server
        instance_pk = instance.pk
        db_name = instance.db_name
        http_port = instance.http_port
        assignment_ids = list(
            instance.domain_assignments
            .exclude(status="DELETED")
            .values_list("pk", flat=True)
        )

        try:
            # Synchronous DB delete — cascades to git repos, history, etc.
            instance.delete()
        except Exception as exc:
            logger.exception("OdooInstance %s delete failed", instance_id)
            return JsonResponse({"error": f"Delete failed: {exc}"}, status=500)

        _broadcast_instance_removed(instance_pk, server.pk if server else None)
        if server:
            _broadcast_server_snapshot(server)

        # Dispatch remote cleanup (non-blocking): stop service, drop DB,
        # remove Traefik route, remove Cloudflare DNS record.
        if server and server.ip_address:
            _dispatch(cleanup_deleted_instance, server.pk, db_name, http_port, assignment_ids)

        return JsonResponse({"ok": True, "message": "Instance permanently deleted."})


class OdooInstanceReprovisionAPIView(LoginRequiredMixin, View):
    """POST /odoo/instances/<instance_id>/reprovision/ — re-run instance creation on a FAILED/STOPPED instance."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server", "server__infrastructure"),
            pk=instance_id,
            organization=org,
        )

        retryable = {OdooInstance.Status.FAILED, OdooInstance.Status.STOPPED}
        if instance.status not in retryable:
            return JsonResponse(
                {"error": f"Instance must be FAILED or STOPPED to re-provision (current: {instance.status})."},
                status=409,
            )

        server = instance.server
        if not server:
            return JsonResponse({"error": "Instance has no server; cannot re-provision."}, status=400)
        if server.status != OdooServer.Status.PROVISIONED or not server.ip_address:
            return JsonResponse(
                {"error": f"Server must be PROVISIONED with an IP address before re-provisioning the instance (server status: {server.status})."},
                status=409,
            )

        instance.status = OdooInstance.Status.PENDING
        instance.provisioning_log = (instance.provisioning_log or "").rstrip("\n") + "\n--- Re-provision triggered ---"
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])

        _dispatch(create_odoo_instance, instance.id)

        logger.info(
            "Instance %s reprovision triggered by user %s (db=%s server=%s)",
            instance.id, request.user, instance.db_name, server.id,
        )
        return JsonResponse({"ok": True, "message": "Instance re-provisioning started."})


class OdooServerArchiveAPIView(LoginRequiredMixin, View):
    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer.objects.select_related(
                "infrastructure",
                "infrastructure__cloud_account",
                "cloud_account",
            ),
            pk=server_id,
            organization=org,
        )
        if not server.is_active:
            return JsonResponse({"ok": True, "message": "Server already archived."})

        # Cancel any running provisioning task before archiving.
        busy_statuses = {
            OdooServer.Status.CONNECTING,
            OdooServer.Status.PROVISIONING,
            OdooServer.Status.CONFIGURING,
        }
        if server.status in busy_statuses and server.celery_task_id:
            try:
                from dafeapp.celery import app as celery_app
                celery_app.control.revoke(server.celery_task_id, terminate=True, signal="SIGTERM")
            except Exception:
                pass

        try:
            server.is_active = False
            server.status = OdooServer.Status.ARCHIVED
            server.provisioning_log = (server.provisioning_log + "\n" + "Server archived (inactivated).").strip()
            server.save(update_fields=["is_active", "status", "provisioning_log", "updated_at"])
            _broadcast_server_event(server.id, {"type": "removed", "server_id": server.id, "reason": "archived"})
            return JsonResponse({"ok": True, "message": "Server archived and hidden from the UI."})

        except Exception as exc:
            logger.exception("Unexpected error archiving OdooServer %s", server_id)
            return JsonResponse({"error": f"Archive failed: {exc}"}, status=500)


class OdooServerActivateAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/servers/<id>/activate/ — restore an archived server."""

    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(OdooServer, pk=server_id, organization=org)

        if server.status == OdooServer.Status.DELETED:
            return JsonResponse({"error": "Deleted servers cannot be reactivated."}, status=400)

        if server.is_active and server.status != OdooServer.Status.ARCHIVED:
            return JsonResponse({"ok": True, "message": "Server is already active."})

        server.is_active = True
        server.status = OdooServer.Status.PROVISIONED
        server.provisioning_log = (server.provisioning_log + "\n" + "Server reactivated.").strip()
        server.save(update_fields=["is_active", "status", "provisioning_log", "updated_at"])
        return JsonResponse({"ok": True, "message": "Server reactivated."})


class OdooServerDeleteAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/servers/<id>/delete/ — permanent DB delete, like Django admin."""

    def post(self, request, server_id):
        org = getattr(request, "organization", None)

        if not org and getattr(request.user, "is_platform_admin", False):
            server = get_object_or_404(OdooServer, pk=server_id)
            org = server.organization
        elif not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        else:
            server = get_object_or_404(OdooServer, pk=server_id, organization=org)

        if request.org_role not in ("SUPER_ADMIN", "ADMIN") and not getattr(request.user, "is_platform_admin", False):
            return JsonResponse({"error": "Permission denied. Only SUPER_ADMIN or ADMIN can delete a server."}, status=403)

        # Cancel any running provisioning task before deleting.
        if server.celery_task_id:
            try:
                from dafeapp.celery import app as celery_app
                celery_app.control.revoke(server.celery_task_id, terminate=True, signal="SIGTERM")
            except Exception:
                pass

        server_pk = server.pk
        try:
            # Delete directly — cascades to instances, SSH keys, history, jobs
            # exactly as Django admin does.
            server.delete()
        except Exception as exc:
            logger.exception("OdooServer %s delete failed", server_id)
            return JsonResponse({"error": f"Delete failed: {exc}"}, status=500)

        _broadcast_server_event(server_pk, {"type": "removed", "server_id": server_pk, "reason": "deleted"})
        return JsonResponse({"ok": True, "message": "Server permanently deleted."})


class InfrastructureDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, infrastructure_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        force = str(request.POST.get("force", "")).lower() in ("1", "true", "yes")
        infra = get_object_or_404(Infrastructure, pk=infrastructure_id, organization=org)
        servers = infra.servers.all()
        if servers.exists() and not force:
            return JsonResponse(
                {"error": "Infrastructure has servers. Set force=true to delete recursively."},
                status=400,
            )
        if force:
            for server in servers:
                for inst in server.instances.exclude(status=OdooInstance.Status.DELETED):
                    inst.status = OdooInstance.Status.DELETED
                    inst.provisioning_log = (inst.provisioning_log + "\n" + "Deleted due to infrastructure force delete.").strip()
                    inst.save(update_fields=["status", "provisioning_log", "updated_at"])
                server.delete()
        infra.delete()
        return JsonResponse({"ok": True, "message": "Infrastructure deleted."})


# ---------------------------------------------------------------------------
# Phase 2: Deployment Jobs, History, Health Check, Rollback
# ---------------------------------------------------------------------------

class DeploymentJobListAPIView(LoginRequiredMixin, View):
    """GET /deployments/jobs/ — list recent deployment jobs for the active org."""

    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        qs = DeploymentJob.objects.filter(organization=org).order_by("-created_at")
        instance_id = request.GET.get("instance_id")
        server_id = request.GET.get("server_id")
        if instance_id:
            qs = qs.filter(odoo_instance_id=instance_id)
        if server_id:
            qs = qs.filter(odoo_server_id=server_id)
        return JsonResponse({"results": DeploymentJobSerializer(qs[:100], many=True).data})


class DeploymentJobCancelAPIView(LoginRequiredMixin, View):
    """POST /deployments/jobs/<id>/cancel/ — revoke a running Celery task and mark it cancelled."""

    def post(self, request, job_id):
        from celery import current_app

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        job = get_object_or_404(DeploymentJob, pk=job_id, organization=org)
        if job.status not in (DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING):
            return JsonResponse({"error": f"Job is already {job.status}."}, status=400)

        if job.celery_task_id:
            try:
                current_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
            except Exception:
                logger.warning("Could not revoke Celery task %s", job.celery_task_id, exc_info=True)

        from django.utils import timezone
        job.status = DeploymentJob.Status.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "updated_at"])
        return JsonResponse({"ok": True, "status": job.status})


class OdooServerHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/servers/<id>/history/ — deployment history for a server."""

    def get(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        qs = OdooServerHistory.objects.filter(server=server).order_by("-deployed_at")
        return JsonResponse({"results": OdooServerHistorySerializer(qs, many=True).data})


class OdooServerDockerCleanupAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/servers/<id>/docker-cleanup/ — current cleanup stats and category counts."""

    def get(self, request, server_id):
        server, err = _get_docker_cleanup_server(request, server_id)
        if err:
            return err

        age_days = _docker_cleanup_age_days(request.GET.get("age_days"))
        try:
            preview = collect_docker_cleanup_preview(server, age_days=age_days)
        except Exception as exc:
            logger.warning("Docker cleanup stats failed for server %s: %s", server.id, exc)
            return JsonResponse({"error": str(exc)}, status=502)

        last_cleanup = server.docker_cleanup_runs.filter(status=DockerCleanupRun.Status.DONE).order_by("-started_at").first()
        summary = {}
        for key, row in (preview.get("summary") or {}).items():
            summary[key] = {
                **row,
                "label": _docker_cleanup_labels([key])[0] if key in _normalized_docker_cleanup_types([key]) else key,
                "estimated_reclaimable_display": _format_bytes_human(row.get("estimated_reclaimable_bytes")),
            }
        disk = preview.get("disk") or {}
        total_bytes = int(disk.get("total_bytes") or 0)
        payload = {
            "server_id": server.id,
            "age_threshold_days": age_days,
            "stopped_containers": int(preview.get("stopped_containers") or 0),
            "reclaimable_bytes": int(preview.get("reclaimable_bytes") or 0),
            "reclaimable_display": _format_bytes_human(preview.get("reclaimable_bytes")),
            "last_cleanup_at": last_cleanup.started_at.isoformat() if last_cleanup else "",
            "last_cleanup_display": timezone.localtime(last_cleanup.started_at).strftime("%b %d, %Y") if last_cleanup else "Never",
            "disk": {
                **disk,
                "used_display": _format_bytes_human(disk.get("used_bytes")),
                "available_display": _format_bytes_human(disk.get("available_bytes")),
                "total_display": _format_bytes_human(total_bytes),
                "database_display": _format_bytes_human(disk.get("database_bytes")),
                "filestore_display": _format_bytes_human(disk.get("filestore_bytes")),
                "logs_display": _format_bytes_human(disk.get("logs_bytes")),
                "database_percent": round((int(disk.get("database_bytes") or 0) / total_bytes) * 100, 1) if total_bytes else 0,
                "filestore_percent": round((int(disk.get("filestore_bytes") or 0) / total_bytes) * 100, 1) if total_bytes else 0,
                "logs_percent": round((int(disk.get("logs_bytes") or 0) / total_bytes) * 100, 1) if total_bytes else 0,
            },
            "summary": summary,
        }
        return JsonResponse(payload)


class OdooServerDockerCleanupPreviewAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/servers/<id>/docker-cleanup/preview/ — inspect what would be removed."""

    def post(self, request, server_id):
        server, err = _get_docker_cleanup_server(request, server_id)
        if err:
            return err

        payload = _request_data(request)
        age_days = _docker_cleanup_age_days(payload.get("age_days"))
        cleanup_types = _normalized_docker_cleanup_types(
            payload.get("cleanup_types") or request.POST.getlist("cleanup_types")
        )
        if not cleanup_types:
            return JsonResponse({"error": "Select at least one cleanup type."}, status=400)

        try:
            preview = collect_docker_cleanup_preview(server, age_days=age_days)
        except Exception as exc:
            logger.warning("Docker cleanup preview failed for server %s: %s", server.id, exc)
            return JsonResponse({"error": str(exc)}, status=502)

        summary = preview.get("summary") or {}
        selected = {}
        items_deleted = 0
        reclaimable_bytes = 0
        for key in cleanup_types:
            row = summary.get(key) or {"count": 0, "estimated_reclaimable_bytes": 0, "items": []}
            selected[key] = {
                **row,
                "label": _docker_cleanup_labels([key])[0],
                "estimated_reclaimable_display": _format_bytes_human(row.get("estimated_reclaimable_bytes")),
            }
            items_deleted += int(row.get("count") or 0)
            reclaimable_bytes += int(row.get("estimated_reclaimable_bytes") or 0)

        return JsonResponse(
            {
                "server_id": server.id,
                "age_threshold_days": age_days,
                "cleanup_types": cleanup_types,
                "cleanup_type_labels": _docker_cleanup_labels(cleanup_types),
                "items_deleted": items_deleted,
                "estimated_reclaimable_bytes": reclaimable_bytes,
                "estimated_reclaimable_display": _format_bytes_human(reclaimable_bytes),
                "summary": selected,
            }
        )


class OdooServerDockerCleanupExecuteAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/servers/<id>/docker-cleanup/execute/ — execute cleanup now."""

    def post(self, request, server_id):
        server, err = _get_docker_cleanup_server(request, server_id)
        if err:
            return err

        payload = _request_data(request)
        age_days = _docker_cleanup_age_days(payload.get("age_days"))
        cleanup_types = _normalized_docker_cleanup_types(
            payload.get("cleanup_types") or request.POST.getlist("cleanup_types")
        )
        if not cleanup_types:
            return JsonResponse({"error": "Select at least one cleanup type."}, status=400)

        run = DockerCleanupRun.objects.create(
            organization=server.organization,
            server=server,
            status=DockerCleanupRun.Status.RUNNING,
            cleanup_types=cleanup_types,
            age_threshold_days=age_days,
            created_by=request.user,
        )
        started_at = timezone.now()

        try:
            result = execute_docker_cleanup(server, cleanup_types, age_days=age_days)
            finished_at = timezone.now()
            run.status = DockerCleanupRun.Status.DONE
            run.items_deleted = int(result.get("items_deleted") or 0)
            run.space_freed_bytes = int(result.get("space_freed_bytes") or 0)
            run.duration_seconds = max(1, int((finished_at - started_at).total_seconds()))
            run.summary = result.get("type_results") or {}
            run.command_log = (result.get("log") or "")[:20000]
            run.finished_at = finished_at
            run.error_message = ""
            run.save(
                update_fields=[
                    "status",
                    "items_deleted",
                    "space_freed_bytes",
                    "duration_seconds",
                    "summary",
                    "command_log",
                    "finished_at",
                    "error_message",
                    "updated_at",
                ]
            )
            log_audit(
                request.user,
                AuditLog.Action.OTHER,
                None,
                f"Docker cleanup ran on server '{server.name}'.",
                metadata={
                    "server_id": server.id,
                    "cleanup_types": cleanup_types,
                    "age_threshold_days": age_days,
                    "items_deleted": run.items_deleted,
                    "space_freed_bytes": run.space_freed_bytes,
                },
                organization=server.organization,
            )
        except Exception as exc:
            finished_at = timezone.now()
            run.status = DockerCleanupRun.Status.FAILED
            run.duration_seconds = max(1, int((finished_at - started_at).total_seconds()))
            run.finished_at = finished_at
            run.error_message = str(exc)
            run.save(update_fields=["status", "duration_seconds", "finished_at", "error_message", "updated_at"])
            logger.warning("Docker cleanup failed for server %s: %s", server.id, exc)
            return JsonResponse({"error": str(exc), "run": _docker_cleanup_row_payload(run)}, status=502)

        return JsonResponse({"ok": True, "run": _docker_cleanup_row_payload(run)})


class OdooServerDockerCleanupHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/servers/<id>/docker-cleanup/history/ — cleanup execution history."""

    def get(self, request, server_id):
        server, err = _get_docker_cleanup_server(request, server_id)
        if err:
            return err

        rows = [
            _docker_cleanup_row_payload(run)
            for run in server.docker_cleanup_runs.select_related("created_by").all()[:50]
        ]
        return JsonResponse({"results": rows})


class OdooServerDockerCleanupExportAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/servers/<id>/docker-cleanup/export/ — export cleanup history as CSV."""

    def get(self, request, server_id):
        server, err = _get_docker_cleanup_server(request, server_id)
        if err:
            return err

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="server-{server.id}-docker-cleanup-history.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "timestamp",
                "status",
                "cleanup_types",
                "age_threshold_days",
                "items_deleted",
                "space_freed_bytes",
                "space_freed_display",
                "duration_seconds",
                "user",
                "error_message",
            ]
        )
        for run in server.docker_cleanup_runs.select_related("created_by").all():
            payload = _docker_cleanup_row_payload(run)
            writer.writerow(
                [
                    payload["started_at"],
                    payload["status"],
                    payload["cleanup_types_label"],
                    payload["age_threshold_days"],
                    payload["items_deleted"],
                    payload["space_freed_bytes"],
                    payload["space_freed_display"],
                    payload["duration_seconds"] or "",
                    payload["created_by_name"],
                    payload["error_message"],
                ]
            )
        return response


class OdooInstanceHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/instances/<id>/history/ — deployment history for an instance."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        repo_qs = list(
            OdooInstanceGitRepo.objects.filter(instance=instance).only(
                "id", "repo_name", "branch", "git_url"
            )
        )
        repo_ids = {repo.id for repo in repo_qs}
        repo_map = {repo.id: repo for repo in repo_qs}

        timeline = []

        snapshots = (
            OdooInstanceHistory.objects.filter(instance=instance)
            .select_related("deployed_by")
            .order_by("-deployed_at")[:50]
        )
        for snap in snapshots:
            access_label = snap.domain or f":{snap.http_port}"
            timeline.append(
                {
                    "id": f"snapshot-{snap.id}",
                    "event_type": "snapshot",
                    "event_label": "Deployment",
                    "title": snap.note or "Deployment snapshot saved.",
                    "details": f"Odoo {snap.odoo_version}.0 · {access_label} · {snap.status}",
                    "status": snap.status,
                    "timestamp": snap.deployed_at.isoformat(),
                    "actor": _timeline_actor_label(snap.deployed_by),
                    "history_id": snap.id,
                    "can_rollback": True,
                }
            )

        backups = (
            OdooInstanceBackup.objects.filter(instance=instance)
            .select_related("created_by")
            .order_by("-created_at")[:50]
        )
        for backup in backups:
            backup_note = (backup.note or "").strip()
            title = backup_note or (
                "Backup created successfully."
                if backup.status == OdooInstanceBackup.Status.DONE
                else "Backup in progress."
                if backup.status in (OdooInstanceBackup.Status.RUNNING, OdooInstanceBackup.Status.PENDING)
                else "Backup failed."
            )
            details = f"{backup.get_backup_type_display()} · {backup.size_display if backup.size_bytes else 'size pending'}"
            timeline.append(
                {
                    "id": f"backup-{backup.id}",
                    "event_type": "backup",
                    "event_label": "Backup",
                    "title": title,
                    "details": details,
                    "status": backup.status,
                    "timestamp": backup.created_at.isoformat(),
                    "actor": _timeline_actor_label(backup.created_by),
                    "history_id": None,
                    "can_rollback": False,
                }
            )

        history_job_types = (
            DeploymentJob.JobType.RESTORE_INSTANCE,
            DeploymentJob.JobType.ROLLBACK_INSTANCE,
            DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
            DeploymentJob.JobType.ROLLBACK_INSTANCE_REPO,
            DeploymentJob.JobType.CHECKOUT_INSTANCE_REPO_BRANCH,
            DeploymentJob.JobType.CLONE_INSTANCE_REPO,
            DeploymentJob.JobType.AUTO_SYNC_INSTANCE_REPOS,
        )
        jobs = (
            DeploymentJob.objects.filter(organization=org, odoo_instance=instance, job_type__in=history_job_types)
            .select_related("created_by")
            .order_by("-created_at")[:75]
        )
        for job in jobs:
            details = _timeline_last_log_line(job.log) or job.get_job_type_display()
            timeline.append(
                {
                    "id": f"job-{job.id}",
                    "event_type": "job",
                    "event_label": _timeline_job_title(job),
                    "title": _timeline_job_title(job),
                    "details": details,
                    "status": job.status,
                    "timestamp": (job.finished_at or job.created_at).isoformat(),
                    "actor": _timeline_actor_label(job.created_by),
                    "history_id": None,
                    "can_rollback": False,
                }
            )

        webhook_events = list(GitHubWebhookEvent.objects.order_by("-received_at")[:200])
        for event in webhook_events:
            matched_ids = set((event.matched_repo_ids or []) + (event.queued_repo_ids or []))
            if repo_ids and not matched_ids.intersection(repo_ids):
                continue
            matched_repos = []
            for repo_id in event.queued_repo_ids or event.matched_repo_ids or []:
                repo = repo_map.get(repo_id)
                if repo:
                    matched_repos.append(f"{repo.repo_name}:{repo.branch}")
            repo_label = ", ".join(matched_repos[:2]) if matched_repos else event.repository
            sha = (event.head_commit_sha or "")[:8] or "—"
            commits_count = len(event.commits_data) if event.commits_data else (1 if event.head_commit_sha else 0)
            if commits_count > 1:
                title = f"{commits_count} commits pushed to {event.branch or 'unknown branch'}"
            elif commits_count == 1:
                title = event.head_commit_message or f"1 commit pushed to {event.branch}"
            else:
                title = event.head_commit_message or f"GitHub push on {event.branch}"
            details = f"{repo_label} · {event.branch or 'unknown branch'} · {sha}"
            timeline.append(
                {
                    "id": f"github-{event.id}",
                    "event_type": "github",
                    "event_label": "GitHub",
                    "title": title,
                    "details": details,
                    "commits_count": commits_count,
                    "commits_data": event.commits_data or [],
                    "status": event.status,
                    "timestamp": event.received_at.isoformat(),
                    "actor": event.pusher_name or "GitHub",
                    "history_id": None,
                    "can_rollback": False,
                }
            )

        timeline.sort(key=lambda item: item["timestamp"], reverse=True)
        return JsonResponse({"results": timeline[:150]})


class OdooInstanceRollbackAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/rollback/ — re-deploy from a history snapshot."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        history_id = request.POST.get("history_id")
        if not history_id:
            return JsonResponse({"error": "history_id is required."}, status=400)
        snap = get_object_or_404(OdooInstanceHistory, pk=history_id, instance=instance)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.ROLLBACK_INSTANCE,
            odoo_instance=instance,
            created_by=request.user,
        )
        _dispatch(rollback_odoo_instance, instance.id, snap.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id, "history_id": snap.id})


class OdooInstanceHealthCheckView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/health/ — manual HTTP health probe."""

    def post(self, request, instance_id):
        import urllib.error
        import urllib.request
        from django.utils import timezone

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if not instance.server.ip_address:
            return JsonResponse({"error": "Server has no IP yet."}, status=400)

        url = f"http://{instance.server.ip_address}:{instance.http_port}/web/health"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                reachable = resp.status == 200
        except Exception:
            reachable = False

        now = timezone.now()
        instance.is_reachable = reachable
        instance.last_health_check = now
        instance.save(update_fields=["is_reachable", "last_health_check"])
        return JsonResponse({
            "is_reachable": reachable,
            "last_health_check": now.isoformat(),
            "url_probed": url,
        })


class OdooInstanceCommandsAPIView(LoginRequiredMixin, View):
    """GET /api/deployments/odoo/instances/<id>/commands/ — generated shell commands for this instance."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related(
                "server",
                "server__infrastructure",
                "server__infrastructure__external_server",
            ),
            pk=instance_id,
            organization=org,
        )
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"error": "Instance is deleted."}, status=400)

        from deployments.tasks import _instance_shell_commands
        commands = _instance_shell_commands(instance)
        return JsonResponse({"commands": commands})


class OdooInstanceUpdateModulesAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/instances/<id>/maintenance/update-modules/ — update all Odoo modules."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"error": "Instance is deleted."}, status=400)
        if not instance.server.is_reachable:
            return JsonResponse(
                {"error": "Server is not reachable. Check connectivity before running updates."},
                status=409,
            )
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.UPDATE_MODULES_ALL,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(update_instance_modules_all, instance.id, job.id)

        try:
            from audit.models import AuditLog
            AuditLog.objects.create(
                user=request.user,
                organization=org,
                action=AuditLog.Action.ODOO_UPDATE_MODULES,
                description=f"Queued update all modules for instance {instance.name!r}",
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                metadata={"instance_id": instance.id, "job_id": job.id},
            )
        except Exception:
            pass

        return JsonResponse({"ok": True, "job_id": job.id})


class OdooInstanceRestartAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/instances/<id>/maintenance/restart/ — restart the Odoo service."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"error": "Instance is deleted."}, status=400)
        if not instance.server.is_reachable:
            return JsonResponse({"error": "Server is not reachable."}, status=409)
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.RESTART_INSTANCE,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(restart_odoo_instance, instance.id, job.id)

        try:
            from audit.models import AuditLog
            AuditLog.objects.create(
                user=request.user,
                organization=org,
                action=AuditLog.Action.ODOO_RESTART_INSTANCE,
                description=f"Queued restart for instance {instance.name!r}",
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                metadata={"instance_id": instance.id, "job_id": job.id},
            )
        except Exception:
            pass

        return JsonResponse({"ok": True, "job_id": job.id})


class OdooInstanceStopAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/instances/<id>/maintenance/stop/ — stop the Odoo service."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if instance.status in (OdooInstance.Status.DELETED, OdooInstance.Status.STOPPED):
            return JsonResponse({"error": f"Instance is already {instance.status.lower()}."}, status=400)
        lock_reason = _instance_mutation_lock_reason(instance)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.RESTART_INSTANCE,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(stop_odoo_instance, instance.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id})


class StagingEnvironmentListAPIView(LoginRequiredMixin, View):
    """GET /api/deployments/odoo/instances/<id>/staging/ — list staging envs for a source instance."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        staging_envs = (
            StagingEnvironment.objects.filter(source_instance=instance)
            .select_related("staging_instance", "staging_instance__server")
            .order_by("-created_at")
        )
        return JsonResponse({"results": StagingEnvironmentSerializer(staging_envs, many=True).data})


class StagingEnvironmentCreateAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/instances/<id>/staging/create/ — create a staging instance from a branch."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if instance.status != OdooInstance.Status.RUNNING:
            return JsonResponse({"error": "Source instance must be RUNNING."}, status=400)
        if instance.server.deployment_mode != OdooServer.DeploymentMode.DOCKER:
            return JsonResponse({"error": "Staging is only supported on Docker servers."}, status=400)

        data = _request_data(request)
        repo_id = data.get("repo_id")
        branch = (data.get("branch") or "").strip()
        ttl_days = int(data.get("ttl_days") or 7)
        auto_delete = bool(data.get("auto_delete", True))

        if not repo_id or not branch:
            return JsonResponse({"error": "repo_id and branch are required."}, status=400)

        try:
            source_repo = OdooInstanceGitRepo.objects.get(pk=repo_id, instance=instance)
        except OdooInstanceGitRepo.DoesNotExist:
            return JsonResponse({"error": "Git repo not found on this instance."}, status=404)

        # Idempotency check
        if StagingEnvironment.objects.filter(source_repo=source_repo, branch=branch).exclude(
            staging_instance__status=OdooInstance.Status.DELETED
        ).exists():
            return JsonResponse({"error": f"Staging environment for branch '{branch}' already exists."}, status=409)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.CREATE_STAGING_INSTANCE,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(create_staging_instance, instance.id, source_repo.id, branch, ttl_days, auto_delete, job.id)
        return JsonResponse({"ok": True, "job_id": job.id}, status=202)


class StagingEnvironmentDetailAPIView(LoginRequiredMixin, View):
    """GET /api/deployments/odoo/staging/<staging_id>/ — detail for one staging env."""

    def get(self, request, staging_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        staging_env = get_object_or_404(
            StagingEnvironment.objects.select_related("staging_instance", "staging_instance__server"),
            pk=staging_id,
            staging_instance__organization=org,
        )
        return JsonResponse(StagingEnvironmentSerializer(staging_env).data)


class StagingEnvironmentDeleteAPIView(LoginRequiredMixin, View):
    """POST /api/deployments/odoo/staging/<staging_id>/delete/ — delete a staging env."""

    def post(self, request, staging_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        staging_env = get_object_or_404(
            StagingEnvironment,
            pk=staging_id,
            staging_instance__organization=org,
        )
        # Disable auto-delete to prevent TTL re-queue racing this manual delete
        staging_env.auto_delete_enabled = False
        staging_env.save(update_fields=["auto_delete_enabled", "updated_at"])

        _dispatch(delete_odoo_instance, staging_env.staging_instance_id)
        return JsonResponse({"ok": True, "message": "Staging instance deletion queued."}, status=202)


class ServerSSHKeyListCreateAPIView(LoginRequiredMixin, View):
    """
    GET  /api/deployments/odoo/servers/<id>/ssh-keys/  — list keys for a server
    POST /api/deployments/odoo/servers/<id>/ssh-keys/  — add a new key and deploy it
    """

    def _get_server(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return None, JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        lock_reason = _server_mutation_lock_reason(server)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        return server, None

    def get(self, request, server_id):
        server, err = self._get_server(request, server_id)
        if err:
            return err
        keys = server.ssh_keys.all().values("id", "label", "public_key", "deployed", "created_at")
        return JsonResponse({"keys": list(keys)})

    def post(self, request, server_id):
        import json as _json
        server, err = self._get_server(request, server_id)
        if err:
            return err

        try:
            body = _json.loads(request.body)
        except Exception:
            body = request.POST

        label = (body.get("label") or "").strip()
        public_key = (body.get("public_key") or "").strip()
        if not label:
            return JsonResponse({"error": "Label is required."}, status=400)
        if not public_key:
            return JsonResponse({"error": "Public key is required."}, status=400)
        if not (public_key.startswith("ssh-") or public_key.startswith("ecdsa-")):
            return JsonResponse({"error": "Invalid public key format."}, status=400)
        if ServerSSHKey.objects.filter(server=server, public_key=public_key).exists():
            return JsonResponse({"error": "This key is already registered on this server."}, status=400)

        key_obj = ServerSSHKey.objects.create(
            server=server,
            label=label,
            public_key=public_key,
            added_by=request.user,
        )

        if server.status == OdooServer.Status.PROVISIONED and server.ip_address:
            _dispatch(deploy_server_ssh_key, key_obj.pk)
            message = "Key added and deployment queued."
        else:
            message = "Key saved. It will be deployed when the server is provisioned."

        return JsonResponse({
            "id": key_obj.pk,
            "label": key_obj.label,
            "deployed": key_obj.deployed,
            "message": message,
        }, status=201)


class ServerSSHKeyDeleteAPIView(LoginRequiredMixin, View):
    """DELETE /api/deployments/odoo/servers/<id>/ssh-keys/<key_id>/"""

    def post(self, request, server_id, key_id):
        import json as _json
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        lock_reason = _server_mutation_lock_reason(server)
        if lock_reason:
            return JsonResponse({"error": lock_reason}, status=409)
        key_obj = get_object_or_404(ServerSSHKey, pk=key_id, server=server)
        key_obj.delete()
        return JsonResponse({"deleted": True})


# ── Odoo Admin Login Relay ────────────────────────────────────────────────────

import secrets as _secrets
from django.core.cache import cache as _cache

_LOGIN_TOKEN_TTL = 60  # seconds — token is single-use and short-lived


class OdooInstanceClearAdminPasswordAPIView(LoginRequiredMixin, View):
    """
    POST /api/deployments/odoo/instances/<id>/clear-admin-password/

    Wipes the stored admin password from the DB after the operator has
    acknowledged and saved it.  The password is then never shown again.
    """

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        instance.odoo_admin_password = ""
        instance.save(update_fields=["odoo_admin_password", "updated_at"])
        return JsonResponse({"ok": True})


class OdooAdminLoginAPIView(LoginRequiredMixin, View):
    """
    POST /api/deployments/odoo/instances/<id>/admin-login/

    Generates a short-lived one-time relay token.  The caller should open
    the returned relay_url in a new tab: that page auto-submits an HTML
    form directly to Odoo's /web/login, logging the user in as admin
    without the password being visible in the DafeApp UI.
    """

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org and getattr(request.user, "is_platform_admin", False):
            from deployments.models import OdooInstance
            instance = get_object_or_404(OdooInstance, pk=instance_id)
        elif not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        else:
            instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)

        if not instance.odoo_admin_password:
            return JsonResponse(
                {
                    "error": (
                        "Admin login credentials are not available for this instance. "
                        "Re-provision the instance to generate them automatically."
                    )
                },
                status=404,
            )

        odoo_url = (instance.preferred_access_url or "").rstrip("/")
        if not odoo_url:
            return JsonResponse(
                {"error": "No accessible URL found for this instance."},
                status=400,
            )

        token = _secrets.token_urlsafe(32)
        _cache.set(
            f"odoo_login_token:{token}",
            {
                "instance_id": instance.pk,
                "odoo_url":    odoo_url,
                "db_name":     instance.db_name,
                "login":       "admin",
                "password":    instance.odoo_admin_password,
            },
            timeout=_LOGIN_TOKEN_TTL,
        )

        relay_url = reverse("deployments:odoo-admin-login-relay") + f"?t={token}"
        return JsonResponse({"relay_url": relay_url})


class OdooInstanceUsersAPIView(LoginRequiredMixin, View):
    """
    GET /api/deployments/odoo/instances/<id>/odoo-users/

    Returns the list of internal (non-portal) active Odoo users for the
    user-picker in the console.  Fetches directly from the instance DB via SSH.
    """

    def get(self, request, instance_id):
        from deployments.tasks import _ssh_run

        org = getattr(request, "organization", None)
        if not org and getattr(request.user, "is_platform_admin", False):
            instance = get_object_or_404(OdooInstance, pk=instance_id)
        elif not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        else:
            instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)

        server  = instance.server
        db_name = instance.db_name

        sql = (
            "SELECT id, name, login FROM res_users "
            "WHERE active = true AND share = false "
            "ORDER BY name"
        )

        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            cmd = (
                f"docker exec odoo-postgres psql -U odoo -d {shlex.quote(db_name)} "
                f"-tAF'|' -c {shlex.quote(sql)}"
            )
        else:
            cmd = (
                f"sudo -u postgres psql -d {shlex.quote(db_name)} "
                f"-tAF'|' -c {shlex.quote(sql)}"
            )

        try:
            code, output = _ssh_run(server, cmd, timeout=30)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        if code != 0:
            return JsonResponse({"error": output[:500]}, status=502)

        users = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                users.append({
                    "id":    parts[0].strip(),
                    "name":  parts[1].strip(),
                    "login": parts[2].strip(),
                })

        return JsonResponse({"results": users})


class OdooLoginAsUserAPIView(LoginRequiredMixin, View):
    """
    POST /api/deployments/odoo/instances/<id>/login-as/
    Body: {"user_id": 7}

    Generates a temporary password for the selected Odoo user (writes hash to DB
    via SSH), then returns a short-lived relay URL that auto-submits a login form
    to Odoo's /web/login endpoint.
    """

    def post(self, request, instance_id):
        from deployments.tasks import _ssh_run

        org = getattr(request, "organization", None)
        if not org and getattr(request.user, "is_platform_admin", False):
            instance = get_object_or_404(OdooInstance, pk=instance_id)
        elif not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        else:
            instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)

        try:
            payload = json.loads(request.body)
            user_id = int(payload.get("user_id", 0))
        except Exception:
            return JsonResponse({"error": "Invalid request body."}, status=400)

        if not user_id:
            return JsonResponse({"error": "user_id is required."}, status=400)

        odoo_url = (instance.preferred_access_url or "").rstrip("/")
        if not odoo_url:
            return JsonResponse({"error": "No accessible URL found for this instance."}, status=400)

        server  = instance.server
        db_name = instance.db_name

        # ── 1. Fetch the user's login name ──────────────────────────────────
        sql_get = (
            f"SELECT login FROM res_users "
            f"WHERE id = {int(user_id)} AND active = true AND share = false"
        )
        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            cmd_get = (
                f"docker exec odoo-postgres psql -U odoo -d {shlex.quote(db_name)} "
                f"-tAc {shlex.quote(sql_get)}"
            )
        else:
            cmd_get = (
                f"sudo -u postgres psql -d {shlex.quote(db_name)} "
                f"-tAc {shlex.quote(sql_get)}"
            )

        try:
            code, output = _ssh_run(server, cmd_get, timeout=20)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        user_login = output.strip()
        if code != 0 or not user_login:
            return JsonResponse({"error": "User not found or is not an internal user."}, status=404)

        # ── 2. Generate a temp password (alphanumeric only — safe for shell) ─
        temp_pw = _secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")

        # ── 3. Hash the password using Odoo's Python env on the server ───────
        hash_script = (
            "from passlib.context import CryptContext; "
            f"print(CryptContext(['pbkdf2_sha512']).hash('{temp_pw}'))"
        )
        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            odoo_ver = server.odoo_version or "17"
            cmd_hash = f"docker run --rm odoo:{odoo_ver} python3 -c {shlex.quote(hash_script)}"
        else:
            venv_py = f"/odoo/instances/{db_name}/venv/bin/python3"
            cmd_hash = f"sudo -u odoo {venv_py} -c {shlex.quote(hash_script)}"

        try:
            code, pw_hash = _ssh_run(server, cmd_hash, timeout=60)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        pw_hash = pw_hash.strip()
        if code != 0 or not pw_hash:
            return JsonResponse({"error": "Failed to hash password on server."}, status=502)

        # ── 4. Write hash to DB ───────────────────────────────────────────────
        # pbkdf2_sha512 output contains only base64 chars — no single quotes
        sql_upd = (
            f"UPDATE res_users SET password = '{pw_hash}' "
            f"WHERE id = {int(user_id)}"
        )
        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            cmd_upd = (
                f"docker exec odoo-postgres psql -U odoo -d {shlex.quote(db_name)} "
                f"-c {shlex.quote(sql_upd)}"
            )
        else:
            cmd_upd = (
                f"sudo -u postgres psql -d {shlex.quote(db_name)} "
                f"-c {shlex.quote(sql_upd)}"
            )

        try:
            code, _ = _ssh_run(server, cmd_upd, timeout=20)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        if code != 0:
            return JsonResponse({"error": "Failed to set temporary password on server."}, status=502)

        # ── 5. Create one-time relay token ────────────────────────────────────
        token = _secrets.token_urlsafe(32)
        _cache.set(
            f"odoo_login_token:{token}",
            {
                "instance_id": instance.pk,
                "odoo_url":    odoo_url,
                "db_name":     db_name,
                "login":       user_login,
                "password":    temp_pw,
            },
            timeout=_LOGIN_TOKEN_TTL,
        )

        relay_url = reverse("deployments:odoo-admin-login-relay") + f"?t={token}"
        return JsonResponse({"relay_url": relay_url})


class OdooAdminLoginRelayView(LoginRequiredMixin, View):
    """
    GET /deployments/odoo/instances/login-relay/?t=<token>

    Validates the one-time token and returns an HTML page that immediately
    auto-submits a form to Odoo's /web/login.  Because the form POSTs
    directly to the Odoo domain, the browser sets the session cookie there
    — no cross-domain cookie hacks required.
    """

    def get(self, request):
        token = request.GET.get("t", "").strip()
        cache_key = f"odoo_login_token:{token}"
        data = _cache.get(cache_key)

        if not data:
            return HttpResponse(
                "<html><body><p>Login link expired or invalid. Please try again from DafeApp.</p></body></html>",
                status=400,
                content_type="text/html",
            )

        # Consume the token immediately (one-time use)
        _cache.delete(cache_key)

        odoo_url   = data["odoo_url"].rstrip("/")
        db_name    = data["db_name"]
        user_login = data.get("login", "admin")
        password   = data["password"]

        # Escape any characters that could break out of HTML attributes
        def _esc(v):
            return (
                v.replace("&", "&amp;")
                 .replace('"', "&quot;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
            )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Logging in to Odoo…</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; margin: 0; background: #f8fafc; color: #334155;
    }}
    .box {{
      text-align: center; padding: 2rem;
      background: #fff; border-radius: 1rem; border: 1px solid #e2e8f0;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }}
    p {{ margin: 0.5rem 0; font-size: .9rem; color: #64748b; }}
  </style>
</head>
<body>
  <div class="box">
    <p>Logging you in as <strong>{_esc(user_login)}</strong>…</p>
    <p style="font-size:.75rem">You will be redirected automatically.</p>
  </div>
  <form id="relay" method="POST" action="{_esc(odoo_url)}/web/login">
    <input type="hidden" name="db"       value="{_esc(db_name)}">
    <input type="hidden" name="login"    value="{_esc(user_login)}">
    <input type="hidden" name="password" value="{_esc(password)}">
    <input type="hidden" name="redirect" value="/odoo">
  </form>
  <script>
    // Submit after a tiny delay so the page paints first
    setTimeout(function() {{ document.getElementById('relay').submit(); }}, 120);
  </script>
</body>
</html>"""

        return HttpResponse(html, content_type="text/html; charset=utf-8")
