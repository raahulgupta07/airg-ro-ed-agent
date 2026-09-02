<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';

  let {
    username = '',
    role = '',
    onlogout = undefined as (() => void) | undefined,
  } = $props();

  type Item = { label: string; href: string; page: string };

  // Flat top-nav — ROVER deep-review stays reachable at /rover/* but is not in the nav.
  const ITEMS: Item[] = [
    { label: 'Agent',        href: '/agent',        page: 'agent' },
    { label: 'History',      href: '/history',      page: 'history' },
    { label: 'Review',       href: '/review',       page: 'history' },
    // Gated on the same permission as Review — someone who may not review
    // documents has no business settling individual values on them.
    { label: 'Checks',       href: '/checks',       page: 'history' },
    { label: 'Items',        href: '/items',        page: 'items' },
    { label: 'Declarations', href: '/declarations', page: 'declarations' },
    { label: 'Costs',        href: '/costs',        page: 'costs' },
    { label: 'Settings',     href: '/settings',     page: 'settings' },
  ];

  const items = $derived(ITEMS.filter(i => auth.canPage(i.page)));
  const currentPath = $derived(page.url.pathname);
  const initial = $derived(username ? username[0].toUpperCase() : '?');

  // Outstanding checks, shown as a badge on the Checks tab. Fail-quiet: a nav bar
  // must never break because a count could not be fetched. Paused while the tab
  // is hidden, matching the stats polling in +layout.
  let checkCount = $state(0);
  async function refreshCount() {
    if (!auth.isAuthenticated) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    try { checkCount = (await api.evidenceCount()).count || 0; } catch {}
  }
  onMount(() => {
    refreshCount();
    const t = setInterval(refreshCount, 60000);
    return () => clearInterval(t);
  });
  // Re-count on navigation so settling a value updates the badge immediately.
  $effect(() => { void currentPath; refreshCount(); });
</script>

<header class="sticky top-0 z-50 w-full"
        style="background: var(--surface-container-lowest); border-bottom: 1px solid var(--line);">
  <div class="mx-auto max-w-[1760px] px-4 md:px-8 flex items-center gap-3 md:gap-5" style="height: 52px;">

    <!-- Brand -->
    <a href="/agent" class="no-underline shrink-0 flex items-center">
      <img src="/cityagent-logo-web.png" alt="CityAgent" style="height: 26px; width: auto;" />
    </a>

    <!-- Tabs — horizontally scrollable on small screens -->
    <nav class="flex-1 flex items-center gap-1 overflow-x-auto custom-scrollbar" style="height: 100%; scrollbar-width: none;">
      {#each items as item}
        {@const active = currentPath.startsWith(item.href)}
        <a href={item.href}
           class="flex items-center px-3 text-sm no-underline whitespace-nowrap transition-colors"
           style="height: 100%; border-bottom: 2px solid {active ? 'var(--primary)' : 'transparent'}; {active
             ? 'color: var(--primary-hover); font-weight: 600;'
             : 'color: var(--on-surface-muted);'}"
           onmouseenter={(e) => { if (!active) e.currentTarget.style.color = 'var(--on-surface)'; }}
           onmouseleave={(e) => { if (!active) e.currentTarget.style.color = 'var(--on-surface-muted)'; }}
        >
          {item.label}
          {#if item.href === '/checks' && checkCount > 0}
            <span class="ml-1.5 px-1.5 text-[10px] font-semibold leading-[16px]"
                  style="background: var(--warning); color: var(--surface-container-lowest);
                         border-radius: 999px; font-variant-numeric: tabular-nums;">
              {checkCount}
            </span>
          {/if}
        </a>
      {/each}
    </nav>

    <!-- User / logout -->
    <div class="flex items-center gap-2.5 shrink-0">
      <div class="w-7 h-7 flex items-center justify-center font-medium text-xs text-white shrink-0"
           style="background: var(--tertiary); border-radius: 50%;" title={username}>
        {initial}
      </div>
      <div class="hidden md:block min-w-0 max-w-[160px]">
        <div class="text-xs font-medium truncate" style="color: var(--on-surface); line-height: 1.2;">{username || '—'}</div>
        <div class="text-[10px] truncate" style="color: var(--on-surface-subtle); line-height: 1.2;">
          {role}{auth.user?.auth_source ? ' · ' + auth.user.auth_source : ''}
        </div>
      </div>
      <button onclick={onlogout} title="Logout" aria-label="Logout"
              class="text-xs font-medium px-2 py-1 cursor-pointer transition-colors shrink-0"
              style="color: var(--on-surface-muted); border: 1px solid var(--line); background: transparent; border-radius: var(--radius-md);"
              onmouseenter={(e) => { e.currentTarget.style.background = 'var(--surface-container)'; e.currentTarget.style.color = 'var(--on-surface)'; }}
              onmouseleave={(e) => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--on-surface-muted)'; }}>
        ⎋
      </button>
    </div>
  </div>
</header>
