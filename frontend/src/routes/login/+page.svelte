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

  <!-- Top brand bar -->
  <header class="px-10 py-6 shrink-0">
    <a href="/login" class="inline-flex items-center gap-2.5 no-underline">
      <span class="w-9 h-9 flex items-center justify-center text-white font-medium text-sm"
            style="background: var(--primary); border-radius: 10px;">RO</span>
      <span class="font-serif text-xl" style="color: var(--on-surface); letter-spacing: -0.01em;">RO‑ED</span>
      <span class="text-xs" style="color: var(--on-surface-muted);">Command Center</span>
    </a>
  </header>

  <!-- Main split -->
  <main class="flex-1 px-10 pb-10">
    <div class="max-w-7xl mx-auto grid lg:grid-cols-2 gap-12 items-center">

      <!-- LEFT: greeting + form -->
      <section class="max-w-lg w-full lg:pt-6">
        <h1 class="font-serif text-5xl leading-[1.1]" style="color: var(--on-surface); letter-spacing: -0.02em; font-weight: 500;">
          {greeting},<br>sign in to RO‑ED
        </h1>
        <p class="mt-5 text-base" style="color: var(--on-surface-muted); line-height: 1.6;">
          AI-powered Myanmar customs declaration extraction. Typed and handwritten, on one queue.
        </p>

        <!-- Stats -->
        <div class="mt-5 flex items-center gap-2 text-sm" style="color: var(--on-surface-muted);">
          <span class="inline-block w-2 h-2 rounded-full" style="background: var(--success);"></span>
          <span>10 concurrent · V11 Maestro queue · 99.9% uptime</span>
        </div>

        <!-- Form card -->
        <div class="mt-8 p-6 ink-border" style="background: var(--surface-container-lowest); box-shadow: var(--shadow-sm);">

          {#if error}
            <div class="mb-4 px-3 py-2 text-sm" style="background: var(--error-container); color: var(--on-error-container); border-radius: var(--radius-md);">
              {error}
            </div>
          {/if}

          <!-- SSO / LDAP top buttons -->
          {#if auth.isKeycloak}
            <button
              type="button"
              onclick={handleKeycloakLogin}
              disabled={redirecting}
              class="w-full flex items-center justify-center gap-2 py-2.5 text-sm font-medium cursor-pointer transition-colors"
              style="background: var(--surface-container-lowest); color: var(--on-surface); border: 1px solid var(--outline-variant); border-radius: var(--radius-md);"
              onmouseenter={(e) => e.currentTarget.style.background = 'var(--surface-container-low)'}
              onmouseleave={(e) => e.currentTarget.style.background = 'var(--surface-container-lowest)'}
            >
              <span class="w-1.5 h-1.5 rounded-full" style="background: var(--on-surface-muted);"></span>
              {redirecting ? 'Redirecting…' : 'Continue with SSO (SAML / OIDC)'}
            </button>
          {/if}

          <button
            type="button"
            disabled
            class="mt-2 w-full py-2.5 text-sm font-medium cursor-not-allowed transition-colors"
            style="background: var(--surface-container-lowest); color: var(--on-surface-muted); border: 1px solid var(--outline-variant); border-radius: var(--radius-md); opacity: 0.65;"
            title="Use credentials below — LDAP auto-cascade on backend"
          >
            Continue with LDAP / Active Directory
          </button>

          <!-- OR divider -->
          <div class="flex items-center gap-3 my-5">
            <div class="flex-1 h-px" style="background: var(--outline-variant);"></div>
            <span class="text-xs font-medium" style="color: var(--on-surface-subtle); letter-spacing: 0.08em;">OR</span>
            <div class="flex-1 h-px" style="background: var(--outline-variant);"></div>
          </div>

          <!-- Username -->
          <input
            type="text"
            placeholder="Username"
            bind:value={username}
            class="w-full text-sm focus:outline-none transition-colors"
            style="padding: 12px 14px; background: var(--surface-container-lowest); border: 1px solid var(--outline-variant); color: var(--on-surface); border-radius: var(--radius-md);"
            onfocus={(e) => { e.currentTarget.style.borderColor = 'var(--primary)'; e.currentTarget.style.boxShadow = '0 0 0 3px var(--primary-container)'; }}
            onblur={(e) => { e.currentTarget.style.borderColor = 'var(--outline-variant)'; e.currentTarget.style.boxShadow = 'none'; }}
          />

          <!-- Password -->
          <div class="relative mt-2.5">
            <input
              type={showPassword ? 'text' : 'password'}
              placeholder="Password"
              bind:value={password}
              class="w-full text-sm focus:outline-none transition-colors pr-16"
              style="padding: 12px 14px; background: var(--surface-container-lowest); border: 1px solid var(--outline-variant); color: var(--on-surface); border-radius: var(--radius-md);"
              onkeydown={(e) => { if (e.key === 'Enter' && username && password) handleLocalLogin(); }}
              onfocus={(e) => { e.currentTarget.style.borderColor = 'var(--primary)'; e.currentTarget.style.boxShadow = '0 0 0 3px var(--primary-container)'; }}
              onblur={(e) => { e.currentTarget.style.borderColor = 'var(--outline-variant)'; e.currentTarget.style.boxShadow = 'none'; }}
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

          <!-- Remember -->
          <label class="flex items-center gap-2 mt-4 cursor-pointer text-sm" style="color: var(--on-surface);">
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
            class="mt-4 w-full press-effect py-3 text-sm font-medium cursor-pointer transition-opacity"
            class:opacity-50={loading || !username || !password}
            style="background: var(--secondary); color: #fff; border-radius: var(--radius-md);"
          >
            {loading ? 'Signing in…' : 'Continue with email'}
          </button>
        </div>
      </section>

      <!-- RIGHT: pipeline preview -->
      <aside class="hidden lg:block">
        <div class="relative p-6 ink-border"
             style="background-color: var(--surface-container-lowest);
                    background-image:
                      linear-gradient(rgba(31,30,29,0.05) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(31,30,29,0.05) 1px, transparent 1px);
                    background-size: 32px 32px;
                    background-position: -1px -1px;
                    box-shadow: var(--shadow-md);">

          <!-- Tooltip pill -->
          <div class="mb-5 inline-flex items-center px-3 py-1.5 text-xs font-medium text-white"
               style="background: var(--primary); border-radius: var(--radius-md); box-shadow: var(--shadow-sm);">
            "Route this declaration"
          </div>

          <!-- Tiles grid -->
          <div class="grid grid-cols-4 gap-3">
            {#each tiles as t}
              <div class="aspect-square p-3 flex flex-col justify-between transition-all cursor-default"
                   style="background: var(--surface-container-lowest);
                          border: {t.accent ? '1.5px solid var(--primary)' : '1px solid var(--outline-variant)'};
                          border-radius: var(--radius-md);
                          box-shadow: {t.accent ? '0 0 0 3px var(--primary-container)' : 'var(--shadow-xs)'};">
                <span class="material-symbols-outlined text-lg"
                      style="color: {t.accent ? 'var(--primary)' : 'var(--on-surface-muted)'};">{t.icon}</span>
                <span class="text-xs font-medium"
                      style="color: {t.accent ? 'var(--primary)' : 'var(--on-surface)'};">{t.label}</span>
              </div>
            {/each}
          </div>

          <!-- Bottom prompt strip -->
          <div class="mt-6 flex items-center gap-2 p-2"
               style="background: var(--surface-container-lowest); border: 1px solid var(--outline-variant); border-radius: var(--radius-md);">
            <span class="material-symbols-outlined text-base ml-1" style="color: var(--on-surface-muted);">science</span>
            <span class="flex-1 text-sm" style="color: var(--on-surface);">Customs declaration · 5 pages</span>
            <button class="w-7 h-7 flex items-center justify-center text-base cursor-default"
                    style="background: var(--surface-container); color: var(--on-surface-muted); border-radius: var(--radius-sm);">+</button>
            <button class="px-3 py-1.5 text-xs font-medium cursor-default"
                    style="background: var(--primary-container); color: var(--primary-hover); border-radius: var(--radius-sm);">
              Let's go →
            </button>
          </div>
        </div>
      </aside>

    </div>
  </main>

  <!-- Footer -->
  <footer class="px-10 py-6 text-center text-xs" style="color: var(--on-surface-subtle);">
    © 2026 City Holdings Myanmar · RO‑ED Command Center
  </footer>
</div>
