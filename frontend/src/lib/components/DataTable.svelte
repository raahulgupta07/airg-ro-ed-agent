<script lang="ts">
  let {
    title = '',
    count = 0,
    columns = [] as { key: string; label: string; align?: string; width?: string }[],
    rows = [] as Record<string, any>[],
    maxHeight = '500px',
  } = $props();
</script>

<div class="border-2 border-[var(--on-surface)] stamp-shadow">
  <!-- Dark title bar -->
  {#if title}
    <div class="dark-bar flex justify-between items-center">
      <span>{title}</span>
      {#if count > 0}
        <span class="text-[10px] font-bold py-0.5 px-2"
              style="background: var(--surface); color: var(--on-surface);">
          {count} {count === 1 ? 'RECORD' : 'RECORDS'}
        </span>
      {/if}
    </div>
  {/if}

  <!-- Table container -->
  <div class="bg-white overflow-x-auto custom-scrollbar" style="max-height: {maxHeight}; overflow-y: auto;">
    <table class="w-full border-collapse text-xs">
      <thead class="sticky top-0 z-[1]" style="background: var(--surface-container-highest);">
        <tr>
          {#each columns as col}
            <th class="px-4 py-2 text-left font-black uppercase text-[10px] whitespace-nowrap border-b-2 border-[var(--on-surface)]"
                style="text-align: {col.align || 'left'};{col.width ? ` width: ${col.width};` : ''}">
              {col.label}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as row, i}
          <tr class="border-b border-[rgba(56,56,50,0.15)]"
              style="background: {i % 2 === 1 ? 'var(--surface-container-low)' : 'white'};">
            {#each columns as col}
              <td class="px-4 py-2.5 font-mono"
                  style="text-align: {col.align || 'left'}; color: var(--on-surface);">
                {row[col.key] ?? '—'}
              </td>
            {/each}
          </tr>
        {/each}
        {#if rows.length === 0}
          <tr>
            <td colspan={columns.length} class="px-4 py-8 text-center uppercase font-bold text-sm"
                style="color: var(--outline);">
              NO DATA
            </td>
          </tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>
