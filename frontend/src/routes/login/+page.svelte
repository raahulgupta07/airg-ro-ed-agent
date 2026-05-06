<script lang="ts">
  import { goto } from '$app/navigation';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';
  import FormInput from '$lib/components/FormInput.svelte';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let loading = $state(false);
  let showPassword = $state(false);
  let redirecting = $state(false);

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
      error = e.message || 'AUTHENTICATION_FAILED';
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
      error = 'REDIRECT_FAILED';
    }
  }
</script>

<div class="h-screen flex flex-col overflow-hidden" style="background: var(--surface);">
  <!-- Top bar (matches main app header) -->
  <div class="flex items-center justify-between px-6 py-2 shrink-0" style="border-bottom: 3px solid var(--on-surface);">
    <div class="text-lg font-black tracking-tighter uppercase px-3 py-1"
         style="background: var(--on-surface); color: var(--primary-container); letter-spacing: -0.5px;">
      RO-ED COMMAND CENTER
    </div>
    <div class="flex items-center gap-3">
      <span class="text-[9px] font-bold uppercase" style="color: var(--outline);">CITY AI TEAM</span>
      <span class="text-[9px] font-bold uppercase px-2 py-0.5" style="background: var(--secondary); color: white;">v2.0</span>
    </div>
  </div>

  <!-- Main content -->
  <div class="flex-1 flex items-center px-6 md:px-16 min-h-0">
    <div class="w-full max-w-7xl mx-auto flex items-center justify-between gap-12">

      <!-- Left: Form -->
      <div class="w-full max-w-md">
        <!-- Auth badge + Title -->
        <div class="mb-1">
          <span class="tag-label" style="font-size: 9px;">AUTHENTICATION_REQUIRED</span>
        </div>
        <div class="text-3xl font-black uppercase tracking-tighter" style="color: var(--on-surface); border-bottom: 3px solid var(--on-surface); padding-bottom: 6px;">
          ACCESS_PORTAL
        </div>
        <div class="text-xs font-medium uppercase mt-2 mb-4" style="color: var(--outline);">
          CITY HOLDINGS MYANMAR — PG : RO AGENT
        </div>

        <!-- Form container -->
        <div class="p-5 stamp-shadow"
             style="background: var(--surface-container); border-top: 2px solid var(--on-surface); border-left: 2px solid var(--on-surface); border-bottom: 3px solid var(--on-surface); border-right: 3px solid var(--on-surface);">

          {#if error}
            <div class="mb-4 p-2 font-bold text-xs uppercase text-white border-2"
                 style="background: var(--error); border-color: var(--on-surface);">
              {error}
            </div>
          {/if}

          {#if auth.isKeycloak}
            <!-- KEYCLOAK SSO -->
            <div class="flex items-center justify-between mb-3">
              <div>
                <div class="text-[9px] font-black uppercase tracking-widest" style="color: var(--outline);">SSO_PROVIDER</div>
                <div class="text-xs font-bold uppercase" style="color: var(--secondary);">KEYCLOAK IDENTITY SERVER</div>
              </div>
            </div>

            <button
              onclick={handleKeycloakLogin}
              disabled={redirecting}
              class="w-full press-effect font-black uppercase tracking-wider text-xs cursor-pointer py-3"
              class:opacity-50={redirecting}
              style="background: var(--on-surface); color: var(--surface); border: 2px solid var(--on-surface); box-shadow: 3px 3px 0px 0px var(--on-surface);"
            >
              {redirecting ? 'REDIRECTING...' : 'SIGN IN WITH KEYCLOAK'}
            </button>

            <!-- Divider -->
            <div class="flex items-center gap-3 my-4">
              <div class="flex-1 h-[2px]" style="background: var(--on-surface); opacity: 0.15;"></div>
              <span class="text-[9px] font-black uppercase tracking-widest" style="color: var(--outline);">OR</span>
              <div class="flex-1 h-[2px]" style="background: var(--on-surface); opacity: 0.15;"></div>
            </div>
          {/if}

          <!-- Local login -->
          <div class="space-y-3">
            {#if auth.isKeycloak}
              <div class="text-[9px] font-black uppercase tracking-widest mb-1" style="color: var(--outline);">LOCAL_CREDENTIALS</div>
            {/if}

            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">OPERATOR_ID</div>
              <input
                type="text"
                placeholder="Enter credentials"
                bind:value={username}
                class="w-full font-bold text-sm focus:outline-none"
                style="padding: 8px 12px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface); color: var(--on-surface);"
              />
            </div>

            <div>
              <div class="tag-label mb-0.5" style="font-size: 9px;">ACCESS_KEY</div>
              <div class="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  placeholder="Enter password"
                  bind:value={password}
                  class="w-full font-bold text-sm focus:outline-none pr-14"
                  style="padding: 8px 12px; font-family: 'Space Grotesk', sans-serif; background: white; border: 2px solid var(--on-surface); color: var(--on-surface);"
                />
                <button
                  type="button"
                  class="absolute right-3 top-1/2 -translate-y-1/2 text-[9px] font-black uppercase cursor-pointer"
                  style="color: var(--outline);"
                  onclick={() => showPassword = !showPassword}
                >
                  {showPassword ? 'HIDE' : 'SHOW'}
                </button>
              </div>
            </div>
          </div>

          <div class="mt-4">
            <button
              onclick={handleLocalLogin}
              disabled={loading || !username || !password}
              class="w-full press-effect font-black uppercase tracking-wider text-xs cursor-pointer py-3"
              class:opacity-50={loading}
              style="background: var(--primary-container); color: var(--on-surface); border: 2px solid var(--on-surface); box-shadow: 3px 3px 0px 0px var(--on-surface);"
            >
              {loading ? 'AUTHENTICATING...' : 'INITIATE_AUTHENTICATION'}
            </button>
          </div>
        </div>

        <!-- Status bar -->
        <div class="mt-3 flex items-center gap-3 opacity-40">
          <div class="flex items-center gap-1.5">
            <span class="inline-block w-1.5 h-1.5" style="background: var(--primary);"></span>
            <span class="text-[9px] font-bold uppercase tracking-widest" style="color: var(--on-surface);">NODE_ACTIVE</span>
          </div>
          <span style="color: var(--outline);">|</span>
          <span class="text-[9px] font-bold uppercase tracking-widest" style="color: var(--on-surface);">
            {auth.isKeycloak ? 'OIDC_PKCE' : 'AES-256'}
          </span>
          <span style="color: var(--outline);">|</span>
          <span class="text-[9px] font-bold uppercase tracking-widest" style="color: var(--on-surface);">V1.0</span>
        </div>
      </div>

      <!-- Right: Big text decoration -->
      <div class="hidden lg:block flex-1 text-right">
        <div class="font-black uppercase leading-[0.85] tracking-tight"
             style="font-size: 7rem; color: #1a1a1a; opacity: 0.12;">
          CITY<br>HOLDINGS<br>MYANMAR
        </div>
        <div class="mt-4 flex items-center justify-end gap-3">
          <span class="text-xl font-black uppercase tracking-tight" style="color: #1a1a1a; opacity: 0.35;">RO AGENT</span>
          <span class="text-xl font-black uppercase" style="color: #1a1a1a; opacity: 0.35;">V1.0</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Footer -->
  <div class="flex items-center justify-between px-6 py-2 shrink-0" style="border-top: 1px solid rgba(56,56,50,0.15);">
    <span class="text-[9px] font-mono uppercase" style="color: var(--outline); opacity: 0.4;">&copy; 2026 CITY HOLDINGS MYANMAR</span>
    <span class="text-[9px] font-mono uppercase" style="color: var(--outline); opacity: 0.4;">SECURE_TERMINAL</span>
  </div>
</div>
