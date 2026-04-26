# Plan: DafeApp VPS — 3rd Server Provisioning Option

## Context
Currently users can provision Odoo servers using two paths:
1. **Connect your Cloud Provider** — their own DigitalOcean/AWS account
2. **Use VPS (PYOS)** — their own VPS via SSH

The platform admin can connect a DigitalOcean account (already using the existing `AddCloudAccountView` flow). The new feature adds a 3rd option — **DafeApp VPS** — where that platform-admin-owned DO account is shared with all organizations, so users don't need to connect their own cloud provider. Each org still gets isolated servers/infrastructure.

---

## Files to Modify

1. `cloud/models.py`
2. `cloud/migrations/0011_platform_cloud_account.py` ← new file
3. `cloud/admin.py`
4. `cloud/views.py`
5. `cloud/urls.py`
6. `deployments/views.py`
7. `templates/deployments/create_instance.html`

---

## Step 1 — `cloud/models.py`: Add platform account support

**1a. Make `organization` nullable on `CloudAccount`** (currently required FK):
```python
organization = models.ForeignKey(
    "organizations.Organization",
    on_delete=models.CASCADE,
    related_name="cloud_accounts",
    null=True,
    blank=True,
)
```

**1b. Add `is_platform` field** after `is_verified`:
```python
is_platform = models.BooleanField(
    default=False,
    help_text="Shared platform account available to all organizations.",
)
```

**1c. Add `get_platform_account()` classmethod** inside `CloudAccount`:
```python
@classmethod
def get_platform_account(cls):
    return cls.objects.filter(is_platform=True, is_verified=True).first()
```

> Existing `CloudDashboardView` queries `filter(organization=org)` — this naturally excludes `organization=None` platform accounts. No change needed there.

---

## Step 2 — Migration: `cloud/migrations/0011_platform_cloud_account.py`

Two operations:
1. `AlterField` — make `organization` nullable (null=True, blank=True)
2. `AddField` — add `is_platform` BooleanField(default=False)

Run: `python manage.py makemigrations cloud --name platform_cloud_account && python manage.py migrate`

---

## Step 3 — `cloud/admin.py`: Expose `is_platform`, superuser-only

- Add `is_platform` to `list_display` and `list_filter` on `CloudAccountAdmin`
- Override `get_readonly_fields` so only Django superusers can toggle `is_platform`

```python
def get_readonly_fields(self, request, obj=None):
    readonly = list(super().get_readonly_fields(request, obj))
    if not request.user.is_superuser:
        readonly.append("is_platform")
    return readonly
```

---

## Step 4 — `cloud/views.py`: New `PlatformCloudAccountOptionsView`

Add at the bottom of the file, following the same pattern as `CloudAccountOptionsAPIView` (line ~377):

```python
class PlatformCloudAccountOptionsView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        account = CloudAccount.get_platform_account()
        if not account:
            return JsonResponse({"regions": [], "sizes": [], "error": "No platform account configured."}, status=404)
        try:
            provider = get_provider(account)
            regions = provider.list_regions()
            region = request.GET.get("region", "").strip()
            sizes = provider.list_sizes(region=region)
            return JsonResponse({"regions": regions, "sizes": sizes, "provider": account.provider})
        except Exception as exc:
            return JsonResponse({"regions": [], "sizes": [], "error": str(exc)}, status=400)
```

---

## Step 5 — `cloud/urls.py`: Register new URL

Insert **before** `accounts/<int:pk>/options/` (so the static `platform` path takes priority):
```python
path("accounts/platform/options/", views.PlatformCloudAccountOptionsView.as_view(), name="platform-account-options"),
```

---

## Step 6 — `deployments/views.py`: Two changes

**6a. Context injection in `DeploymentCreateView.get_context_data()`** — after line 1481:
```python
ctx["platform_account_available"] = CloudAccount.get_platform_account() is not None
```

**6b. New 3rd flow in `OdooServerCreateAPIView.post()`** — insert between the PYOS early-return block (line 1765) and the Managed block (line 1767):

```python
# ── DafeApp Platform VPS ────────────────────────────────────
use_platform = payload.get("use_platform_account") in (True, "true", "True", "1")
if use_platform:
    region = (payload.get("region") or "").strip()
    size   = (payload.get("size") or "").strip()
    if not name or not region or not size:
        return JsonResponse({"error": "name, region and size are required."}, status=400)
    platform_account = CloudAccount.get_platform_account()
    if not platform_account:
        return JsonResponse({"error": "DafeApp VPS is not configured. Contact support."}, status=400)
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
        organization=org, infrastructure=infrastructure,
        cloud_account=platform_account, name=name,
        odoo_version=odoo_version, region=region, size=size,
        dns_domain=dns_domain, managed_dns_enabled=managed_dns_enabled,
        managed_dns_zone=managed_zone, domain_routing_enabled=domain_routing_enabled,
        tls_mode=tls_mode, deployment_mode=deployment_mode, created_by=request.user,
    )
    server.status = OdooServer.Status.CONNECTING
    server.save(update_fields=["status", "updated_at"])
    _dispatch(provision_odoo_server, server.id)
    return JsonResponse(OdooServerSerializer(server).data, status=201)
```

> `provision_odoo_server` is reused unchanged. It reads `server.effective_cloud_account` which resolves to the platform account via `infra.cloud_account`. No task changes needed.

---

## Step 7 — `templates/deployments/create_instance.html`: UI changes

**7a. Line 1454** — change `grid-cols-2` to `grid-cols-3`, add 3rd button after `pick-pyos`:
```html
<div class="grid grid-cols-3 gap-3">
  <button id="pick-cloud" ...>Cloud Provider</button>
  <button id="pick-pyos" ...>PYOS (SSH/VPS)</button>
  {% if platform_account_available %}
  <button id="pick-platform" type="button" class="px-3 py-2 rounded-lg border border-violet-300 text-sm font-medium hover:bg-violet-50 text-violet-800">DafeApp VPS</button>
  {% else %}
  <button id="pick-platform" type="button" disabled class="px-3 py-2 rounded-lg border border-gray-200 text-sm font-medium text-gray-300 cursor-not-allowed" title="DafeApp VPS is not available on this platform.">DafeApp VPS</button>
  {% endif %}
</div>
```

**7b. Lines 1618–1619** — insert platform panel between `</div>` (pyos-panel close) and `<p id="source-modal-msg">`:
```html
<!-- DafeApp VPS panel -->
<div id="platform-panel" class="hidden space-y-3">
  <div class="rounded-xl border border-violet-100 bg-violet-50/60 p-3">
    <p class="text-xs font-medium text-violet-900">DafeApp hosts this server on its own DigitalOcean account.</p>
    <p class="text-[11px] text-violet-700 mt-1">No cloud provider needed. Choose a region and size below.</p>
  </div>
  <div class="grid grid-cols-2 gap-3">
    <div>
      <label class="block text-xs font-medium text-gray-600 mb-1">Server Name</label>
      <input id="platform-server-name" class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" placeholder="e.g. odoo-prod" />
    </div>
    <div>
      <label class="block text-xs font-medium text-gray-600 mb-1">Odoo Version</label>
      <select id="platform-version" class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
        <option value="19">19 (Latest)</option><option value="18">18</option><option value="17">17</option>
      </select>
    </div>
  </div>
  <div>
    <label class="block text-xs font-medium text-gray-600 mb-1">Deployment Mode</label>
    <select id="platform-deployment-mode" class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
      <option value="BARE_METAL">Bare-metal</option><option value="DOCKER">Docker</option>
    </select>
  </div>
  <div class="grid grid-cols-2 gap-3">
    <div>
      <label class="block text-xs font-medium text-gray-600 mb-1">Region</label>
      <select id="platform-region" disabled class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white disabled:opacity-50">
        <option value="">Loading…</option>
      </select>
    </div>
    <div>
      <label class="block text-xs font-medium text-gray-600 mb-1">Size</label>
      <select id="platform-size" disabled class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white disabled:opacity-50">
        <option value="">— select region first —</option>
      </select>
    </div>
  </div>
  <div class="flex justify-end">
    <button id="submit-create-platform-server" type="button"
            class="px-4 py-2 text-sm font-medium text-white bg-violet-700 rounded-lg hover:bg-violet-800">
      Provision DafeApp VPS
    </button>
  </div>
</div>
```

**7c. JavaScript** — add after line 4483 (existing panel variable declarations):
```js
const pickPlatform       = $("pick-platform");
const platformPanel      = $("platform-panel");
const platformRegionEl   = $("platform-region");
const platformSizeEl     = $("platform-size");
const platformVersionEl  = $("platform-version");
const platformDeployEl   = $("platform-deployment-mode");
```

Update `showCloud()` (line 4537) and `showPyos()` (line 4544) — add `hide(platformPanel)` and deactivate `pickPlatform`:
```js
function showCloud() {
  mode = "cloud";
  show(cloudPanel); hide(pyosPanel); hide(platformPanel);
  activateBtn(pickCloud, pickPyos);
  pickPlatform?.classList.remove("bg-gray-900", "text-white", "border-gray-900");
  if ($("source-modal-msg")) $("source-modal-msg").textContent = "";
  syncCloudAccountState();
}
function showPyos() {
  mode = "pyos";
  show(pyosPanel); hide(cloudPanel); hide(platformPanel);
  hide(detailsModal);
  activateBtn(pickPyos, pickCloud);
  pickPlatform?.classList.remove("bg-gray-900", "text-white", "border-gray-900");
  // ...existing field resets unchanged...
}
```

Add `showPlatform()` after `showPyos()`:
```js
function showPlatform() {
  mode = "platform";
  show(platformPanel); hide(cloudPanel); hide(pyosPanel);
  activateBtn(pickPlatform, pickCloud);
  pickPyos?.classList.remove("bg-gray-900", "text-white", "border-gray-900");
  if ($("source-modal-msg")) $("source-modal-msg").textContent = "";
  loadPlatformRegionsAndSizes();
}
pickPlatform?.addEventListener("click", showPlatform);
```

Add platform region/size loaders (after existing `loadSizesForRegion`, line 4634):
```js
async function loadPlatformRegionsAndSizes() {
  if (!platformRegionEl) return;
  setSelectLoading(platformRegionEl, "Loading regions…");
  setSelectLoading(platformSizeEl, "— select region first —");
  try {
    const res = await fetch("/cloud/accounts/platform/options/");
    const data = await res.json();
    if (!res.ok || !Array.isArray(data.regions) || !data.regions.length) {
      setSelectError(platformRegionEl, data.error || "Failed to load regions"); return;
    }
    setOptions(platformRegionEl, data.regions);
    platformRegionEl.disabled = false;
    await loadPlatformSizesForRegion(platformRegionEl.value);
  } catch (_) { setSelectError(platformRegionEl, "Failed to load regions"); }
}
async function loadPlatformSizesForRegion(region) {
  if (!region || !platformSizeEl) return;
  setSelectLoading(platformSizeEl, "Loading sizes…");
  try {
    const res = await fetch(`/cloud/accounts/platform/options/?region=${encodeURIComponent(region)}`);
    const data = await res.json();
    if (!res.ok || !Array.isArray(data.sizes) || !data.sizes.length) {
      setSelectError(platformSizeEl, data.error || "Failed to load sizes"); return;
    }
    setOptions(platformSizeEl, data.sizes);
    platformSizeEl.disabled = false;
  } catch (_) { setSelectError(platformSizeEl, "Failed to load sizes"); }
}
platformRegionEl?.addEventListener("change", () => loadPlatformSizesForRegion(platformRegionEl.value));
```

Add submit handler (after the PYOS submit handler, line ~4708):
```js
$("submit-create-platform-server")?.addEventListener("click", async () => {
  const msgEl = $("source-modal-msg");
  const name   = ($("platform-server-name")?.value || "").trim();
  const region = platformRegionEl?.value || "";
  const size   = platformSizeEl?.value || "";
  const odooVersion = platformVersionEl?.value || "19";
  const deployMode  = platformDeployEl?.value || "BARE_METAL";
  if (!name)   { setMsg(msgEl, "Server name is required.", true); return; }
  if (!region) { setMsg(msgEl, "Select a region.", true); return; }
  if (!size)   { setMsg(msgEl, "Select a size.", true); return; }
  setMsg(msgEl, "Provisioning DafeApp VPS… This may take 5–15 minutes.");
  const resp = await postForm("/api/deployments/odoo/servers/create/", {
    name, region, size, odoo_version: odooVersion, deployment_mode: deployMode,
    use_platform_account: true,
  });
  if (!resp.ok) { setMsg(msgEl, resp.data?.error || "Provisioning failed.", true); return; }
  hide($("server-source-modal"));
  insertServerCard(resp.data, { name, odooVersion, deployMode, isPyos: false });
  syncLiveServerPolling([resp.data]);
});
```

---

## Platform Admin Setup (post-deploy, one-time)

1. Platform admin logs into Django admin
2. Creates a `CloudAccount` with DigitalOcean credentials (or uses existing one)
3. Sets `is_platform = True` (only superusers can do this)
4. `validate_cloud_account` task runs automatically → sets `is_verified = True`
5. "DafeApp VPS" button becomes active for all orgs

If no platform account is configured: the button is disabled in the UI and the API returns a clear error.

---

## Verification

1. `python manage.py migrate` — confirm migration applies cleanly
2. In Django admin, set `is_platform=True` on a verified DO account
3. As an org user, open the server creation modal — confirm "DafeApp VPS" button is active
4. Select region + size, provision — confirm `OdooServer` created with `cloud_account=platform_account`, `organization=org`
5. Confirm provisioning task runs and server reaches `RUNNING` status
6. Confirm existing Cloud Provider and PYOS flows still work unchanged
7. Confirm org users cannot see the platform account in their cloud dashboard
