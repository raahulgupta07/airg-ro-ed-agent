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

<div class="min-h-screen flex flex-col" style="background: var(--surface);">

  <!-- Logo top-left -->
  <div class="px-8 pt-6">
    <img src="/cityagent-logo.png" alt="CityAgent · Release Order" style="width: 190px; height: auto;" />
  </div>

  <!-- Two-column hero -->
  <div class="flex-1 flex items-center justify-center px-6 py-8">
    <div class="w-full max-w-6xl grid lg:grid-cols-2 gap-10 lg:gap-16 items-center">

      <!-- LEFT: greeting + tagline + stats + form -->
      <div class="max-w-md w-full mx-auto lg:mx-0">
        <h1 class="font-serif" style="font-size: 38px; line-height: 1.15; color: var(--on-surface); letter-spacing: -0.02em; font-weight: 500;">
          {greeting},<br/>sign in to CityAgent
        </h1>
        <p class="mt-4 text-base" style="color: var(--on-surface-muted); line-height: 1.55;">
          Myanmar customs intelligence — classify, extract, reconcile and approve
          declarations at the desk.
        </p>
        <div class="mt-4 flex items-center gap-2 text-sm font-mono" style="color: var(--on-surface-muted);">
          <span class="w-2 h-2 rounded-full" style="background: var(--success);"></span>
          Atlas Gen 2 · Maestro router · queue-driven
        </div>

        <!-- Form card -->
        <div class="cl-panel mt-7">
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
      </div>

      <!-- RIGHT: product preview -->
      <div class="hidden lg:block">
        <div class="cl-panel" style="background: var(--surface-container-low);">
          <div class="p-5" style="background-image: radial-gradient(var(--line) 1px, transparent 1px); background-size: 22px 22px;">

            <!-- sample query bubble -->
            <div class="inline-block px-4 py-2 mb-6 text-sm font-medium text-white"
                 style="background: var(--primary); border-radius: 12px 12px 12px 2px;">
              “Extract this customs declaration”
            </div>

            <!-- action tiles -->
            <div class="grid grid-cols-4 gap-3 mb-6">
              {#each tiles as t}
                <div class="flex flex-col items-center justify-center gap-1.5 py-4 px-1 text-center transition-colors"
                     style="background: var(--surface-container-lowest); border: 1px solid {t.accent ? 'var(--primary)' : 'var(--line)'}; border-radius: var(--radius-md); {t.accent ? 'box-shadow: 0 0 0 3px var(--primary-tint);' : ''}">
                  <span class="material-symbols-outlined" style="font-size: 22px; color: {t.accent ? 'var(--primary)' : 'var(--on-surface-muted)'};">{t.icon}</span>
                  <span class="text-xs font-medium" style="color: {t.accent ? 'var(--primary-hover)' : 'var(--on-surface)'};">{t.label}</span>
                </div>
              {/each}
            </div>

            <!-- input bar -->
            <div class="flex items-center gap-2">
              <div class="flex-1 flex items-center gap-2 px-3 py-2.5"
                   style="background: var(--surface-container-lowest); border: 1px solid var(--line); border-radius: var(--radius-md);">
                <span class="material-symbols-outlined" style="font-size: 18px; color: var(--on-surface-subtle);">science</span>
                <span class="text-sm" style="color: var(--on-surface-muted);">CityAgent · Maestro router</span>
              </div>
              <div class="px-4 py-2.5 text-sm font-medium" style="background: var(--primary-soft); color: var(--primary-hover); border-radius: var(--radius-md);">
                Let's go →
              </div>
            </div>
          </div>
        </div>
        <p class="mt-4 text-center text-xs" style="color: var(--on-surface-subtle);">
          © 2026 City Holdings Myanmar · CityAgent · Release Order
        </p>
      </div>

    </div>
  </div>
</div>
