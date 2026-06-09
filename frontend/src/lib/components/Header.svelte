<script lang="ts">
  import { page } from '$app/state';
  import { auth } from '$lib/stores/auth.svelte';

  let {
    username = '',
    role = '',
    onlogout = undefined as (() => void) | undefined,
  } = $props();

  const navItems = $derived(
    [
      { label: 'Agent', href: '/agent', icon: 'description', page: 'agent' },
      { label: 'History', href: '/history', icon: 'history', page: 'history' },
      { label: 'Review', href: '/review', icon: 'checklist', page: 'history' },
      { label: 'Items', href: '/items', icon: 'inventory_2', page: 'items' },
      { label: 'Declarations', href: '/declarations', icon: 'receipt_long', page: 'declarations' },
      { label: 'Costs', href: '/costs', icon: 'payments', page: 'costs' },
      { label: 'Settings', href: '/settings', icon: 'settings', page: 'settings' },
    ].filter(item => auth.canPage(item.page))
  );

  const currentPath = $derived(page.url.pathname);
  const initial = $derived(username ? username[0].toUpperCase() : '?');
</script>

<header class="fixed top-0 w-full z-50"
        style="background: rgba(245,244,238,0.85); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-bottom: 1px solid var(--outline-variant);">
  <div class="flex items-center justify-between px-8 py-3 max-w-[1920px] mx-auto">
    <!-- Brand -->
    <a href="/agent" class="flex items-center gap-2 no-underline" style="color: var(--on-surface);">
      <span class="w-7 h-7 flex items-center justify-center text-white font-medium text-xs"
            style="background: var(--primary); border-radius: var(--radius-sm);">RO</span>
      <span class="font-serif text-lg" style="letter-spacing: -0.01em;">RO‑ED</span>
      <span class="text-xs" style="color: var(--on-surface-muted);">Command Center</span>
    </a>

    <!-- Nav -->
    <nav class="hidden md:flex items-center gap-1">
      {#each navItems as item}
        {@const active = currentPath.startsWith(item.href)}
        <a href={item.href}
           class="px-3 py-1.5 text-sm font-medium transition-colors no-underline"
           style="border-radius: var(--radius-md); {active
             ? 'background: var(--primary-container); color: var(--primary); font-weight: 600;'
             : 'color: var(--on-surface-muted);'}"
           onmouseenter={(e) => { if (!active) { e.currentTarget.style.background = 'var(--surface-container-low)'; e.currentTarget.style.color = 'var(--on-surface)'; } }}
           onmouseleave={(e) => { if (!active) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--on-surface-muted)'; } }}
        >
          {item.label}
        </a>
      {/each}
    </nav>

    <!-- User -->
    <div class="flex items-center gap-3">
      {#if auth.user?.auth_source}
        <span class="tag-label" style="text-transform: uppercase; letter-spacing: 0.04em;">
          {auth.user.auth_source}
        </span>
      {/if}
      <button onclick={onlogout}
              class="text-xs font-medium px-3 py-1.5 cursor-pointer transition-colors"
              style="color: var(--on-surface-muted); border: 1px solid var(--outline-variant); background: transparent; border-radius: var(--radius-md);"
              onmouseenter={(e) => { e.currentTarget.style.background = 'var(--surface-container)'; e.currentTarget.style.color = 'var(--on-surface)'; }}
              onmouseleave={(e) => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--on-surface-muted)'; }}>
        Logout
      </button>
      <div class="w-9 h-9 flex items-center justify-center font-medium text-sm text-white"
           style="background: var(--tertiary); border-radius: 50%;">
        {initial}
      </div>
    </div>
  </div>
</header>
