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
      { label: 'AGENT', href: '/agent', icon: 'description', page: 'agent' },
      { label: 'HISTORY', href: '/history', icon: 'history', page: 'history' },
      { label: 'REVIEW', href: '/review', icon: 'checklist', page: 'history' },
      { label: 'ITEMS', href: '/items', icon: 'inventory_2', page: 'items' },
      { label: 'DECLARATIONS', href: '/declarations', icon: 'receipt_long', page: 'declarations' },
      { label: 'COSTS', href: '/costs', icon: 'payments', page: 'costs' },
      { label: 'SETTINGS', href: '/settings', icon: 'settings', page: 'settings' },
    ].filter(item => auth.canPage(item.page))
  );

  const currentPath = $derived(page.url.pathname);
  const initial = $derived(username ? username[0].toUpperCase() : '?');
</script>

<header class="fixed top-0 w-full z-50" style="background: var(--surface); border-bottom: 3px solid var(--on-surface);">
  <div class="flex items-center justify-between px-6 py-2 max-w-[1920px] mx-auto">
    <!-- Brand -->
    <div class="text-2xl font-bold tracking-tighter uppercase px-2 py-1"
         style="background: var(--on-surface); color: var(--surface);">
      RO-ED COMMAND CENTER
    </div>

    <!-- Nav -->
    <nav class="hidden md:flex items-center gap-0.5">
      {#each navItems as item}
        <a href={item.href}
           class="px-2 py-1 text-sm font-bold uppercase tracking-tighter transition-colors no-underline"
           style="{currentPath.startsWith(item.href)
             ? 'background: var(--on-surface); color: var(--surface);'
             : 'color: var(--on-surface);'}"
           onmouseenter={(e) => { if (!currentPath.startsWith(item.href)) { e.currentTarget.style.background = '#007518'; e.currentTarget.style.color = 'white'; } }}
           onmouseleave={(e) => { if (!currentPath.startsWith(item.href)) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--on-surface)'; } }}
        >
          {item.label}
        </a>
      {/each}
    </nav>

    <!-- User avatar -->
    <div class="flex items-center gap-3">
      {#if auth.user?.auth_source}
        <span class="text-[10px] px-1.5 py-0.5 rounded bg-zinc-200 text-zinc-700 font-bold uppercase">
          {auth.user.auth_source}
        </span>
      {/if}
      <button onclick={onlogout} class="text-[10px] font-black uppercase px-2 py-1 cursor-pointer"
              style="color: var(--tertiary); border: 1px solid var(--tertiary); background: transparent;">
        LOGOUT
      </button>
      <div class="w-10 h-10 flex items-center justify-center font-bold text-sm text-white"
           style="background: var(--tertiary); border: 2px solid var(--on-surface);">
        {initial}
      </div>
    </div>
  </div>
</header>
