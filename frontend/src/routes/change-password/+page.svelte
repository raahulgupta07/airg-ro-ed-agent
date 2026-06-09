<script lang="ts">
  import { goto } from '$app/navigation';
  import { auth } from '$lib/stores/auth.svelte';

  let current = $state('');
  let newPw = $state('');
  let confirm = $state('');
  let busy = $state(false);
  let error = $state('');

  async function submit(e: Event) {
    e.preventDefault();
    error = '';
    if (newPw.length < 8) { error = 'Min 8 characters'; return; }
    if (newPw !== confirm) { error = 'Passwords do not match'; return; }
    if (newPw === current) { error = 'New password must differ from current'; return; }
    busy = true;
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ current_password: current, new_password: newPw }),
      });
      const t = await r.text();
      const j = JSON.parse(t);
      if (!r.ok) throw new Error(j.detail || 'Change failed');
      // Update local user state — clear must_change_password
      if (auth.user) {
        auth.user = { ...auth.user, must_change_password: false };
      }
      goto('/agent');
    } catch (e: any) {
      error = e?.message || 'Failed';
      busy = false;
    }
  }
</script>

<div class="min-h-screen flex items-center justify-center p-6" style="background: var(--surface);">
  <div class="cl-panel w-full max-w-md">
    <div class="cl-hd"><span class="dot">◉</span>Change Password</div>
    <form onsubmit={submit} class="cl-bd space-y-4">
      <p class="text-sm" style="color: var(--on-surface-muted); line-height: 1.5;">
        For security, you must set a new password before continuing.
      </p>

      <div>
        <label class="cl-lbl" for="cp-current">Current Password</label>
        <input id="cp-current" type="password" bind:value={current} required autofocus
               class="cl-inp" />
      </div>

      <div>
        <label class="cl-lbl" for="cp-new">New Password (≥8 chars)</label>
        <input id="cp-new" type="password" bind:value={newPw} required minlength="8"
               class="cl-inp" />
      </div>

      <div>
        <label class="cl-lbl" for="cp-confirm">Confirm New Password</label>
        <input id="cp-confirm" type="password" bind:value={confirm} required
               class="cl-inp" />
      </div>

      {#if error}
        <div class="text-sm" style="color: var(--error);">
          {error}
        </div>
      {/if}

      <button type="submit" disabled={busy}
              class="cl-btn primary w-full"
              class:opacity-50={busy}>
        {busy ? 'Saving…' : 'Change Password'}
      </button>
    </form>
  </div>
</div>
