<script lang="ts">
  import { goto } from '$app/navigation';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let loading = $state(false);
  let showPassword = $state(false);
  let redirecting = $state(false);
  let remember = $state(true);

  $effect(() => {
    if (auth.isAuthenticated) {
      if (auth.user?.must_change_password) goto('/change-password');
      else goto('/agent');
    }
  });

  async function handleLocalLogin() {
    error = '';
    loading = true;
    try {
      const res = await api.login(username, password);
      await auth.loginLocal(res.access_token, res.user);
      if (auth.user?.must_change_password) {
        goto('/change-password');
      } else {
        goto('/agent');
      }
    } catch (e: any) {
      error = e.message || 'Authentication failed';
    } finally {
      loading = false;
    }
  }

  async function handleKeycloakLogin() {
    redirecting = true;
    try {
      await auth.initiateLogin();
    } catch {
      redirecting = false;
      error = 'Redirect failed';
    }
  }

  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  })();

  const tiles = [
    { icon: 'upload_file',    label: 'Upload PDF',     accent: false },
    { icon: 'auto_awesome',   label: 'Classify pages', accent: true  },
    { icon: 'description',    label: 'Typed extract',  accent: false },
    { icon: 'edit_note',      label: 'Handwritten',    accent: false },
    { icon: 'merge',          label: 'Merge results',  accent: false },
    { icon: 'checklist',      label: 'Review queue',   accent: false },
    { icon: 'history_edu',    label: 'Audit edits',    accent: false },
    { icon: 'file_download',  label: 'Export',         accent: false },
  ];
</script>

<div class="min-h-screen flex flex-col items-center justify-center p-6" style="background: var(--surface);">

  <div class="w-full max-w-md">

    <!-- Brand -->
    <div class="flex flex-col items-center text-center mb-6">
      <span class="w-12 h-12 flex items-center justify-center text-white font-medium text-base mb-4"
            style="background: var(--primary); border-radius: var(--radius-md);">RO</span>
      <h1 class="font-serif text-3xl" style="color: var(--on-surface); letter-spacing: -0.01em; font-weight: 500;">
        {greeting}
      </h1>
      <p class="mt-2 text-sm" style="color: var(--on-surface-muted); line-height: 1.5;">
        Sign in to RO‑ED Command Center
      </p>
    </div>

    <!-- Form card -->
    <div class="cl-panel">
      <div class="cl-hd"><span class="dot">◉</span>Sign In</div>
      <div class="cl-bd space-y-4">

        {#if error}
          <div class="text-sm" style="color: var(--error);">
            {error}
          </div>
        {/if}

        <!-- SSO / LDAP top buttons -->
        {#if auth.isKeycloak}
          <button
            type="button"
            onclick={handleKeycloakLogin}
            disabled={redirecting}
            class="cl-btn w-full flex items-center justify-center gap-2"
          >
            <span class="w-1.5 h-1.5 rounded-full" style="background: var(--on-surface-muted);"></span>
            {redirecting ? 'Redirecting…' : 'Continue with SSO (SAML / OIDC)'}
          </button>
        {/if}

        <button
          type="button"
          disabled
          class="cl-btn w-full opacity-60 cursor-not-allowed"
          title="Use credentials below — LDAP auto-cascade on backend"
        >
          Continue with LDAP / Active Directory
        </button>

        <!-- OR divider -->
        <div class="flex items-center gap-3">
          <div class="flex-1 h-px" style="background: var(--line);"></div>
          <span class="text-xs font-medium" style="color: var(--on-surface-subtle); letter-spacing: 0.08em;">OR</span>
          <div class="flex-1 h-px" style="background: var(--line);"></div>
        </div>

        <!-- Username -->
        <div>
          <label class="cl-lbl" for="login-username">Username</label>
          <input
            id="login-username"
            type="text"
            placeholder="Username"
            bind:value={username}
            class="cl-inp"
          />
        </div>

        <!-- Password -->
        <div>
          <label class="cl-lbl" for="login-password">Password</label>
          <div class="relative">
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              placeholder="Password"
              bind:value={password}
              class="cl-inp pr-16"
              onkeydown={(e) => { if (e.key === 'Enter' && username && password) handleLocalLogin(); }}
            />
            <button
              type="button"
              class="absolute right-3 top-1/2 -translate-y-1/2 text-xs font-medium cursor-pointer"
              style="color: var(--on-surface-muted);"
              onclick={() => showPassword = !showPassword}
            >
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </div>
        </div>

        <!-- Remember -->
        <label class="flex items-center gap-2 cursor-pointer text-sm" style="color: var(--on-surface);">
          <input type="checkbox" bind:checked={remember}
                 class="w-4 h-4 cursor-pointer"
                 style="accent-color: var(--primary);" />
          Remember me on this device
        </label>

        <!-- CTA -->
        <button
          type="button"
          onclick={handleLocalLogin}
          disabled={loading || !username || !password}
          class="cl-btn primary w-full"
          class:opacity-50={loading || !username || !password}
        >
          {loading ? 'Signing in…' : 'Continue with email'}
        </button>
      </div>
    </div>

    <!-- Footer -->
    <p class="mt-6 text-center text-xs" style="color: var(--on-surface-subtle);">
      © 2026 City Holdings Myanmar · RO‑ED Command Center
    </p>
  </div>
</div>
