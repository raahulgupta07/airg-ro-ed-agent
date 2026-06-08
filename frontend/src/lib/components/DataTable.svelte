<script lang="ts">
  let {
    title = '',
    count = 0,
    columns = [] as { key: string; label: string; align?: string; width?: string }[],
    rows = [] as Record<string, any>[],
    maxHeight = '500px',
  } = $props();
</script>

<div class="ink-border stamp-shadow overflow-hidden">
  <!-- Header -->
  {#if title}
    <div class="dark-bar flex justify-between items-center">
      <span>{title}</span>
      {#if count > 0}
        <span class="tag-label">
          {count} {count === 1 ? 'record' : 'records'}
        </span>
      {/if}
    </div>
  {/if}

  <!-- Table -->
  <div class="overflow-x-auto custom-scrollbar"
       style="max-height: {maxHeight}; overflow-y: auto; background: var(--surface-container-lowest);">
    <table class="border-collapse text-sm w-full">
      <thead class="sticky top-0 z-[1]" style="background: var(--surface-container-low);">
        <tr>
          {#each columns as col}
            <th class="px-4 py-3 text-left font-medium text-xs whitespace-nowrap"
                style="text-align: {col.align || 'left'}; color: var(--on-surface-muted); border-bottom: 1px solid var(--outline-variant); letter-spacing: 0.04em; text-transform: uppercase;{col.width ? ` width: ${col.width};` : ''}">
              {col.label}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as row, i}
          <tr class="transition-colors hover:bg-[var(--surface-container-low)]"
              style="border-bottom: 1px solid var(--outline-variant);">
            {#each columns as col}
              <td class="px-4 py-3 font-mono text-[13px]"
                  style="text-align: {col.align || 'left'}; color: var(--on-surface);">
                {row[col.key] ?? '—'}
              </td>
            {/each}
          </tr>
        {/each}
        {#if rows.length === 0}
          <tr>
            <td colspan={columns.length} class="px-4 py-12 text-center text-sm"
                style="color: var(--on-surface-subtle);">
              No data
            </td>
          </tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>
