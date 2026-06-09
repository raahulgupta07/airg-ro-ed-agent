<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import Footer from '$lib/components/Footer.svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';

  let { children } = $props();
  let totalJobs = $state(0);
  let totalCost = $state(0);

  // Fetch stats periodically
  async function fetchStats() {
    if (!auth.isAuthenticated) return;
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
  <Sidebar
    username={auth.user?.username ?? ''}
    role={auth.user?.role ?? ''}
    onlogout={() => auth.logout()}
  />
  <div class="ml-[236px] min-h-screen flex flex-col">
    <main class="flex-1 pt-6 pb-16 px-8 w-full max-w-[1760px] mx-auto">
      {@render children()}
    </main>
    <Footer totalJobs={totalJobs} totalCost={totalCost} />
  </div>
{/if}
