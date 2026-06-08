<script lang="ts">
  import { getPageTypeColor } from '$lib/colors';

  let { pages = [] as any[], onselect = (page: any) => {} } = $props();

  const typeGroups = $derived(() => {
    const groups: Record<string, number> = {};
    for (const p of pages) {
      const t = p.page_type || 'unknown';
      groups[t] = (groups[t] || 0) + 1;
    }
    return Object.entries(groups).sort((a, b) => b[1] - a[1]);
  });
</script>

<div class="border-2" style="border-color: var(--on-surface);">
  <div class="dark-bar flex items-center justify-between text-xs">
    <span>PAGE_MAP</span>
    <span>{pages.length} PAGES / {typeGroups().length} TYPES</span>
  </div>

  <!-- Type legend -->
  <div class="px-3 py-2 flex flex-wrap gap-1.5 border-b" style="border-color: rgba(56,56,50,0.15); background: var(--surface-container);">
    {#each typeGroups() as [type, count]}
      {@const c = getPageTypeColor(type)}
      <span class="px-1.5 py-0.5 text-[8px] font-medium uppercase"
        style="background: {c.bg}; color: {c.text};">
        {type.replace(/_/g, ' ')} ({count})
      </span>
    {/each}
  </div>

  <!-- Page list -->
  <div class="overflow-y-auto bg-white" style="max-height: 500px;">
    {#each pages as page}
      {@const ptc = getPageTypeColor(page.page_type || 'unknown')}
      <button
        class="w-full text-left px-3 py-2 flex gap-3 cursor-pointer border-b transition-colors"
        style="border-color: rgba(56,56,50,0.08);"
        onmouseenter={(e) => (e.currentTarget as HTMLElement).style.background = 'var(--surface-container)'}
        onmouseleave={(e) => (e.currentTarget as HTMLElement).style.background = 'white'}
        onclick={() => onselect(page)}
      >
        <!-- Page number -->
        <div class="flex-shrink-0 w-8 text-right">
          <span class="text-[10px] font-mono font-bold" style="color: var(--outline);">{page.page_number}</span>
        </div>

        <!-- Type bar -->
        <div class="flex-shrink-0 w-1 rounded" style="background: {ptc.bg};"></div>

        <!-- Content -->
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <span class="px-1.5 py-0.5 text-[8px] font-medium uppercase"
              style="background: {ptc.bg}; color: {ptc.text};">
              {page.page_type?.replace(/_/g, ' ') || 'unknown'}
            </span>
            <span class="text-[9px] font-mono" style="color: var(--outline);">
              conf: {(page.confidence ?? 0).toFixed(2)}
            </span>
            {#if page.has_stamp}<span class="text-[8px]" title="Has stamp">&#9733;</span>{/if}
            {#if page.has_signature}<span class="text-[8px]" title="Has signature">&#9998;</span>{/if}
            {#if page.has_barcode}<span class="text-[8px]" title="Has barcode">&#9638;</span>{/if}
          </div>
          <div class="text-[10px] mt-0.5 truncate" style="color: var(--on-surface);">
            {page.explanation || '—'}
          </div>
          <div class="flex gap-3 mt-0.5 text-[9px] font-mono" style="color: var(--outline);">
            {#if page.fields && Object.keys(page.fields).length > 0}
              <span>Fields: {Object.keys(page.fields).length}</span>
            {/if}
            {#if page.items?.length > 0}
              <span>Items: {page.items.length}</span>
            {/if}
            {#if page.amounts?.length > 0}
              <span>Amounts: {page.amounts.length}</span>
            {/if}
          </div>
        </div>

        <!-- Arrow -->
        <div class="flex-shrink-0 self-center">
          <span class="material-symbols-outlined text-sm" style="color: var(--outline);">chevron_right</span>
        </div>
      </button>
    {/each}

    {#if pages.length === 0}
      <div class="p-8 text-center text-xs font-bold uppercase" style="color: var(--outline);">
        No page data available. Page map loads after extraction completes.
      </div>
    {/if}
  </div>
</div>
