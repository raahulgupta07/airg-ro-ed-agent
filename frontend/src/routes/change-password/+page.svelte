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
  <div class="w-full max-w-md border-2 stamp-shadow" style="border-color: var(--on-surface); background: var(--surface);">
    <div class="dark-bar text-xs">⚠ PASSWORD_CHANGE_REQUIRED</div>
    <form onsubmit={submit} class="bg-white p-6 space-y-3">
      <div class="text-[10px] font-bold uppercase" style="color: var(--outline);">
        For security, you must set a new password before continuing.
      </div>

      <div>
        <div class="text-[8px] font-medium uppercase mb-1" style="color: var(--outline);">CURRENT PASSWORD</div>
        <input type="password" bind:value={current} required autofocus
               class="w-full text-xs font-mono px-3 py-2 focus:outline-none"
               style="border: 1px solid var(--on-surface); background: white;" />
      </div>

      <div>
        <div class="text-[8px] font-medium uppercase mb-1" style="color: var(--outline);">NEW PASSWORD (≥8 chars)</div>
        <input type="password" bind:value={newPw} required minlength="8"
               class="w-full text-xs font-mono px-3 py-2 focus:outline-none"
               style="border: 1px solid var(--on-surface); background: white;" />
      </div>

      <div>
        <div class="text-[8px] font-medium uppercase mb-1" style="color: var(--outline);">CONFIRM NEW PASSWORD</div>
        <input type="password" bind:value={confirm} required
               class="w-full text-xs font-mono px-3 py-2 focus:outline-none"
               style="border: 1px solid var(--on-surface); background: white;" />
      </div>

      {#if error}
        <div class="p-2 text-[10px] font-bold uppercase" style="background: var(--error); color: white;">
          {error}
        </div>
      {/if}

      <button type="submit" disabled={busy}
              class="w-full px-3 py-2.5 text-[11px] font-medium uppercase border-2 cursor-pointer"
              style="border-color: var(--on-surface); background: {busy ? '#9ca3af' : 'var(--primary-container)'}; box-shadow: var(--shadow-sm);">
        {busy ? '… SAVING' : 'CHANGE_PASSWORD'}
      </button>
    </form>
  </div>
</div>
