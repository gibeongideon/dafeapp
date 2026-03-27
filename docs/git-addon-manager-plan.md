# Git Addon Manager Plan

This document maps out how to add a Git-based addon manager to DafeApp without breaking the current server and instance lifecycle.

---

## 1. Current App Shape

DafeApp already has the right primitives for this feature:

- `OdooServer` owns the deployed host or container environment.
- `OdooInstance` represents a single Odoo database/service on that server.
- `create_odoo_instance` and `delete_odoo_instance` already handle lifecycle tasks.
- The instance console already has tabs, live logs, history, and websocket updates.

What is missing today:

- No model for Git repositories attached to an instance.
- No persistent addon-path management.
- No branch tracking or repo update workflow.
- No UI for managing multiple addon sources per instance.

---

## 2. Product Goal

Each Odoo instance should be able to consume multiple Git repositories as isolated addon sources.

Target behavior:

- One instance can have many repos.
- Each repo lives in its own folder.
- The instance builds `addons_path` dynamically from the repo folders.
- Users can add, update, switch branches, remove, and auto-sync repos.
- Odoo restarts and module refreshes happen after repo changes.

Important rule:

- Do not merge repositories into one directory.
- Keep every repo isolated for easier debugging and rollback.

---

## 3. Recommended Data Model

### 3.1 OdooInstance extensions

Keep `OdooInstance` as the owner of the addon stack and add a few derived/runtime fields:

- `addons_root_path`
- `addons_path_cache`
- `addons_sync_status`
- `addons_last_sync_at`

These fields let the app know where the instance lives on disk and what was last deployed without recomputing everything from scratch.

### 3.2 New model: `OdooInstanceGitRepo`

One instance to many repos.

Suggested fields:

- `instance` `ForeignKey(OdooInstance)`
- `repo_name`
- `git_url`
- `branch`
- `auth_type` (`GITHUB_OAUTH`, `TOKEN`, `SSH_KEY`, `PUBLIC`)
- `credential_ref` or `credential_payload` depending on how credentials are stored
- `local_path`
- `auto_update`
- `last_pulled_commit`
- `last_pulled_at`
- `status` (`CONNECTED`, `CLONING`, `UPDATING`, `ERROR`, `DISCONNECTED`)
- `last_error`
- `created_by`
- `created_at`
- `updated_at`

Recommended extra fields:

- `default_branch`
- `pinned_commit`
- `display_order`
- `is_enabled`

### 3.3 Credential model

Do not store tokens directly on the repo row unless encryption is already standardized.

Best option:

- Create a separate credential model scoped to organization or instance.
- Encrypt secret material at rest.
- Reference the credential from `OdooInstanceGitRepo`.

This keeps repo metadata separate from secret management.

---

## 4. Filesystem Layout

Use a canonical instance root and keep repo folders under it.

Example:

```text
/odoo_instances/
  instance_1/
    addons/
      repo_1/
      repo_2/
  instance_2/
    addons/
      repo_3/
```

Suggested naming:

- Instance root: `/odoo_instances/<instance_id>/`
- Addons root: `/odoo_instances/<instance_id>/addons/`
- Repo path: `/odoo_instances/<instance_id>/addons/<repo_slug>/`

Why this works:

- Easy to mount in Docker.
- Easy to reference in systemd / nginx / Ansible.
- Easy to inspect on disk.
- Easy to remove a single repo cleanly.

---

## 5. Addons Path Strategy

Build the instance `addons_path` from two parts:

- Core Odoo addons
- One path per enabled Git repo

Example:

```text
core_addons,/odoo_instances/12/addons/repo_one,/odoo_instances/12/addons/repo_two
```

Implementation notes:

- Keep the repo list ordered.
- Recompute `addons_path` whenever a repo is added, removed, renamed, or switched branches.
- Store the computed value on the instance for quick rendering, but treat repo records as the source of truth.

---

## 6. Repo Lifecycle

### 6.1 Add Repository

User flow:

1. Open an instance.
2. Go to the `Addons` tab.
3. Click `Add Repository`.
4. Choose one of:
   - Add from GitHub
   - Add by Git URL
   - Upload ZIP and publish to GitHub later
5. Select branch and auth method.
6. Save.

System flow:

1. Validate the repo URL and auth method.
2. Create the repo folder.
3. Clone the repository into the instance addons directory.
4. Record the initial commit SHA.
5. Rebuild `addons_path`.
6. Restart Odoo.
7. Refresh app list / module registry.

### 6.2 Update Repository

Manual update:

- Fetch remote changes.
- Compare local and remote commit.
- Pull if changed.
- Restart Odoo.
- Refresh module list.

Auto update:

- Background worker checks repos on a schedule.
- Only pull if there is a new remote commit.
- Rebuild `addons_path` only if the repo is active.

### 6.3 Branch Change

When the branch changes:

- Save the new branch.
- Fetch the branch.
- Checkout the branch.
- Pull latest commit.
- Rebuild `addons_path`.
- Restart Odoo.
- Refresh modules.

### 6.4 Remove Repository

When a repo is removed:

- Detach it from the instance.
- Remove its folder from the filesystem.
- Rebuild `addons_path`.
- Restart Odoo.
- Refresh app list.

---

## 7. Git Integration Jobs

This feature should use background jobs, not synchronous request handling.

Suggested jobs:

- `clone_instance_repo`
- `update_instance_repo`
- `checkout_instance_repo_branch`
- `remove_instance_repo`
- `refresh_instance_addons`
- `sync_instance_repo_status`

Job behavior:

- Lock per instance while a repo is updating.
- Emit websocket progress events.
- Save success or error state on the repo record.

---

## 8. UI Plan

### 8.1 Instance Console

Add a new `Addons` tab to the instance console.

Show:

- Repo name
- Branch
- Auto update
- Last pulled commit
- Last sync time
- Status
- Actions

Actions:

- Update Repo
- Change Branch
- Toggle Auto Update
- Remove Repo
- View Logs

### 8.2 Add Repository Modal

Support these entry points:

- Add from GitHub
- Add from Git URL
- Upload ZIP

The modal should collect:

- Repo name
- URL or GitHub selection
- Branch
- Auth method
- Auto update toggle

### 8.3 Repo Detail Drawer

Show:

- Remote URL
- Local path
- Current branch
- Last commit
- Last update result
- Error output
- Raw sync logs

---

## 9. Authentication Strategy

Recommended support order:

1. GitHub OAuth for best UX.
2. Personal Access Token for private repositories.
3. SSH key for advanced users.

Implementation guidance:

- Reuse encrypted-secret patterns already used for cloud credentials.
- Never expose raw tokens in the UI.
- Store only what is needed to reconnect and sync.

---

## 10. Odoo Reload Strategy

After repo changes, the system should:

1. Rebuild the instance addon path.
2. Restart the Odoo service or container.
3. Trigger module app-list refresh.
4. Optionally run a targeted module upgrade.

Smart update preference:

- Detect changed modules first.
- Update only affected modules when possible.
- Offer `Update All Modules` as a fallback action.

---

## 11. Error Handling

Track repo-level errors explicitly:

- Clone failed
- Auth failed
- Branch checkout failed
- Merge conflict
- Invalid addon structure
- Restart failed
- Module refresh failed

Show these clearly in the UI and keep the previous healthy state when possible.

---

## 12. Suggested Implementation Phases

### Phase 1: Data + Read-Only UI

- Add repo model.
- Add instance addon-path fields.
- Add serializers and API endpoints.
- Add `Addons` tab in the instance console.
- Show repo rows with status only.

### Phase 2: Clone / Pull / Remove

- Implement repo clone, update, and remove jobs.
- Store local paths.
- Rebuild `addons_path`.
- Restart Odoo after changes.

### Phase 3: Branch + Auto Sync

- Add branch switching.
- Add auto-update checkbox.
- Add scheduled sync job.
- Add per-repo logs and status.

### Phase 4: Auth + GitHub UX

- Add GitHub OAuth.
- Add PAT support.
- Add SSH key auth.
- Add repo picker UI.

### Phase 5: Advanced Controls

- Change detection for selective module updates.
- Rollback to previous commit.
- Repo health dashboard.
- Notifications.

---

## 13. Files Likely to Change

Based on the current codebase, this feature will likely touch:

- `deployments/models.py`
- `deployments/views.py`
- `deployments/tasks.py`
- `deployments/serializers.py`
- `deployments/urls.py`
- `deployments/consumers.py`
- `templates/deployments/odoo_instance_console.html`
- `templates/deployments/create_instance.html`
- `docs/deployment-flow.md`
- `docs/api-endpoints.md`

---

## 14. Main Risks

- Race conditions when multiple repo jobs hit the same instance.
- Incorrect `addons_path` order breaking module discovery.
- Secret storage mistakes.
- Docker vs bare-metal path differences.
- Long-running restarts blocking user actions.

Mitigation:

- Use per-instance locks.
- Keep repo folders isolated.
- Store credentials securely.
- Make repo updates asynchronous.
- Surface job logs in the UI.

---

## 15. Recommended First Build Slice

Start with the smallest useful version:

1. Add the repo model.
2. Store repo metadata and local path.
3. Render repos in the instance `Addons` tab.
4. Clone and remove repos on demand.
5. Rebuild `addons_path` and restart Odoo.

That gives you a working multi-repo foundation before adding OAuth, branch automation, and smart module updates.
