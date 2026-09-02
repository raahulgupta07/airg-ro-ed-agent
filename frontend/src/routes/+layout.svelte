<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';
  import AppSidebar from '$lib/components/AppSidebar.svelte';
  import Footer from '$lib/components/Footer.svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';

  let { children } = $props();
  let totalJobs = $state(0);
  let totalCost = $state(0);
  // Owned by AppSidebar (it persists the preference); read here so the main
  // column reserves the right amount of space.
  let navCollapsed = $state(false);

  // Fetch stats periodically
  async function fetchStats() {
    if (!auth.isAuthenticated) return;
    // Skip polling while the tab is backgrounded — no point spending a request
    // every 30s on a screen nobody is looking at.
    if (typeof document !== 'undefined' && document.hidden) return;
    try {
      const stats = await api.stats();
      totalJobs = stats.total_jobs || stats.completed_jobs || 0;
      totalCost = stats.total_cost || 0;
    } catch {}
  }

  $effect(() => {
    if (auth.isAuthenticated) fetchStats();
  });

  // Refresh stats every 30s
  onMount(() => {
    // Initialize auth (handles OIDC callback, session restore, etc.)
    auth.init();

    const interval = setInterval(fetchStats, 30000);
    return () => clearInterval(interval);
  });

  // Handle tab visibility — refresh Keycloak token if needed
  $effect(() => {
    if (typeof document === 'undefined') return;
    const handler = () => {
      if (!document.hidden && auth.isKeycloak && auth.isAuthenticated) {
        auth.ensureValidToken();
      }
    };
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  });

  const isLoginPage = $derived(page.url.pathname === '/login');
  const isChangePasswordPage = $derived(page.url.pathname === '/change-password');

  // Redirect to login if not authenticated and not on login page (skip during init)
  $effect(() => {
    if (!auth.initializing && !auth.isAuthenticated && !isLoginPage) {
      goto('/login');
    }
  });

  // Force-change password guard: any authenticated user with must_change_password=true
  // gets redirected to /change-password from any other route.
  $effect(() => {
    if (auth.initializing) return;
    if (!auth.isAuthenticated) return;
    if (isLoginPage || isChangePasswordPage) return;
    if (auth.user?.must_change_password) {
      goto('/change-password');
    }
  });
</script>

{#if auth.initializing}
  <div class="min-h-screen flex items-center justify-center" style="background: var(--surface);">
    <div class="text-center">
      <div class="spinner mx-auto mb-4"></div>
      <div class="text-sm font-bold uppercase tracking-widest" style="color: var(--outline);">INITIALIZING...</div>
    </div>
  </div>
{:else if isLoginPage || isChangePasswordPage || !auth.isAuthenticated}
  {@render children()}
{:else}
  <AppSidebar
    username={auth.user?.username ?? ''}
    role={auth.user?.role ?? ''}
    onlogout={() => auth.logout()}
    bind:collapsed={navCollapsed}
  />
  <div class="min-h-screen flex flex-col app-shell" style="--sb-w: {navCollapsed ? '56px' : '224px'};">
    <main class="flex-1 pt-5 pb-16 px-4 md:px-8 w-full max-w-[1760px] mx-auto">
      {@render children()}
    </main>
    <Footer totalJobs={totalJobs} totalCost={totalCost} />
  </div>
{/if}

<style>
  /* Docked sidebar: reserve its width. It transitions at the same 180ms. */
  .app-shell {
    margin-inline-start: var(--sb-w);
    transition: margin-inline-start 180ms ease;
  }
  /* Below 640px the sidebar is an off-canvas drawer and the 48px bar takes over. */
  @media (max-width: 639px) {
    .app-shell { margin-inline-start: 0; padding-block-start: 48px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .app-shell { transition-duration: 0.01ms; }
  }
</style>
