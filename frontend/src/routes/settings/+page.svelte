<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import DataTable from '$lib/components/DataTable.svelte';
  import FormInput from '$lib/components/FormInput.svelte';
  import Button from '$lib/components/Button.svelte';
  import ExcelTable from '$lib/components/ExcelTable.svelte';

  let users = $state<any[]>([]);
  let logs = $state<any[]>([]);
  let groups = $state<any[]>([]);
  let activeTab = $state<'users' | 'logs' | 'auth' | 'groups' | 'ldap' | 'storage' | 'auto_approve'>('users');

  // Auto-approve settings state
  let autoApprove = $state<{ enabled: boolean; threshold: number; last_run?: string; last_count?: number }>(
    { enabled: false, threshold: 0.95 }
  );
  let autoApproveSaving = $state(false);
  let autoApproveMsg = $state<{ type: 'ok' | 'err'; msg: string } | null>(null);

  async function loadAutoApprove() {
    try {
      const r = await api.getAutoApprove();
      autoApprove = {
        enabled: !!r.enabled,
        threshold: typeof r.threshold === 'number' ? r.threshold : 0.95,
        last_run: r.last_run,
        last_count: r.last_count,
      };
    } catch (e: any) {
      autoApproveMsg = { type: 'err', msg: e?.message || 'load failed' };
    }
  }

  async function saveAutoApprove() {
    autoApproveSaving = true;
    autoApproveMsg = null;
    try {
      const t = Math.max(0, Math.min(1, Number(autoApprove.threshold) || 0));
      await api.saveAutoApprove({ enabled: !!autoApprove.enabled, threshold: t });
      autoApproveMsg = { type: 'ok', msg: 'Saved' };
      await loadAutoApprove();
    } catch (e: any) {
      autoApproveMsg = { type: 'err', msg: e?.message || 'save failed' };
    }
    autoApproveSaving = false;
  }

  $effect(() => { if (auth.isAdmin && activeTab === 'auto_approve') loadAutoApprove(); });

  // Storage state
  let storageConfigs = $state<any[]>([]);
  let storageEditing = $state<any>(null);
  let storageTesting = $state<number | null>(null);
  let storageToast = $state<{type: string, msg: string} | null>(null);

  const PROVIDER_PRESETS: Record<string, any> = {
    aws:        { endpoint_url: 'https://s3.{region}.amazonaws.com', signature_version: 's3v4', addressing_style: 'auto' },
    minio:      { endpoint_url: 'http://localhost:9000', signature_version: 's3v4', addressing_style: 'path' },
    r2:         { endpoint_url: 'https://{account_id}.r2.cloudflarestorage.com', signature_version: 's3v4', addressing_style: 'auto' },
    wasabi:     { endpoint_url: 'https://s3.{region}.wasabisys.com', signature_version: 's3v4', addressing_style: 'auto' },
    backblaze:  { endpoint_url: 'https://s3.{region}.backblazeb2.com', signature_version: 's3v4', addressing_style: 'auto' },
    custom:     { endpoint_url: '', signature_version: 's3v4', addressing_style: 'auto' },
  };

  async function loadStorage() {
    try {
      const r = await fetch('/api/storage/configs', { headers: { 'Authorization': `Bearer ${auth.token}` } });
      if (r.ok) storageConfigs = (JSON.parse(await r.text())).configs || [];
    } catch (e) { console.error(e); }
  }
  async function saveStorage() {
    const body: any = { ...storageEditing };
    const isEdit = body.id !== undefined && body.id !== null;
    const url = isEdit ? `/api/storage/configs/${body.id}` : '/api/storage/configs';
    const method = isEdit ? 'PUT' : 'POST';
    if (isEdit && !body.secret_access_key) delete body.secret_access_key;
    delete body.id;
    try {
      const r = await fetch(url, {
        method, headers: { 'Authorization': `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        storageEditing = null;
        await loadStorage();
        storageToast = { type: 'ok', msg: isEdit ? 'Updated' : 'Created' };
      } else {
        storageToast = { type: 'err', msg: `Save failed (${r.status})` };
      }
    } catch (e: any) {
      storageToast = { type: 'err', msg: e.message };
    }
  }
  async function testStorage(id: number) {
    storageTesting = id;
    try {
      const r = await fetch(`/api/storage/configs/${id}/test`, {
        method: 'POST', headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      const j = JSON.parse(await r.text());
      storageToast = j.ok ? { type: 'ok', msg: `S3 OK (bucket=${j.bucket||''})` } : { type: 'err', msg: j.error || 'failed' };
    } finally { storageTesting = null; }
  }
  async function activateStorage(id: number) {
    await fetch(`/api/storage/configs/${id}/activate`, {
      method: 'POST', headers: { 'Authorization': `Bearer ${auth.token}` },
    });
    await loadStorage();
    storageToast = { type: 'ok', msg: 'Activated' };
  }
  async function deleteStorage(id: number) {
    if (!confirm('Delete this storage config?')) return;
    await fetch(`/api/storage/configs/${id}`, {
      method: 'DELETE', headers: { 'Authorization': `Bearer ${auth.token}` },
    });
    await loadStorage();
  }

  function applyPreset(preset: string) {
    if (!storageEditing) return;
    const p = PROVIDER_PRESETS[preset] || {};
    storageEditing = { ...storageEditing, provider: preset, ...p };
  }

  $effect(() => { if (auth.isAdmin && activeTab === 'storage') loadStorage(); });

  // LDAP state
  let ldaps = $state<any[]>([]);
  let ldapEditing = $state<any>(null);  // null = not open, {} = create new, {id...} = edit
  let ldapTesting = $state<number | null>(null);
  let ldapToast = $state<{type: string, msg: string} | null>(null);

  async function loadLdaps() {
    try {
      const r = await fetch('/api/ldap/configs', { headers: { 'Authorization': `Bearer ${auth.token}` } });
      if (r.ok) {
        const j = JSON.parse(await r.text());
        ldaps = j.configs || [];
      }
    } catch (e) { console.error(e); }
  }

  $effect(() => { if (auth.isAdmin) loadLdaps(); });

  async function saveLdap() {
    const body: any = { ...ldapEditing };
    const isEdit = body.id !== undefined && body.id !== null;
    const url = isEdit ? `/api/ldap/configs/${body.id}` : '/api/ldap/configs';
    const method = isEdit ? 'PUT' : 'POST';
    if (isEdit && !body.bind_password) delete body.bind_password;
    delete body.id;
    try {
      const r = await fetch(url, {
        method,
        headers: { 'Authorization': `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        ldapEditing = null;
        await loadLdaps();
        ldapToast = { type: 'ok', msg: isEdit ? 'Updated' : 'Created' };
      } else {
        ldapToast = { type: 'err', msg: `Save failed (${r.status})` };
      }
    } catch (e: any) {
      ldapToast = { type: 'err', msg: e.message };
    }
  }

  async function testLdap(id: number) {
    ldapTesting = id;
    try {
      const r = await fetch(`/api/ldap/configs/${id}/test`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      const j = JSON.parse(await r.text());
      ldapToast = j.ok ? { type: 'ok', msg: 'LDAP bind OK' } : { type: 'err', msg: j.error || 'failed' };
    } catch (e: any) {
      ldapToast = { type: 'err', msg: e.message };
    } finally {
      ldapTesting = null;
    }
  }

  async function deleteLdap(id: number) {
    if (!confirm('Delete this LDAP config?')) return;
    await fetch(`/api/ldap/configs/${id}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${auth.token}` },
    });
    await loadLdaps();
  }
  let loading = $state(true);

  // Create user form
  let newUsername = $state('');
  let newPassword = $state('');
  let newDisplayName = $state('');
  let newRole = $state('user');
  let createError = $state('');
  let createSuccess = $state('');

  // Self-service change-my-password form
  let myCurrentPw = $state('');
  let myNewPw = $state('');
  let myConfirmPw = $state('');
  let myPwBusy = $state(false);
  let myPwError = $state('');
  let myPwSuccess = $state('');

  async function changeMyPassword() {
    myPwError = ''; myPwSuccess = '';
    if (myNewPw.length < 8) { myPwError = 'MIN_8_CHARS'; return; }
    if (myNewPw !== myConfirmPw) { myPwError = 'PASSWORDS_DO_NOT_MATCH'; return; }
    if (myNewPw === myCurrentPw) { myPwError = 'NEW_MUST_DIFFER'; return; }
    myPwBusy = true;
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ current_password: myCurrentPw, new_password: myNewPw }),
      });
      const t = await r.text();
      const j = JSON.parse(t);
      if (!r.ok) throw new Error(j.detail || 'CHANGE_FAILED');
      myCurrentPw = ''; myNewPw = ''; myConfirmPw = '';
      myPwSuccess = 'PASSWORD_UPDATED';
      if (auth.user) auth.user = { ...auth.user, must_change_password: false };
    } catch (e: any) {
      myPwError = e?.message || 'FAILED';
    } finally {
      myPwBusy = false;
    }
  }

  // Keycloak settings
  let kcRealmUrl = $state('');
  let kcClientId = $state('');
  let kcClientSecret = $state('');
  let kcAdminRole = $state('admin');
  let kcEnabled = $state(false);
  let kcSaving = $state(false);
  let kcTesting = $state(false);
  let kcMessage = $state('');
  let kcMessageType = $state<'success' | 'error' | ''>('');
  let kcTestResult = $state('');
  let kcTestType = $state<'success' | 'error' | ''>('');

  // Group editor
  let editingGroup = $state<any>(null);
  let groupName = $state('');
  let groupDesc = $state('');
  let groupPages = $state({ agent: true, history: true, items: true, declarations: true, costs: true, settings: false });
  let groupActions = $state({ run_pipeline: true, upload_pdf: true, download_excel: true, delete_jobs: false, export_data: true });
  let groupScope = $state('own');
  let groupMembers = $state<number[]>([]);
  let groupSaving = $state(false);
  let groupMessage = $state('');

  const userColumns = [
    { key: 'username', label: 'Username' },
    { key: 'role', label: 'Role' },
    { key: 'group_name', label: 'Group' },
    { key: 'auth_type', label: 'Auth' },
    { key: 'email', label: 'Email' },
    { key: 'created_at', label: 'Created' },
    { key: 'last_login', label: 'Login' },
  ];

  const logColumns = [
    { key: 'created_at', label: 'Timestamp' },
    { key: 'username', label: 'User' },
    { key: 'action', label: 'Action' },
    { key: 'detail', label: 'Details' },
  ];

  const userRows = $derived(users.map(u => ({
    ...u,
    auth_type: u.keycloak_id ? 'KEYCLOAK' : 'LOCAL',
    group_name: u.group_name || '—',
    email: u.email || '—',
    is_active: u.is_active ? 'ACTIVE' : 'DISABLED',
    created_at: (u.created_at || '').split('T')[0] || (u.created_at || '').split(' ')[0] || '',
    last_login: u.last_login ? ((u.last_login || '').split('T')[0] || (u.last_login || '').split(' ')[0]) : 'Never',
  })));

  const logRows = $derived(logs.map(l => ({
    ...l,
    created_at: l.created_at?.replace('T', ' ').slice(0, 19) ?? '',
  })));

  async function loadData() {
    loading = true;
    try { users = await api.listUsers(); } catch { users = []; }
    try { logs = await api.activityLogs(); } catch { logs = []; }
    try { groups = await api.listGroups(); } catch { groups = []; }
    loading = false;
  }

  async function loadKeycloakSettings() {
    try {
      const s = await api.getKeycloakSettings();
      kcRealmUrl = s.realm_url || '';
      kcClientId = s.client_id || '';
      kcClientSecret = s.client_secret || '';
      kcAdminRole = s.admin_role || 'admin';
      kcEnabled = s.enabled || false;
    } catch {}
  }

  async function createUser() {
    createError = '';
    createSuccess = '';
    try {
      await api.createUser({ username: newUsername, password: newPassword, display_name: newDisplayName, role: newRole });
      createSuccess = `User '${newUsername}' created`;
      newUsername = ''; newPassword = ''; newDisplayName = ''; newRole = 'user';
      await loadData();
    } catch (e: any) { createError = e.message; }
  }

  async function saveKeycloakSettings() {
    kcSaving = true; kcMessage = '';
    try {
      await api.saveKeycloakSettings({ realm_url: kcRealmUrl, client_id: kcClientId, client_secret: kcClientSecret, admin_role: kcAdminRole, enabled: kcEnabled });
      if (kcEnabled && (!kcRealmUrl.trim() || !kcClientId.trim())) {
        kcMessage = 'SAVED — will not activate until REALM_URL and CLIENT_ID are configured';
        kcMessageType = 'error';
      } else if (kcEnabled) {
        kcMessage = 'KEYCLOAK ENABLED — All users must authenticate via SSO';
        kcMessageType = 'success';
      } else {
        kcMessage = 'SETTINGS SAVED — Using local auth';
        kcMessageType = 'success';
      }
      await auth.fetchAuthConfig();
    } catch (e: any) { kcMessage = e.message || 'SAVE_FAILED'; kcMessageType = 'error'; }
    kcSaving = false;
  }

  async function testKeycloakConnection() {
    kcTesting = true; kcTestResult = '';
    try {
      const r = await api.testKeycloakConnection({ realm_url: kcRealmUrl, client_id: kcClientId, client_secret: kcClientSecret, admin_role: kcAdminRole, enabled: kcEnabled });
      kcTestResult = r.message; kcTestType = r.success ? 'success' : 'error';
    } catch (e: any) { kcTestResult = e.message || 'FAILED'; kcTestType = 'error'; }
    kcTesting = false;
  }

  // Group editor functions
  function startNewGroup() {
    editingGroup = { id: null };
    groupName = ''; groupDesc = '';
    groupPages = { agent: true, history: true, items: true, declarations: true, costs: true, settings: false };
    groupActions = { run_pipeline: true, upload_pdf: true, download_excel: true, delete_jobs: false, export_data: true };
    groupScope = 'own'; groupMembers = []; groupMessage = '';
  }

  async function editGroup(g: any) {
    editingGroup = g;
    groupName = g.name; groupDesc = g.description || '';
    groupPages = { agent: !!g.page_agent, history: !!g.page_history, items: !!g.page_items, declarations: !!g.page_declarations, costs: !!g.page_costs, settings: !!g.page_settings };
    groupActions = { run_pipeline: !!g.action_run_pipeline, upload_pdf: !!g.action_upload_pdf, download_excel: !!g.action_download_excel, delete_jobs: !!g.action_delete_jobs, export_data: !!g.action_export_data };
    groupScope = g.data_scope || 'own'; groupMessage = '';
    // Fetch members
    try {
      const full = await api.getGroup(g.id);
      groupMembers = (full.members || []).map((m: any) => m.id);
    } catch { groupMembers = []; }
  }

  async function saveGroup() {
    groupSaving = true; groupMessage = '';
    const data = {
      name: groupName, description: groupDesc,
      page_agent: groupPages.agent, page_history: groupPages.history, page_items: groupPages.items,
      page_declarations: groupPages.declarations, page_costs: groupPages.costs, page_settings: groupPages.settings,
      action_run_pipeline: groupActions.run_pipeline, action_upload_pdf: groupActions.upload_pdf,
      action_download_excel: groupActions.download_excel, action_delete_jobs: groupActions.delete_jobs,
      action_export_data: groupActions.export_data,
      data_scope: groupScope, member_ids: groupMembers,
    };
    try {
      if (editingGroup?.id) {
        await api.updateGroup(editingGroup.id, data);
        groupMessage = 'GROUP UPDATED';
      } else {
        await api.createGroup(data);
        groupMessage = 'GROUP CREATED';
      }
      await loadData();
    } catch (e: any) { groupMessage = e.message; }
    groupSaving = false;
  }

  async function deleteGroup() {
    if (!editingGroup?.id) return;
    try {
      await api.deleteGroup(editingGroup.id);
      editingGroup = null;
      await loadData();
    } catch (e: any) { groupMessage = e.message; }
  }

  function toggleMember(uid: number) {
    if (groupMembers.includes(uid)) groupMembers = groupMembers.filter(id => id !== uid);
    else groupMembers = [...groupMembers, uid];
  }



  onMount(() => { loadData(); loadKeycloakSettings(); });

  // ===== Activity Log (enhanced) =====
  let logSubTab = $state<'all'|'security'|'jobs'|'users'>('all');
  let logEvents = $state<any[]>([]);
  let logTotal = $state(0);
  let logLimit = $state(50);
  let logOffset = $state(0);
  let logStats = $state<any>(null);
  let logFilter = $state<any>({ action: '', status: '', user: '', search: '', date_from: '', date_to: '' });
  let drawerEvent = $state<any>(null);

  const VIEW_ACTIONS: Record<string, string> = {
    security: 'LOGIN_FAILED,LDAP_BIND_FAIL,LDAP_TEST,DELETE_USER,ROLE_CHANGE,CONFIG_CHANGE,LOCKOUT,MFA_FAIL,TOKEN_REVOKED,LDAP_DELETE',
    jobs:     'JOB_START,JOB_SUCCESS,JOB_FAIL,RUN_JOB,EXPORT_EXCEL,JOB_REVIEWED,JOB_CORRECTED',
    users:    'USER_CREATE,USER_UPDATE,USER_DELETE,ROLE_CHANGE,PASSWORD_RESET,USER_ENABLE,USER_DISABLE,GROUP_CREATE,GROUP_UPDATE,GROUP_DELETE,GROUP_MEMBER_ADD,GROUP_MEMBER_REMOVE',
  };

  async function loadLogs() {
    const headers = { 'Authorization': `Bearer ${auth.token}` };
    const qs = Object.entries(logFilter)
      .filter(([_, v]) => v)
      .map(([k, v]) => `&${k}=${encodeURIComponent(v as string)}`)
      .join('');
    let url = `/api/activity/?limit=${logLimit}&offset=${logOffset}${qs}`;
    const viewActions = VIEW_ACTIONS[logSubTab];
    if (viewActions) url += `&action_in=${encodeURIComponent(viewActions)}`;
    try {
      const r = await fetch(url, { headers });
      if (r.ok) {
        const j = JSON.parse(await r.text());
        logEvents = j.events || [];
        logTotal = j.total || 0;
      }
    } catch (e) { console.error(e); }
  }

  async function loadStats() {
    const headers = { 'Authorization': `Bearer ${auth.token}` };
    try {
      const r = await fetch('/api/activity/stats', { headers });
      if (r.ok) logStats = JSON.parse(await r.text());
    } catch (e) { console.error(e); }
  }

  async function openDrawer(id: number) {
    const headers = { 'Authorization': `Bearer ${auth.token}` };
    try {
      const r = await fetch(`/api/activity/${id}`, { headers });
      if (r.ok) drawerEvent = JSON.parse(await r.text());
    } catch (e) { console.error(e); }
  }

  async function exportCsv() {
    const params = new URLSearchParams(
      Object.entries(logFilter).filter(([_, v]) => v) as any
    ).toString();
    const url = `/api/activity/export/csv${params ? '?' + params : ''}`;
    window.open(url, '_blank');
  }

  function statusColor(s: string) {
    if (s === 'OK') return '#10b981';
    if (s === 'FAILED') return '#ef4444';
    if (s === 'WARN') return '#f59e0b';
    return 'var(--on-surface)';
  }

  function rowBg(e: any) {
    const a = e.action || '';
    const s = e.status || '';
    if (s === 'FAILED') return 'background: rgba(239,68,68,0.10);';
    if (a === 'DELETE_USER' || a === 'ROLE_CHANGE') return 'background: rgba(239,68,68,0.05);';
    if (a.startsWith('JOB_')) return 'background: rgba(59,130,246,0.05);';
    if (a.startsWith('LDAP_')) return 'background: rgba(245,158,11,0.05);';
    if (a === 'CONFIG_CHANGE') return 'background: rgba(168,85,247,0.05);';
    return '';
  }

  let _logsLoaded = $state(false);
  $effect(() => {
    if (activeTab === 'logs' && !_logsLoaded) {
      _logsLoaded = true;
      // Untracked reload — won't re-fire on filter changes
      queueMicrotask(() => { loadStats(); loadLogs(); });
    }
  });

  function applyLogFilters() {
    logOffset = 0;
    loadLogs();
  }
</script>


<ChapterHeading icon="settings" title="ADMIN_PANEL" subtitle="System settings, user management, and authentication" question="Configure the system" />

{#if !auth.isAdmin}
  <div class="p-8 text-center font-bold uppercase" style="color: var(--error);">ADMIN ACCESS REQUIRED</div>
{:else}
  <!-- Tab bar -->
  <div class="flex gap-0 mb-4 border-2" style="border-color: var(--on-surface); background: var(--surface-container-highest);">
    {#each [['users','USERS'],['logs','ACTIVITY_LOG'],['auth','AUTHENTICATION'],['groups','GROUPS'],['ldap','LDAP'],['storage','STORAGE'],['auto_approve','AUTO_APPROVE']] as [key, label]}
      <button class="px-3 py-2 text-[11px] font-bold uppercase tracking-tight cursor-pointer"
        style="{activeTab === key ? 'background: var(--on-surface); color: var(--surface);' : 'color: var(--outline);'}"
        onclick={() => activeTab = key as any}
      >{label}</button>
    {/each}
  </div>

  {#if loading && !['auth','groups'].includes(activeTab)}
    <div class="skeleton h-64 w-full"></div>

  {:else if activeTab === 'users'}
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 space-y-4">
        <DataTable title="REGISTERED_USERS" count={users.length} columns={userColumns} rows={userRows} />

        {#if !auth.isKeycloak && auth.user?.auth_type !== 'keycloak'}
          <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
            <div class="dark-bar">CHANGE_MY_PASSWORD</div>
            <div class="bg-white p-3 space-y-2">
              {#if myPwError}<div class="p-2 text-xs font-bold uppercase text-white" style="background: var(--error);">{myPwError}</div>{/if}
              {#if myPwSuccess}<div class="p-2 text-xs font-bold uppercase text-white" style="background: var(--primary);">{myPwSuccess}</div>{/if}
              <div class="grid grid-cols-1 md:grid-cols-3 gap-2">
                <FormInput label="CURRENT_PASSWORD" type="password" bind:value={myCurrentPw} placeholder="current" />
                <FormInput label="NEW_PASSWORD" type="password" bind:value={myNewPw} placeholder="≥8 chars" />
                <FormInput label="CONFIRM_NEW" type="password" bind:value={myConfirmPw} placeholder="repeat new" />
              </div>
              <Button variant="secondary" size="md" onclick={changeMyPassword}>{myPwBusy ? '… SAVING' : 'UPDATE_MY_PASSWORD'}</Button>
            </div>
          </div>
        {/if}
      </div>
      {#if !auth.isKeycloak}
        <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
          <div class="dark-bar">CREATE_USER</div>
          <div class="bg-white p-3 space-y-3">
            {#if createError}<div class="p-2 text-xs font-bold uppercase text-white" style="background: var(--error);">{createError}</div>{/if}
            {#if createSuccess}<div class="p-2 text-xs font-bold uppercase text-white" style="background: var(--primary);">{createSuccess}</div>{/if}
            <FormInput label="USERNAME" bind:value={newUsername} placeholder="username" />
            <FormInput label="PASSWORD" type="password" bind:value={newPassword} placeholder="password" />
            <FormInput label="DISPLAY_NAME" bind:value={newDisplayName} placeholder="Display Name" />
            <div>
              <div class="tag-label mb-0.5">ROLE</div>
              <select bind:value={newRole} class="w-full px-2 py-1.5 text-sm font-bold uppercase border-2" style="border-color: var(--on-surface);">
                <option value="user">USER</option>
                <option value="admin">ADMIN</option>
              </select>
            </div>
            <Button variant="secondary" size="md" onclick={createUser}>CREATE_OPERATOR</Button>
          </div>
        </div>
      {:else}
        <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
          <div class="dark-bar">AUTH_PROVIDER</div>
          <div class="bg-white p-3">
            <div class="flex items-center gap-2 mb-2">
              <span class="inline-block w-2 h-2" style="background: var(--primary);"></span>
              <span class="text-xs font-bold uppercase" style="color: var(--primary);">KEYCLOAK_ACTIVE</span>
            </div>
            <p class="text-[10px] font-bold uppercase" style="color: var(--outline);">Users auto-provisioned on first login via Keycloak.</p>
          </div>
        </div>
      {/if}
    </div>

  {:else if activeTab === 'logs'}
    <!-- KPI Strip -->
    <div class="grid grid-cols-2 md:grid-cols-6 gap-2 mb-3">
      {#each [
        ['Total', logStats?.total ?? '—'],
        ['Today', logStats?.today ?? '—'],
        ['Failed Logins', logStats?.failed_logins ?? '—'],
        ['Jobs', `${logStats?.jobs_total ?? 0}/${logStats?.jobs_fail ?? 0} fail`],
        ['Unique Users', logStats?.unique_users ?? '—'],
        ['Top Action', logStats?.top_action ?? '—'],
      ] as [label, val]}
        <div class="border-2 p-2" style="border-color: var(--on-surface); background: var(--surface);">
          <div class="text-[8px] font-black uppercase opacity-60">{label}</div>
          <div class="text-sm font-mono font-bold">{val}</div>
        </div>
      {/each}
    </div>

    <!-- Compact 1-row filter bar (auto-apply on change, no APPLY button) -->
    <div class="border-2 p-2 mb-3 flex flex-wrap items-center gap-2" style="border-color: var(--on-surface); background: var(--surface);">
      <span class="text-[8px] font-black uppercase opacity-60 px-1">VIEW</span>
      <select bind:value={logSubTab}
        class="text-[10px] font-mono px-2 py-1.5 cursor-pointer"
        style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);">
        <option value="all">ALL</option>
        <option value="security">SECURITY</option>
        <option value="jobs">JOBS</option>
        <option value="users">USERS</option>
      </select>

      <span class="text-[8px] font-black uppercase opacity-60 px-1 ml-2">FROM</span>
      <input type="date" bind:value={logFilter.date_from}
        class="text-[10px] font-mono px-2 py-1.5"
        style="border: 2px solid var(--on-surface); background: white;" />

      <span class="text-[8px] font-black uppercase opacity-60 px-1">TO</span>
      <input type="date" bind:value={logFilter.date_to}
        class="text-[10px] font-mono px-2 py-1.5"
        style="border: 2px solid var(--on-surface); background: white;" />

      <span class="text-[8px] font-black uppercase opacity-60 px-1 ml-2">ACTION</span>
      <select bind:value={logFilter.action}
        class="text-[10px] font-mono px-2 py-1.5 cursor-pointer"
        style="border: 2px solid var(--on-surface); background: white;">
        <option value="">ALL</option>
        {#each ['LOGIN','LOGIN_FAILED','LOGOUT','RUN_JOB','JOB_START','JOB_SUCCESS','JOB_FAIL','LDAP_TEST','LDAP_CREATE','LDAP_DELETE','USER_CREATE','USER_DELETE','ROLE_CHANGE','CONFIG_CHANGE'] as a}
          <option>{a}</option>
        {/each}
      </select>

      <span class="text-[8px] font-black uppercase opacity-60 px-1">STATUS</span>
      <select bind:value={logFilter.status}
        class="text-[10px] font-mono px-2 py-1.5 cursor-pointer"
        style="border: 2px solid var(--on-surface); background: white;">
        <option value="">ALL</option>
        <option>OK</option>
        <option>FAILED</option>
        <option>WARN</option>
      </select>

      <input type="text" placeholder="🔍 search user / details / resource / error" bind:value={logFilter.search}
        onkeydown={(e) => { if (e.key === 'Enter') applyLogFilters(); }}
        class="flex-1 min-w-[180px] text-[10px] font-mono px-2 py-1.5"
        style="border: 2px solid var(--on-surface); background: white;" />

      <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
        style="border: 2px solid var(--on-surface); background: var(--on-surface); color: var(--surface);"
        onclick={applyLogFilters}>APPLY</button>

      <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
        style="border: 2px solid var(--on-surface); background: transparent;"
        onclick={exportCsv}>↓ CSV</button>
    </div>

    <!-- Excel-style Table -->
    <ExcelTable
      columns={[
        { id: 'timestamp', header: 'TIMESTAMP', accessor: 'timestamp', width: 150, frozen: true },
        { id: 'user', header: 'USER', accessor: 'user', width: 90 },
        { id: 'auth_source', header: 'AUTH', accessor: 'auth_source', width: 90 },
        { id: 'action', header: 'ACTION', accessor: 'action', width: 130 },
        { id: 'status', header: 'STATUS', accessor: 'status', width: 100,
          cell: (r: any) => r.status === 'OK' ? '✓ OK' : r.status === 'FAILED' ? '✗ FAILED' : (r.status || '—') },
        { id: 'ip_address', header: 'IP', accessor: 'ip_address', width: 130 },
        { id: 'duration_ms', header: 'DURATION', accessor: 'duration_ms', width: 90,
          cell: (r: any) => r.duration_ms ? `${r.duration_ms}ms` : '—' },
        { id: 'resource', header: 'RESOURCE', accessor: 'resource', width: 130 },
        { id: 'details', header: 'DETAILS', accessor: 'details', width: 400 },
      ]}
      data={logEvents}
      enableSort={true}
      enableResize={true}
      exportFilename="activity_log.csv"
      onRowClick={(r: any) => openDrawer(r.id)}
      rowClass={(r: any) => r.status === 'FAILED' ? 'bg-red-50/50' : (r.action || '').startsWith('JOB_') ? 'bg-blue-50/30' : ''}
    />
    <div class="flex justify-between items-center p-3 border-2 border-t-0 stamp-shadow" style="border-color: var(--on-surface); background: var(--surface);">
      <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
        style="border: 2px solid var(--on-surface);" disabled={logOffset === 0}
        onclick={() => { logOffset = Math.max(0, logOffset - logLimit); loadLogs(); }}>« PREV</button>
      <span class="text-[10px] font-mono">{logOffset + 1} – {Math.min(logOffset + logLimit, logTotal)} of {logTotal}</span>
      <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
        style="border: 2px solid var(--on-surface);" disabled={logOffset + logLimit >= logTotal}
        onclick={() => { logOffset += logLimit; loadLogs(); }}>NEXT »</button>
    </div>

  {:else if activeTab === 'auth'}
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <!-- LEFT: Keycloak config form (2 cols wide) -->
      <div class="lg:col-span-2 border-2 stamp-shadow" style="border-color: var(--on-surface);">
        <div class="dark-bar flex items-center justify-between">
          <span>KEYCLOAK_CONFIGURATION</span>
          <button class="px-3 py-1 text-[10px] font-black uppercase cursor-pointer border-2"
            style="border-color: {kcEnabled ? 'white' : 'rgba(255,255,255,0.4)'}; background: {kcEnabled ? 'var(--primary)' : 'transparent'}; color: white;"
            onclick={() => kcEnabled = !kcEnabled}>{kcEnabled ? 'ENABLED' : 'DISABLED'}</button>
        </div>
        <div class="bg-white p-4">
          {#if kcMessage}<div class="mb-3 p-2 text-xs font-bold uppercase text-white border-2" style="background: {kcMessageType === 'success' ? 'var(--primary)' : 'var(--error)'}; border-color: var(--on-surface);">{kcMessage}</div>{/if}
          {#if kcTestResult}<div class="mb-3 p-2 text-xs font-bold uppercase border-2" style="background: {kcTestType === 'success' ? '#C6EFCE' : '#FFC7CE'}; border-color: var(--on-surface);">{kcTestResult}</div>{/if}
          <div class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3">
            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">REALM_URL</div>
              <input bind:value={kcRealmUrl} placeholder="https://keycloak.example.com/realms/myapp" class="w-full font-bold text-sm focus:outline-none" style="padding: 8px 10px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface);" />
            </div>
            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">CLIENT_ID</div>
              <input bind:value={kcClientId} placeholder="ro-ed-frontend" class="w-full font-bold text-sm focus:outline-none" style="padding: 8px 10px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface);" />
            </div>
            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">CLIENT_SECRET <span style="color: var(--outline); font-weight: 500;">(optional)</span></div>
              <input bind:value={kcClientSecret} placeholder="Leave empty for public client (PKCE)" class="w-full font-bold text-sm focus:outline-none" style="padding: 8px 10px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface);" />
            </div>
            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">ADMIN_ROLE_NAME</div>
              <input bind:value={kcAdminRole} placeholder="admin" class="w-full font-bold text-sm focus:outline-none" style="padding: 8px 10px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface);" />
            </div>
          </div>
          <div class="flex gap-3 mt-4">
            <button onclick={testKeycloakConnection} disabled={kcTesting || !kcRealmUrl} class="px-4 py-2.5 text-xs font-black uppercase cursor-pointer border-2" class:opacity-50={kcTesting || !kcRealmUrl} style="border-color: var(--on-surface); background: var(--surface-container-highest);">{kcTesting ? 'TESTING...' : 'TEST_CONNECTION'}</button>
            <button onclick={saveKeycloakSettings} disabled={kcSaving} class="flex-1 px-4 py-2.5 text-xs font-black uppercase cursor-pointer border-2 press-effect" class:opacity-50={kcSaving} style="border-color: var(--on-surface); background: var(--primary-container); box-shadow: 3px 3px 0px 0px var(--on-surface);">{kcSaving ? 'SAVING...' : 'SAVE_CONFIGURATION'}</button>
          </div>
        </div>
      </div>

      <!-- RIGHT: Keycloak setup guide -->
      <div class="border-2" style="border-color: var(--on-surface);">
        <div class="dark-bar">KEYCLOAK_SETUP_GUIDE</div>
        <div class="bg-white p-3 space-y-3 text-[10px] font-bold uppercase" style="color: var(--on-surface);">
          <div>
            <div style="color: var(--secondary);">1. CLIENT_TYPE</div>
            <div style="color: var(--outline);">Public (PKCE) or Confidential (with secret)</div>
          </div>
          <div>
            <div style="color: var(--secondary);">2. VALID_REDIRECT_URIS</div>
            <div class="px-2 py-1 mt-0.5 font-mono text-[9px]" style="background: var(--surface-container); color: var(--on-surface); word-break: break-all;">{typeof window !== 'undefined' ? window.location.origin : 'https://your-app'}/*</div>
          </div>
          <div>
            <div style="color: var(--secondary);">3. POST_LOGOUT_REDIRECT_URIS</div>
            <div class="px-2 py-1 mt-0.5 font-mono text-[9px]" style="background: var(--surface-container); word-break: break-all;">{typeof window !== 'undefined' ? window.location.origin : 'https://your-app'}/*</div>
          </div>
          <div>
            <div style="color: var(--secondary);">4. WEB_ORIGINS</div>
            <div class="px-2 py-1 mt-0.5 font-mono text-[9px]" style="background: var(--surface-container); word-break: break-all;">{typeof window !== 'undefined' ? window.location.origin : 'https://your-app'}</div>
          </div>
          <div>
            <div style="color: var(--secondary);">5. REALM_ROLES_NEEDED</div>
            <div class="flex gap-1 mt-0.5">
              <span class="px-1.5 py-0.5 text-[9px]" style="background: var(--on-surface); color: var(--surface);">{kcAdminRole || 'admin'}</span>
              <span class="px-1.5 py-0.5 text-[9px]" style="background: var(--outline); color: white;">user</span>
            </div>
            <div class="mt-0.5" style="color: var(--outline);">Users without "{kcAdminRole || 'admin'}" role get "user" role</div>
          </div>
          <div class="pt-1" style="border-top: 1px solid var(--surface-container-highest);">
            <div style="color: var(--secondary);">IMPLEMENTATION</div>
            <div class="space-y-1 mt-1" style="color: var(--outline);">
              <div>OIDC Auth Code + PKCE (no keycloak-js needed)</div>
              <div>Session init on app start via auth.init()</div>
              <div>Routes protected by layout guard</div>
              <div>Bearer token in all API requests</div>
              <div>Logout terminates Keycloak session</div>
              <div>Token auto-refresh 60s before expiry</div>
            </div>
          </div>
        </div>
      </div>
    </div>

  {:else if activeTab === 'groups'}
    <!-- GROUPS MANAGEMENT -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <!-- Group list -->
      <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
        <div class="dark-bar flex items-center justify-between">
          <span>GROUPS</span>
          <button class="px-2 py-0.5 text-[10px] font-black uppercase cursor-pointer" style="color: var(--primary-container);" onclick={startNewGroup}>+ NEW</button>
        </div>
        <div class="bg-white">
          {#if groups.length === 0}
            <div class="p-4 text-xs font-bold uppercase text-center" style="color: var(--outline);">No groups created yet</div>
          {:else}
            {#each groups as g}
              <button class="w-full text-left px-4 py-3 flex items-center justify-between cursor-pointer"
                style="border-bottom: 1px solid var(--surface-container-highest); {editingGroup?.id === g.id ? 'background: var(--surface-container);' : ''}"
                onclick={() => editGroup(g)}>
                <div>
                  <div class="text-sm font-bold uppercase" style="color: var(--on-surface);">{g.name}</div>
                  <div class="text-[10px] font-bold uppercase" style="color: var(--outline);">{g.member_count} member{g.member_count !== 1 ? 's' : ''}</div>
                </div>
                <span class="text-[10px] font-bold uppercase" style="color: var(--secondary);">EDIT</span>
              </button>
            {/each}
          {/if}
        </div>
      </div>

      <!-- Group editor -->
      {#if editingGroup}
        <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
          <div class="dark-bar">{editingGroup.id ? 'EDIT_GROUP' : 'CREATE_GROUP'}</div>
          <div class="bg-white p-4 space-y-3">
            {#if groupMessage}<div class="p-2 text-xs font-bold uppercase" style="color: var(--primary);">{groupMessage}</div>{/if}

            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">GROUP_NAME</div>
              <input bind:value={groupName} placeholder="e.g. Operators" class="w-full font-bold text-sm focus:outline-none" style="padding: 6px 10px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface);" />
            </div>

            <!-- Pages -->
            <div>
              <div class="tag-label mb-1" style="font-size: 9px;">PAGES</div>
              <div class="flex flex-wrap gap-2">
                {#each Object.entries(groupPages) as [key, val]}
                  <button class="px-2 py-1 text-[10px] font-black uppercase cursor-pointer border"
                    style="border-color: var(--on-surface); background: {val ? 'var(--primary)' : 'white'}; color: {val ? 'white' : 'var(--outline)'};"
                    onclick={() => groupPages = {...groupPages, [key]: !val}}>{key}</button>
                {/each}
              </div>
            </div>

            <!-- Actions -->
            <div>
              <div class="tag-label mb-1" style="font-size: 9px;">ACTIONS</div>
              <div class="flex flex-wrap gap-2">
                {#each Object.entries(groupActions) as [key, val]}
                  <button class="px-2 py-1 text-[10px] font-black uppercase cursor-pointer border"
                    style="border-color: var(--on-surface); background: {val ? 'var(--secondary)' : 'white'}; color: {val ? 'white' : 'var(--outline)'};"
                    onclick={() => groupActions = {...groupActions, [key]: !val}}>{key.replace(/_/g, ' ')}</button>
                {/each}
              </div>
            </div>

            <!-- Data scope -->
            <div>
              <div class="tag-label mb-1" style="font-size: 9px;">DATA_SCOPE</div>
              <div class="flex gap-2">
                {#each [['own', 'OWN DATA'], ['all_readonly', 'ALL (READ)'], ['all_full', 'ALL (FULL)']] as [val, label]}
                  <button class="px-2 py-1 text-[10px] font-black uppercase cursor-pointer border"
                    style="border-color: var(--on-surface); background: {groupScope === val ? 'var(--on-surface)' : 'white'}; color: {groupScope === val ? 'var(--surface)' : 'var(--outline)'};"
                    onclick={() => groupScope = val}>{label}</button>
                {/each}
              </div>
            </div>

            <!-- Members -->
            <div>
              <div class="tag-label mb-1" style="font-size: 9px;">MEMBERS</div>
              <div class="flex flex-wrap gap-2 max-h-24 overflow-y-auto">
                {#each users.filter(u => u.role !== 'admin') as u}
                  <button class="px-2 py-1 text-[10px] font-bold uppercase cursor-pointer border"
                    style="border-color: var(--on-surface); background: {groupMembers.includes(u.id) ? 'var(--tertiary)' : 'white'}; color: {groupMembers.includes(u.id) ? 'white' : 'var(--outline)'};"
                    onclick={() => toggleMember(u.id)}>{u.username}</button>
                {/each}
                {#if users.filter(u => u.role !== 'admin').length === 0}
                  <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">No non-admin users yet</span>
                {/if}
              </div>
            </div>

            <!-- Buttons -->
            <div class="flex gap-2 pt-1">
              {#if editingGroup.id}
                <button onclick={deleteGroup} class="px-3 py-2 text-[10px] font-black uppercase cursor-pointer border-2" style="border-color: var(--error); color: var(--error);">DELETE</button>
              {/if}
              <button onclick={saveGroup} disabled={groupSaving || !groupName.trim()}
                class="flex-1 px-3 py-2 text-[10px] font-black uppercase cursor-pointer border-2 press-effect"
                class:opacity-50={groupSaving || !groupName.trim()}
                style="border-color: var(--on-surface); background: var(--primary-container); box-shadow: 2px 2px 0px 0px var(--on-surface);">
                {groupSaving ? 'SAVING...' : 'SAVE_GROUP'}
              </button>
              <button onclick={() => editingGroup = null} class="px-3 py-2 text-[10px] font-black uppercase cursor-pointer border-2" style="border-color: var(--on-surface); color: var(--outline);">CANCEL</button>
            </div>
          </div>
        </div>
      {:else}
        <div class="border-2 flex items-center justify-center p-8" style="border-color: var(--surface-container-highest); border-style: dashed;">
          <div class="text-center">
            <div class="text-xs font-bold uppercase" style="color: var(--outline);">Select a group to edit or</div>
            <button class="mt-2 px-4 py-2 text-xs font-black uppercase cursor-pointer border-2 press-effect"
              style="border-color: var(--on-surface); background: var(--primary-container); box-shadow: 2px 2px 0px 0px var(--on-surface);"
              onclick={startNewGroup}>CREATE_NEW_GROUP</button>
          </div>
        </div>
      {/if}
    </div>

  {:else if activeTab === 'ldap'}
    {#if ldapToast}
      <div class="mb-3 p-2 text-xs font-mono border-2"
           style="border-color: {ldapToast.type === 'ok' ? '#10b981' : '#ef4444'}; background: {ldapToast.type === 'ok' ? '#d1fae5' : '#fee2e2'}; color: var(--on-surface);">
        [{ldapToast.type === 'ok' ? 'OK' : 'ERR'}] {ldapToast.msg}
        <button class="ml-2 underline cursor-pointer" onclick={() => ldapToast = null}>×</button>
      </div>
    {/if}

    <div class="flex justify-end mb-2">
      <button
        class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
        style="background: #10b981; color: white; border: 2px solid var(--on-surface);"
        onclick={() => ldapEditing = { port: 389, attr_username: 'uid', attr_mail: 'mail', attr_groups: 'memberOf', priority: 50, active: true, use_tls: false, validate_cert: true }}
      >+ ADD_LDAP</button>
    </div>

    <ExcelTable
      title="LDAP_SERVERS"
      columns={[
        { id: 'label', header: 'LABEL', accessor: 'label', width: 200, frozen: true },
        { id: 'host_port', header: 'HOST:PORT', accessor: 'host', width: 220,
          cell: (r) => `${r.host}:${r.port}` },
        { id: 'use_tls', header: 'TLS', accessor: 'use_tls', width: 70, align: 'center',
          cell: (r) => r.use_tls ? 'TLS' : '—' },
        { id: 'priority', header: 'PRI', accessor: 'priority', width: 60, align: 'center' },
        { id: 'active', header: 'ACTIVE', accessor: 'active', width: 80, align: 'center',
          cell: (r) => r.active ? 'YES' : 'NO' },
        { id: 'bind_dn', header: 'BIND_DN', accessor: 'bind_dn', width: 240 },
        { id: 'search_base', header: 'SEARCH_BASE', accessor: 'search_base', width: 200 },
        { id: 'created_at', header: 'CREATED', accessor: 'created_at', width: 150,
          cell: (r) => (r.created_at || '').slice(0, 19) },
        { id: 'actions', header: 'ACTIONS', accessor: 'id', width: 200, align: 'center', enableSort: false,
          cell: (r) => `id=${r.id}` },
      ]}
      data={ldaps}
      enableSort={true}
      enableResize={true}
      exportFilename="ldap_configs.csv"
      onRowClick={(r) => ldapEditing = { ...r, bind_password: '' }}
    />
    <div class="text-[9px] font-mono opacity-60 mt-1 px-1">click row to edit</div>

    {#if ldapEditing !== null}
      <div class="fixed inset-0 z-50 flex items-center justify-center p-4"
           style="background: rgba(0,0,0,0.5);"
           onclick={() => ldapEditing = null}>
        <div class="border-2 stamp-shadow max-w-2xl w-full max-h-[90vh] overflow-y-auto p-6"
             style="border-color: var(--on-surface); background: var(--surface);"
             onclick={(e) => e.stopPropagation()}>
          <div class="flex items-center justify-between mb-4 -mx-6 -mt-6 px-6 py-3"
               style="background: var(--on-surface); color: var(--surface);">
            <span class="text-xs font-black uppercase tracking-wider">{ldapEditing.id ? 'EDIT_LDAP' : 'ADD_LDAP'}</span>
            <button class="text-[10px] font-black uppercase cursor-pointer" style="color: var(--surface);"
                    onclick={() => ldapEditing = null}>×</button>
          </div>
          <div class="grid grid-cols-2 gap-3">
            {#each [
              ['label','LABEL','text', 'My LDAP'],
              ['host','HOST','text', 'ldap.example.com'],
              ['port','PORT','number', '389'],
              ['bind_dn','BIND_DN','text', 'cn=admin,dc=example,dc=com'],
              ['bind_password', ldapEditing.id ? 'BIND_PASSWORD (LEAVE BLANK TO KEEP)' : 'BIND_PASSWORD','password', '••••••••'],
              ['search_base','SEARCH_BASE','text', 'ou=users,dc=example,dc=com'],
              ['search_filter','SEARCH_FILTER (OPTIONAL)','text', '(objectClass=person)'],
              ['attr_username','ATTR_USERNAME','text', 'uid'],
              ['attr_mail','ATTR_MAIL','text', 'mail'],
              ['attr_groups','ATTR_GROUPS','text', 'memberOf'],
              ['email_domain_hint','EMAIL_DOMAIN_HINT','text', 'example.com'],
              ['priority','PRIORITY','number', '50'],
            ] as [k, label, type, ph]}
              <label class="block">
                <span class="text-[8px] font-black uppercase tag-label">{label}</span>
                <input
                  type={type}
                  value={ldapEditing[k] ?? ''}
                  placeholder={ph}
                  oninput={(e) => ldapEditing[k] = type === 'number' ? +e.currentTarget.value : e.currentTarget.value}
                  class="w-full mt-1 px-2 py-1.5 text-[10px] font-mono focus:outline-none"
                  style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);"
                />
              </label>
            {/each}
          </div>

          <div class="mt-4 flex flex-wrap gap-2">
            {#each [['use_tls','USE_TLS'],['validate_cert','VALIDATE_CERT'],['active','ACTIVE']] as [k, label]}
              <button
                class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                style="border: 2px solid var(--on-surface); background: {ldapEditing[k] ? 'var(--primary)' : 'white'}; color: {ldapEditing[k] ? 'white' : 'var(--outline)'};"
                onclick={() => ldapEditing[k] = !ldapEditing[k]}
              >[{ldapEditing[k] ? 'X' : ' '}] {label}</button>
            {/each}
          </div>

          <div class="flex justify-end gap-2 mt-4">
            <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                    style="border: 2px solid var(--on-surface); background: transparent; color: var(--on-surface);"
                    onclick={() => ldapEditing = null}>CANCEL</button>
            <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                    style="background: #10b981; color: white; border: 2px solid var(--on-surface);"
                    onclick={saveLdap}>{ldapEditing.id ? 'UPDATE' : 'CREATE'}</button>
          </div>
        </div>
      </div>
    {/if}

  {:else if activeTab === 'storage'}
    {#if storageToast}
      <div class="mb-3 p-2 text-xs font-mono border-2"
           style="border-color: {storageToast.type === 'ok' ? '#10b981' : '#ef4444'}; background: {storageToast.type === 'ok' ? '#d1fae5' : '#fee2e2'}; color: var(--on-surface);">
        [{storageToast.type === 'ok' ? 'OK' : 'ERR'}] {storageToast.msg}
        <button class="ml-2 underline cursor-pointer" onclick={() => storageToast = null}>×</button>
      </div>
    {/if}

    <div class="flex justify-end mb-2">
      <button
        class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
        style="background: #10b981; color: white; border: 2px solid var(--on-surface);"
        onclick={() => storageEditing = { provider: 'aws', label: '', endpoint_url: '', region_name: '', bucket_name: '', access_key_id: '', secret_access_key: '', key_prefix: '', use_ssl: true, addressing_style: 'auto', signature_version: 's3v4', use_for_uploads: true, use_for_exports: true, use_for_cache: false, use_for_archive: false, active: true }}
      >+ ADD_STORAGE</button>
    </div>

    <ExcelTable
      title="STORAGE_CONFIGS"
      columns={[
        { id: 'label', header: 'LABEL', accessor: 'label', width: 180, frozen: true },
        { id: 'provider', header: 'PROVIDER', accessor: 'provider', width: 110,
          cell: (r) => (r.provider || '').toUpperCase() },
        { id: 'bucket', header: 'BUCKET', accessor: 'bucket_name', width: 180 },
        { id: 'active', header: 'ACTIVE', accessor: 'active', width: 70, align: 'center',
          cell: (r) => r.active ? 'YES' : 'NO' },
        { id: 'use_for', header: 'USE_FOR', accessor: 'id', width: 220, enableSort: false,
          cell: (r) => [
            r.use_for_uploads ? 'UP' : '',
            r.use_for_exports ? 'EX' : '',
            r.use_for_cache ? 'CA' : '',
            r.use_for_archive ? 'AR' : '',
          ].filter(Boolean).join(' / ') || '—' },
        { id: 'created_at', header: 'CREATED', accessor: 'created_at', width: 150,
          cell: (r) => (r.created_at || '').slice(0, 19) },
        { id: 'actions', header: 'ACTIONS', accessor: 'id', width: 280, align: 'center', enableSort: false,
          cell: (r) => `id=${r.id}` },
      ]}
      data={storageConfigs}
      enableSort={true}
      enableResize={true}
      exportFilename="storage_configs.csv"
      onRowClick={(r) => storageEditing = { ...r, secret_access_key: '' }}
    />

    <div class="text-[9px] font-mono opacity-60 mt-1 px-1">click row to edit</div>

    <!-- Per-row action buttons strip -->
    {#if storageConfigs.length > 0}
      <div class="mt-2 border-2" style="border-color: var(--on-surface); background: var(--surface);">
        {#each storageConfigs as c}
          <div class="flex items-center gap-2 px-3 py-2" style="border-bottom: 1px solid var(--surface-container-highest);">
            <span class="text-[10px] font-mono font-bold flex-1" style="color: var(--on-surface);">
              {c.label} <span class="opacity-60">[{(c.provider||'').toUpperCase()}]</span>
              {#if c.active}<span class="ml-2 px-1.5 py-0.5 text-[8px]" style="background: #10b981; color: white;">ACTIVE</span>{/if}
            </span>
            <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
                    style="border: 2px solid var(--on-surface); background: white;"
                    disabled={storageTesting === c.id}
                    onclick={() => testStorage(c.id)}>{storageTesting === c.id ? 'TESTING…' : 'TEST'}</button>
            {#if !c.active}
              <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
                      style="border: 2px solid var(--on-surface); background: var(--primary-container);"
                      onclick={() => activateStorage(c.id)}>ACTIVATE</button>
            {/if}
            <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
                    style="border: 2px solid var(--on-surface); background: white;"
                    onclick={() => storageEditing = { ...c, secret_access_key: '' }}>EDIT</button>
            <button class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
                    style="border: 2px solid var(--error); color: var(--error); background: white;"
                    onclick={() => deleteStorage(c.id)}>DELETE</button>
          </div>
        {/each}
      </div>
    {/if}

    {#if storageEditing !== null}
      <div class="fixed inset-0 z-50 flex items-center justify-center p-4"
           style="background: rgba(0,0,0,0.5);"
           onclick={() => storageEditing = null}>
        <div class="border-2 stamp-shadow max-w-3xl w-full max-h-[90vh] overflow-y-auto p-6"
             style="border-color: var(--on-surface); background: var(--surface);"
             onclick={(e) => e.stopPropagation()}>
          <div class="flex items-center justify-between mb-4 -mx-6 -mt-6 px-6 py-3"
               style="background: var(--on-surface); color: var(--surface);">
            <span class="text-xs font-black uppercase tracking-wider">{storageEditing.id ? 'EDIT_STORAGE' : 'ADD_STORAGE'}</span>
            <button class="text-[10px] font-black uppercase cursor-pointer" style="color: var(--surface);"
                    onclick={() => storageEditing = null}>×</button>
          </div>

          <!-- Provider radio -->
          <div class="mb-3">
            <div class="text-[8px] font-black uppercase tag-label mb-1">PROVIDER</div>
            <div class="flex flex-wrap gap-2">
              {#each [['aws','AWS'],['minio','MINIO'],['r2','R2'],['wasabi','WASABI'],['backblaze','BACKBLAZE'],['custom','CUSTOM']] as [val, label]}
                <button class="text-[9px] font-black uppercase px-3 py-1.5 cursor-pointer"
                        style="border: 2px solid var(--on-surface); background: {storageEditing.provider === val ? 'var(--on-surface)' : 'white'}; color: {storageEditing.provider === val ? 'var(--surface)' : 'var(--on-surface)'};"
                        onclick={() => applyPreset(val)}>{label}</button>
              {/each}
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3">
            {#each [
              ['label','LABEL','text', 'Production S3'],
              ['endpoint_url','ENDPOINT_URL','text', 'https://s3.us-east-1.amazonaws.com'],
              ['region_name','REGION','text', 'us-east-1'],
              ['bucket_name','BUCKET','text', 'my-bucket'],
              ['access_key_id','ACCESS_KEY_ID','text', 'AKIA...'],
              ['secret_access_key', storageEditing.id ? 'SECRET_ACCESS_KEY (LEAVE BLANK TO KEEP)' : 'SECRET_ACCESS_KEY','password', '••••••••'],
              ['key_prefix','KEY_PREFIX (OPTIONAL)','text', 'roed/'],
              ['signature_version','SIGNATURE_VERSION','text', 's3v4'],
            ] as [k, label, type, ph]}
              <label class="block">
                <span class="text-[8px] font-black uppercase tag-label">{label}</span>
                <input
                  type={type}
                  value={storageEditing[k] ?? ''}
                  placeholder={ph}
                  oninput={(e) => storageEditing[k] = e.currentTarget.value}
                  class="w-full mt-1 px-2 py-1.5 text-[10px] font-mono focus:outline-none"
                  style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);"
                />
              </label>
            {/each}

            <label class="block">
              <span class="text-[8px] font-black uppercase tag-label">ADDRESSING_STYLE</span>
              <select
                value={storageEditing.addressing_style ?? 'auto'}
                onchange={(e) => storageEditing.addressing_style = e.currentTarget.value}
                class="w-full mt-1 px-2 py-1.5 text-[10px] font-mono focus:outline-none cursor-pointer"
                style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);"
              >
                <option value="auto">AUTO</option>
                <option value="path">PATH</option>
                <option value="virtual">VIRTUAL</option>
              </select>
            </label>
          </div>

          <div class="mt-4 flex flex-wrap gap-2">
            {#each [['use_ssl','USE_SSL'],['use_for_uploads','USE_FOR_UPLOADS'],['use_for_exports','USE_FOR_EXPORTS'],['use_for_cache','USE_FOR_CACHE'],['use_for_archive','USE_FOR_ARCHIVE'],['active','ACTIVE']] as [k, label]}
              <button
                class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                style="border: 2px solid var(--on-surface); background: {storageEditing[k] ? 'var(--primary)' : 'white'}; color: {storageEditing[k] ? 'white' : 'var(--outline)'};"
                onclick={() => storageEditing[k] = !storageEditing[k]}
              >[{storageEditing[k] ? 'X' : ' '}] {label}</button>
            {/each}
          </div>

          <div class="flex justify-end gap-2 mt-4">
            <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                    style="border: 2px solid var(--on-surface); background: transparent; color: var(--on-surface);"
                    onclick={() => storageEditing = null}>CANCEL</button>
            <button class="text-[8px] font-black uppercase px-3 py-1.5 cursor-pointer"
                    style="background: #10b981; color: white; border: 2px solid var(--on-surface);"
                    onclick={saveStorage}>{storageEditing.id ? 'UPDATE' : 'CREATE'}</button>
          </div>
        </div>
      </div>
    {/if}

  {:else if activeTab === 'auto_approve'}
    <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
      <div class="dark-bar text-xs">AUTO_APPROVE_SETTINGS</div>
      <div class="bg-white p-4 space-y-4">
        <div class="text-[10px] font-mono opacity-70">
          When ENABLED, an hourly background task auto-approves any pending_review job whose
          accuracy_percent is greater than or equal to (threshold × 100). Auto-approved jobs are
          marked <code>reviewed_by = SYSTEM_AUTO</code>.
        </div>

        <div class="flex items-center gap-3">
          <label class="text-xs font-bold uppercase w-32">ENABLED</label>
          <button class="text-[10px] font-bold uppercase px-3 py-1.5 cursor-pointer"
            style="border: 2px solid var(--on-surface);
                   background: {autoApprove.enabled ? '#10b981' : 'white'};
                   color: {autoApprove.enabled ? 'white' : 'var(--on-surface)'};"
            onclick={() => autoApprove.enabled = !autoApprove.enabled}>
            {autoApprove.enabled ? '☑ ON' : '☐ OFF'}
          </button>
        </div>

        <div class="flex items-center gap-3">
          <label class="text-xs font-bold uppercase w-32">THRESHOLD</label>
          <input type="number" min="0" max="1" step="0.01"
            bind:value={autoApprove.threshold}
            class="text-xs font-mono px-2 py-1.5 w-32"
            style="border: 2px solid var(--on-surface); background: white;" />
          <span class="text-[10px] font-mono opacity-60">
            (0.0–1.0; ≥ {((autoApprove.threshold || 0) * 100).toFixed(0)}% accuracy auto-approves)
          </span>
        </div>

        <div class="grid grid-cols-2 gap-3 mt-2 text-[10px] font-mono">
          <div class="border-2 p-2" style="border-color: var(--on-surface);">
            <div class="opacity-60">SCHEDULE</div>
            <div class="font-bold">hourly</div>
          </div>
          <div class="border-2 p-2" style="border-color: var(--on-surface);">
            <div class="opacity-60">LAST_RUN</div>
            <div class="font-bold">
              {autoApprove.last_run || '—'}
              {#if autoApprove.last_count !== undefined}
                <span class="opacity-70">— approved {autoApprove.last_count}</span>
              {/if}
            </div>
          </div>
        </div>

        {#if autoApproveMsg}
          <div class="p-2 text-xs font-bold uppercase text-white"
            style="background: {autoApproveMsg.type === 'ok' ? 'var(--primary)' : 'var(--error)'};">
            {autoApproveMsg.msg}
          </div>
        {/if}

        <div class="pt-2">
          <Button variant="primary" size="md"
            disabled={autoApproveSaving}
            onclick={saveAutoApprove}>
            {autoApproveSaving ? 'SAVING...' : 'SAVE_SETTINGS'}
          </Button>
        </div>
      </div>
    </div>

  {/if}
{/if}

<!-- Activity Log: Event detail drawer -->
{#if drawerEvent}
  <div class="fixed inset-0 z-50 flex justify-end" style="background: rgba(0,0,0,0.5);"
       onclick={() => drawerEvent = null}>
    <div class="border-l-2 max-w-lg w-full overflow-y-auto p-6"
         style="border-color: var(--on-surface); background: var(--surface);"
         onclick={(e) => e.stopPropagation()}>
      <div class="-mx-6 -mt-6 px-6 py-3 mb-4"
           style="background: var(--on-surface); color: var(--surface);">
        <span class="text-xs font-black uppercase">EVENT_DETAIL</span>
      </div>
      <div class="space-y-2 text-[10px] font-mono">
        {#each Object.entries(drawerEvent.event || {}) as [k, v]}
          <div class="grid grid-cols-3 gap-2">
            <div class="font-black opacity-60">{k.toUpperCase()}</div>
            <div class="col-span-2 break-all">{v ?? '—'}</div>
          </div>
        {/each}
      </div>
      {#if drawerEvent.related?.length}
        <div class="mt-6 pt-3" style="border-top: 2px solid var(--on-surface);">
          <div class="text-[10px] font-black uppercase mb-2">RECENT (same user)</div>
          {#each drawerEvent.related as r}
            <div class="text-[10px] font-mono py-1" style="border-bottom: 1px solid rgba(0,0,0,0.1);">
              {r.timestamp} · {r.action} · {r.status || ''}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}
