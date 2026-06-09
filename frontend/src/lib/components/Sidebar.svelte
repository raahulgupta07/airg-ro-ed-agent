<script lang="ts">
  import { page } from '$app/state';
  import { auth } from '$lib/stores/auth.svelte';

  let {
    username = '',
    role = '',
    onlogout = undefined as (() => void) | undefined,
  } = $props();

  type Item = { label: string; href: string; icon: string; page: string };
  type Group = { title: string; items: Item[] };

  const GROUPS: Group[] = [
    {
      title: 'Documents',
      items: [
        { label: 'Agent',        href: '/agent',        icon: '⇪', page: 'agent' },
        { label: 'History',      href: '/history',      icon: '▤', page: 'history' },
        { label: 'Review',       href: '/review',       icon: '✓', page: 'history' },
        { label: 'Items',        href: '/items',        icon: '≣', page: 'items' },
        { label: 'Declarations', href: '/declarations', icon: '▣', page: 'declarations' },
      ],
    },
    {
      title: 'Insights',
      items: [
        { label: 'Costs', href: '/costs', icon: '⟐', page: 'costs' },
      ],
    },
    {
      title: 'Admin',
      items: [
        { label: 'Settings', href: '/settings', icon: '⚙', page: 'settings' },
      ],
    },
  ];

  const groups = $derived(
    GROUPS
      .map(g => ({ ...g, items: g.items.filter(i => auth.canPage(i.page)) }))
      .filter(g => g.items.length)
  );

  const currentPath = $derived(page.url.pathname);
  const initial = $derived(username ? username[0].toUpperCase() : '?');
</script>

<aside class="fixed top-0 left-0 h-screen w-[236px] z-50 flex flex-col"
       style="background: var(--surface-container-lowest); border-right: 1px solid var(--line);">

  <!-- Brand -->
  <a href="/agent" class="block no-underline px-4 pt-4 pb-3">
    <img src="/cityagent-logo.png" alt="CityAgent" style="width: 100%; max-width: 190px; height: auto;" />
  </a>

  <!-- Grouped nav -->
  <nav class="flex-1 overflow-y-auto custom-scrollbar px-2.5 pb-2">
    {#each groups as group}
      <div class="px-2.5 pt-3.5 pb-1.5 font-mono uppercase"
           style="font-size: 9.5px; letter-spacing: 0.12em; color: var(--on-surface-subtle);">
        {group.title}
      </div>
      {#each group.items as item}
        {@const active = currentPath.startsWith(item.href)}
        <a href={item.href}
           class="flex items-center gap-2.5 px-2.5 py-2 my-0.5 text-sm no-underline transition-colors"
           style="border-radius: var(--radius-sm); {active
             ? 'background: var(--primary-container); color: var(--primary-hover); font-weight: 600;'
             : 'color: var(--on-surface-muted);'}"
           onmouseenter={(e) => { if (!active) e.currentTarget.style.background = 'var(--surface-container-low)'; }}
           onmouseleave={(e) => { if (!active) e.currentTarget.style.background = 'transparent'; }}
        >
          <span class="w-4 text-center" style="font-size: 14px; {active ? 'color: var(--primary-hover);' : 'color: var(--on-surface-subtle);'}">{item.icon}</span>
          {item.label}
        </a>
      {/each}
    {/each}
  </nav>

  <!-- User / logout -->
  <div class="px-3 py-3 flex items-center gap-2.5" style="border-top: 1px solid var(--line);">
    <div class="w-8 h-8 flex items-center justify-center font-medium text-sm text-white shrink-0"
         style="background: var(--tertiary); border-radius: 50%;">
      {initial}
    </div>
    <div class="min-w-0 flex-1">
      <div class="text-sm font-medium truncate" style="color: var(--on-surface);">{username || '—'}</div>
      <div class="text-xs truncate" style="color: var(--on-surface-subtle);">
        {role}{auth.user?.auth_source ? ' · ' + auth.user.auth_source : ''}
      </div>
    </div>
    <button onclick={onlogout} title="Logout"
            class="text-xs font-medium px-2.5 py-1.5 cursor-pointer transition-colors shrink-0"
            style="color: var(--on-surface-muted); border: 1px solid var(--line); background: transparent; border-radius: var(--radius-md);"
            onmouseenter={(e) => { e.currentTarget.style.background = 'var(--surface-container)'; e.currentTarget.style.color = 'var(--on-surface)'; }}
            onmouseleave={(e) => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--on-surface-muted)'; }}>
      ⎋
    </button>
  </div>
</aside>
