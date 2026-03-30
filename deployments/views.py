import logging
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from cloud.models import CloudAccount, PyOSSSHSettings
from cloud.providers import get_provider
from cloud.pyos import looks_like_public_key_text
from dns.models import DomainAssignment, DnsZone, normalize_domain_name
from deployments.models import (
    DeploymentJob,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    ServerSSHKey,
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
)
from deployments.serializers import (
    DeploymentJobSerializer,
    GitRepositoryCredentialSerializer,
    InfrastructureSerializer,
    InstanceSerializer,
    OdooInstanceGitRepoSerializer,
    OdooInstanceHistorySerializer,
    OdooInstanceSerializer,
    OdooServerHistorySerializer,
    OdooServerSerializer,
    TerraformRunSerializer,
)
from deployments.tasks import (
    checkout_instance_repo_branch,
    clone_instance_repo,
    create_odoo_instance,
    detach_instance_domain,
    delete_odoo_instance,
    deploy_server_ssh_key,
    provision_instance_domain,
    provision_odoo_server,
    refresh_instance_addons,
    remove_instance_repo,
    rollback_odoo_instance,
    rollback_instance_repo,
    terraform_apply_instance,
    update_instance_repo,
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


def _request_data(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


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
    is_enabled: bool = True,
    display_order=None,
):
    repo_name = _derive_repo_name(repo_name, git_url)
    if instance.git_repos.filter(repo_name=repo_name).exists():
        raise ValueError("A repo with that name already exists on this instance.")

    if display_order in ("", None):
        display_order = instance.git_repos.count()

    repo = OdooInstanceGitRepo.objects.create(
        instance=instance,
        credential=credential,
        repo_name=repo_name,
        git_url=git_url,
        branch=(branch or "main").strip() or "main",
        auth_type=auth_type,
        local_path=_build_repo_local_path(instance, repo_name),
        auto_update=auto_update,
        is_enabled=is_enabled,
        display_order=int(display_order or 0),
        default_branch=(branch or "main").strip() or "main",
        created_by=user,
    )
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
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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


def _create_github_repository(*, account, repo_name: str, private: bool = True):
    try:
        create_response = requests.post(
            "https://api.github.com/user/repos",
            headers=_github_api_headers(account.access_token),
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
        raise RuntimeError(detail or f"GitHub repository creation failed: {exc}") from exc

    return create_response.json()

def _push_zip_to_github_repo(*, account, user, full_name: str, zip_file, branch: str = "main"):
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
        remote_url = f"https://x-access-token:{quote(account.access_token, safe='')}@github.com/{full_name}.git"
        git_author = account.username or user.get_full_name() or user.email.split("@")[0]
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
        is_verified=False,
        verification_error="Reachability has not been verified yet.",
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


def _active_instances_for_server(server: OdooServer):
    return server.instances.exclude(status=OdooInstance.Status.DELETED)


def _next_available_port(server: OdooServer) -> int | None:
    used = set(
        _active_instances_for_server(server).values_list("http_port", flat=True)
    )
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
        server_id = (self.request.GET.get("server_id") or "").strip()
        accounts = CloudAccount.objects.filter(organization=org, is_verified=True)
        ctx["accounts"] = accounts
        ctx["show_dns_view"] = section == "dns"
        ctx["show_instances_view"] = section == "instances"
        ctx["default_tls_mode"] = _default_tls_mode()
        ctx["PLATFORM_BASE_DOMAIN"] = platform_base_domain()
        ctx["platform_domains_enabled"] = platform_domains_enabled()
        ctx["platform_dns_configured"] = platform_dns_is_configured()
        ctx["platform_dns_proxied"] = platform_dns_default_proxied()
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
        ctx["odoo_instances"] = (
            OdooInstance.objects.filter(organization=org, server__is_active=True)
            .exclude(status=OdooInstance.Status.DELETED)
            .select_related("server")
            .order_by("-created_at")[:20]
        )
        ctx["selected_server"] = None
        ctx["selected_instances"] = OdooInstance.objects.none()
        ctx["server_id"] = server_id
        if server_id:
            selected_server = get_object_or_404(
                OdooServer.objects.select_related("infrastructure", "infrastructure__external_server", "cloud_account"),
                pk=server_id,
                organization=org,
                is_active=True,
            )
            ctx["selected_server"] = selected_server
            ctx["selected_instances"] = (
                OdooInstance.objects.filter(organization=org, server=selected_server)
                .exclude(status=OdooInstance.Status.DELETED)
                .select_related("server")
                .order_by("-created_at")
            )
        ctx["recent_runs"] = TerraformRun.objects.filter(
            instance__organization=org
        ).select_related("instance")[:15]
        ctx["enforcer"] = getattr(self.request, "subscription_enforcer", SubscriptionEnforcer(org))
        from cloud.models import SystemSSHKey
        ctx["dafeapp_public_key"] = SystemSSHKey.get_or_create_keypair().public_key
        ctx["pyos_default_ssh_key_path"] = PyOSSSHSettings.get_or_create_settings().default_ssh_key_path
        return ctx

    def post(self, request):
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
            deployment_mode = OdooServer.DeploymentMode.BARE_METAL
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
                created_by=request.user,
            )
            logger.info(
                "Inline PYOS server record created: id=%s infra=%s host=%s version=%s",
                server.id,
                infrastructure.id,
                host,
                odoo_version,
            )
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

        port = int(port_raw) if port_raw else _next_available_port(server)
        if port is None:
            return JsonResponse({"error": "No available port on this server."}, status=400)
        if port < server.min_port or port > server.max_port:
            return JsonResponse({"error": f"Port must be within {server.min_port}-{server.max_port}."}, status=400)
        if _active_instances_for_server(server).filter(http_port=port).exists():
            return JsonResponse({"error": "Selected port is already in use on this server."}, status=400)

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
        data = OdooInstanceSerializer(qs[:200], many=True).data
        return JsonResponse({"results": data})


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
                is_enabled=str(payload.get("is_enabled", "true")).lower() not in ("0", "false", "no", "off"),
                display_order=payload.get("display_order"),
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
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
        payload = _request_data(request)

        branch_changed = False
        refresh_needed = False

        if "repo_name" in payload:
            repo.repo_name = _derive_repo_name(payload.get("repo_name"), repo.git_url)
            refresh_needed = True
        if "git_url" in payload and payload.get("git_url"):
            repo.git_url = payload.get("git_url").strip()
        if "branch" in payload and payload.get("branch") and payload.get("branch").strip() != repo.branch:
            repo.branch = payload.get("branch").strip()
            branch_changed = True
        if "auto_update" in payload:
            repo.auto_update = str(payload.get("auto_update")).lower() in ("1", "true", "yes", "on")
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
            repo.auth_type = auth_type
            try:
                repo.credential = _resolve_git_credential(
                    org=org,
                    user=request.user,
                    payload=payload,
                    auth_type=auth_type,
                )
            except ValueError as exc:
                return JsonResponse({"error": str(exc)}, status=400)

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
        elif refresh_needed:
            job = _repo_job(
                org,
                job_type=DeploymentJob.JobType.REFRESH_INSTANCE_ADDONS,
                instance=repo.instance,
                user=request.user,
            )
            _dispatch(refresh_instance_addons, repo.instance_id, job.id)

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
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.UPDATE_INSTANCE_REPO,
            instance=repo.instance,
            user=request.user,
        )
        _dispatch(update_instance_repo, repo.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id})


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
        job = _repo_job(
            org,
            job_type=DeploymentJob.JobType.REMOVE_INSTANCE_REPO,
            instance=repo.instance,
            user=request.user,
        )
        _dispatch(remove_instance_repo, repo.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id})


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

        get_object_or_404(
            OdooInstance.objects.only("id"),
            pk=instance_id,
            organization=org,
        )

        payload = _request_data(request)
        repo_name = (payload.get("repo_name") or "").strip()
        if not repo_name:
            return JsonResponse({"error": "repo_name is required."}, status=400)

        try:
            account = _active_github_account(
                user=request.user,
                account_id=payload.get("github_account_id"),
            )
            repo_data = _create_github_repository(account=account, repo_name=repo_name)
        except ValueError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "connect_url": reverse("socialaccount_connections"),
                },
                status=400,
            )
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
                }
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

        branch = (request.POST.get("branch") or "main").strip() or "main"
        zip_file = request.FILES.get("zip_file")
        full_name = (request.POST.get("full_name") or "").strip()
        clone_url = (request.POST.get("clone_url") or "").strip()
        repo_name = _derive_repo_name(request.POST.get("repo_name"), clone_url or full_name)
        if not full_name:
            return JsonResponse({"error": "full_name is required."}, status=400)
        if not zip_file:
            return JsonResponse({"error": "zip_file is required."}, status=400)

        try:
            account = _active_github_account(
                user=request.user,
                account_id=request.POST.get("github_account_id"),
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
            _push_zip_to_github_repo(
                account=account,
                user=request.user,
                full_name=full_name,
                zip_file=zip_file,
                branch=branch,
            )
            credential = _ensure_github_oauth_credential(
                org=org,
                user=request.user,
                account=account,
            )
            repo, job = _create_instance_repo_and_dispatch(
                org=org,
                user=request.user,
                instance=instance,
                repo_name=repo_name,
                git_url=clone_url or f"https://github.com/{full_name}.git",
                branch=branch,
                auth_type=OdooInstanceGitRepo.AuthType.GITHUB_OAUTH,
                credential=credential,
            )
        except RuntimeError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        data = OdooInstanceGitRepoSerializer(repo).data
        data["job_id"] = job.id
        data["github_repo"] = {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
        }
        return JsonResponse(data, status=201)


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
            OdooInstance.objects.select_related("server"),
            pk=self.kwargs["instance_id"],
            organization=org,
        )
        ctx["odoo_instance"] = instance
        ctx["odoo_server"] = instance.server
        ctx["instance_git_repos"] = list(instance.git_repos.all())
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
        # Dispatch async cleanup (stop service, drop DB, free port).
        _dispatch(delete_odoo_instance, instance.id)
        return JsonResponse({"ok": True, "message": "Instance deletion queued."})


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


class OdooServerDeleteAPIView(LoginRequiredMixin, View):
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
        try:
            with transaction.atomic():
                server.delete()
            return JsonResponse({"ok": True, "message": "Server deleted from the database."})
        except Exception as exc:
            logger.exception("Unexpected error deleting OdooServer %s", server_id)
            return JsonResponse({"error": f"Delete failed: {exc}"}, status=500)


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


class OdooInstanceHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/instances/<id>/history/ — deployment history for an instance."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        qs = OdooInstanceHistory.objects.filter(instance=instance).order_by("-deployed_at")
        return JsonResponse({"results": OdooInstanceHistorySerializer(qs, many=True).data})


class OdooInstanceRollbackAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/rollback/ — re-deploy from a history snapshot."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
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
        key_obj = get_object_or_404(ServerSSHKey, pk=key_id, server=server)
        key_obj.delete()
        return JsonResponse({"deleted": True})
