<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';

  let {
    username = '',
    role = '',
    onlogout = undefined as (() => void) | undefined,
    // Owned here (with the localStorage persistence) but readable by the layout
    // so the main column can reserve the right amount of space.
    collapsed = $bindable(false),
  } = $props();

  type Item = { label: string; href: string; page: string; icon: string };

  // Heroicons (outline, 24×24) inlined — one icon set, no dependency.
  const ICON = {
    sparkles:
      'M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z',
    clock: 'M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
    clipboardCheck:
      'M11.35 3.836c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75 2.25 2.25 0 0 0-.1-.664m-5.8 0A2.251 2.251 0 0 1 13.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m8.9-4.414c.376.023.75.05 1.124.08 1.131.094 1.976 1.057 1.976 2.192V16.5A2.25 2.25 0 0 1 18 18.75h-2.25m-7.5-10.5H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V18.75m-7.5-10.5h6.375c.621 0 1.125.504 1.125 1.125v9.375m-8.25-3 1.5 1.5 3-3.75',
    warning:
      'M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z',
    cube: 'M21 7.5 12 2.25 3 7.5m18 0-9 5.25m9-5.25v9L12 21.75m0-9L3 7.5m9 5.25v9M3 7.5v9l9 5.25',
    documentText:
      'M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z',
    chartBar:
      'M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z',
    sliders:
      'M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75',
    bars: 'M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H16.5',
    chevronLeft: 'M15.75 19.5 8.25 12l7.5-7.5',
    logout:
      'M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15M12 9l-3 3m0 0 3 3m-3-3h12.75',
  };

  // Same order, routes and `page` permission keys as the old TopNav — ROVER
  // deep-review stays reachable at /rover/* but is deliberately not in the nav.
  const ITEMS: Item[] = [
    { label: 'Agent',        href: '/agent',        page: 'agent',        icon: ICON.sparkles },
    { label: 'History',      href: '/history',      page: 'history',      icon: ICON.clock },
    { label: 'Review',       href: '/review',       page: 'history',      icon: ICON.clipboardCheck },
    // Gated on the same permission as Review — someone who may not review
    // documents has no business settling individual values on them.
    { label: 'Checks',       href: '/checks',       page: 'history',      icon: ICON.warning },
    { label: 'Items',        href: '/items',        page: 'items',        icon: ICON.cube },
    { label: 'Declarations', href: '/declarations', page: 'declarations', icon: ICON.documentText },
    { label: 'Costs',        href: '/costs',        page: 'costs',        icon: ICON.chartBar },
    { label: 'Settings',     href: '/settings',     page: 'settings',     icon: ICON.sliders },
  ];

  const items = $derived(ITEMS.filter(i => auth.canPage(i.page)));
  const currentPath = $derived(page.url.pathname);
  const initial = $derived(username ? username[0].toUpperCase() : '?');
  // `auth_source` is set by the LDAP/Keycloak paths but is not on the User type.
  const authSource = $derived((auth.user as { auth_source?: string } | null)?.auth_source ?? '');

  const STORE_KEY = 'roed.sidebar.collapsed';

  let drawerOpen = $state(false);
  let isNarrow = $state(false);

  // The collapse preference only applies to the docked sidebar. Below 640px the
  // drawer is always the full 224px with labels — an icon-only off-canvas panel
  // would be a strictly worse hamburger.
  const compact = $derived(collapsed && !isNarrow);

  onMount(() => {
    try { collapsed = localStorage.getItem(STORE_KEY) === '1'; } catch {}
  });

  $effect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia('(max-width: 639px)');
    isNarrow = mq.matches;
    const on = (e: MediaQueryListEvent) => { isNarrow = e.matches; };
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  });

  function toggleCollapse() {
    collapsed = !collapsed;
    try { localStorage.setItem(STORE_KEY, collapsed ? '1' : '0'); } catch {}
  }

  // Navigating inside the drawer should close it.
  $effect(() => { void currentPath; drawerOpen = false; });

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && drawerOpen) drawerOpen = false;
  }

  // Outstanding checks, shown as a badge on the Checks item. Fail-quiet: the nav
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

<svelte:window onkeydown={onKeydown} />

<!-- Mobile bar — replaces the old sticky top nav below 640px -->
<div class="sb-topbar">
  <button
    class="sb-icon-btn"
    aria-label="Open navigation"
    aria-expanded={drawerOpen}
    onclick={() => (drawerOpen = true)}
  >
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <path stroke-linecap="round" stroke-linejoin="round" d={ICON.bars} />
    </svg>
  </button>
  <a href="/agent" class="sb-brand-sm"><img src="/cityagent-logo-web.png" alt="CityAgent" /></a>
</div>

{#if drawerOpen}
  <button class="sb-backdrop" aria-label="Close navigation" onclick={() => (drawerOpen = false)}></button>
{/if}

<aside class="sb" class:sb-collapsed={compact} class:sb-open={drawerOpen}>
  <div class="sb-head">
    {#if !compact}
      <a href="/agent" class="sb-brand"><img src="/cityagent-logo-web.png" alt="CityAgent" /></a>
    {/if}
    <button
      class="sb-icon-btn sb-collapse"
      class:sb-flip={collapsed}
      aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      onclick={toggleCollapse}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d={ICON.chevronLeft} />
      </svg>
    </button>
  </div>

  <nav class="sb-nav" aria-label="Main">
    {#each items as item (item.href)}
      {@const active = currentPath.startsWith(item.href)}
      <a
        href={item.href}
        class="sb-item"
        class:sb-active={active}
        aria-current={active ? 'page' : undefined}
        title={compact ? item.label : undefined}
      >
        <svg class="sb-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <path stroke-linecap="round" stroke-linejoin="round" d={item.icon} />
        </svg>
        {#if !compact}
          <span class="sb-label">{item.label}</span>
          {#if item.href === '/checks' && checkCount > 0}
            <span class="sb-count">{checkCount}</span>
          {/if}
        {/if}
      </a>
    {/each}
  </nav>

  <div class="sb-user">
    <div class="sb-avatar" title={username}>{initial}</div>
    {#if !compact}
      <div class="sb-who">
        <div class="sb-who-name">{username || '—'}</div>
        <div class="sb-who-role">{role}{authSource ? ' · ' + authSource : ''}</div>
      </div>
    {/if}
    <button class="sb-icon-btn" onclick={onlogout} aria-label="Log out" title="Log out">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d={ICON.logout} />
      </svg>
    </button>
  </div>
</aside>

<style>
  .sb {
    position: fixed;
    inset-block-start: 0;
    /* Stop above the 40px fixed footer so it keeps its full width. */
    inset-block-end: 40px;
    inset-inline-start: 0;
    z-index: 45;
    width: 224px;
    display: flex;
    flex-direction: column;
    background: var(--sunk);
    border-inline-end: 1px solid var(--line);
    font-size: 12px;
    font-weight: 500;
    transition: width 180ms ease, transform 180ms ease;
  }
  .sb-collapsed { width: 56px; }

  /* ---- head ---- */
  .sb-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    height: 48px;
    padding-inline: 8px;
    border-block-end: 1px solid var(--line-soft);
  }
  .sb-collapsed .sb-head { justify-content: center; }
  .sb-brand { display: flex; align-items: center; min-width: 0; text-decoration: none; }
  .sb-brand img { height: 22px; width: auto; }

  /* ---- nav ---- */
  .sb-nav {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 6px;
  }
  .sb-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 6px;
    color: var(--ink-3);
    text-decoration: none;
    white-space: nowrap;
    transition: background-color 150ms ease, color 150ms ease;
  }
  .sb-collapsed .sb-item { justify-content: center; padding-inline: 0; }
  .sb-item:hover { background: var(--hover); color: var(--ink-2); }
  .sb-active,
  .sb-active:hover { background: var(--accent-weak); color: var(--accent); }
  .sb-ic { width: 16px; height: 16px; flex: none; }
  .sb-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .sb-count {
    flex: none;
    font-family: var(--mono, ui-monospace, SFMono-Regular, monospace);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
    color: var(--ink-3);
  }
  .sb-active .sb-count { color: var(--accent); }

  /* ---- user block ---- */
  .sb-user {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px;
    border-block-start: 1px solid var(--line-soft);
  }
  .sb-collapsed .sb-user { flex-direction: column; gap: 6px; }
  .sb-avatar {
    flex: none;
    width: 24px;
    height: 24px;
    border-radius: 999px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 500;
    background: var(--accent-weak);
    color: var(--accent);
  }
  .sb-who { flex: 1; min-width: 0; }
  .sb-who-name {
    font-size: 12px;
    color: var(--ink-2);
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sb-who-role {
    font-size: 10px;
    color: var(--ink-4);
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ---- buttons ---- */
  .sb-icon-btn {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 26px;
    padding: 0;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--ink-4);
    cursor: pointer;
    transition: background-color 150ms ease, color 150ms ease;
  }
  .sb-icon-btn:hover { background: var(--hover); color: var(--ink-2); }
  .sb-icon-btn svg { width: 16px; height: 16px; }
  .sb-flip svg { transform: rotate(180deg); }

  .sb-item:focus-visible,
  .sb-brand:focus-visible,
  .sb-brand-sm:focus-visible,
  .sb-icon-btn:focus-visible,
  .sb-backdrop:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  /* ---- mobile ---- */
  .sb-topbar { display: none; }
  .sb-backdrop { display: none; }

  @media (max-width: 639px) {
    .sb-topbar {
      position: fixed;
      inset-block-start: 0;
      inset-inline: 0;
      z-index: 44;
      height: 48px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding-inline: 8px;
      background: var(--surface);
      border-block-end: 1px solid var(--line);
    }
    .sb-brand-sm { display: flex; align-items: center; text-decoration: none; }
    .sb-brand-sm img { height: 22px; width: auto; }

    .sb-backdrop {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 46;
      border: 0;
      padding: 0;
      background: rgb(0 0 0 / 0.35);
      cursor: pointer;
    }

    .sb {
      inset-block-end: 0;
      z-index: 47;
      width: 224px;
      transform: translateX(-100%);
    }
    /* Off-canvas is a left-edge drawer in both writing directions. */
    :global([dir='rtl']) .sb { transform: translateX(100%); }
    .sb-collapsed { width: 224px; }
    .sb-open { transform: translateX(0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .sb, .sb-item, .sb-icon-btn { transition-duration: 0.01ms; }
  }
</style>
