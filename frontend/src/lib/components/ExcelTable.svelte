<script lang="ts">
  /** Excel-style table for Svelte 5 — native, no external deps.
   *  Sticky header, click-sort asc/desc, drag-resize cols, frozen first col,
   *  zebra rows, cell borders, CSV export, brutalist theme. */

  type Col = {
    id: string;
    header: string;
    accessor?: string;
    width?: number;
    frozen?: boolean;
    cell?: (row: any) => any;
    align?: 'left' | 'right' | 'center';
    enableSort?: boolean;
  };

  let {
    columns = [] as Col[],
    data = [] as any[],
    enableSort = true,
    enableResize = true,
    exportFilename = 'export.csv',
    onRowClick = (_r: any) => {},
    rowClass = (_r: any) => '',
    title = 'DATA_GRID',
    maxHeight = '600px',
  }: {
    columns: Col[]; data: any[];
    enableSort?: boolean; enableResize?: boolean;
    exportFilename?: string;
    onRowClick?: (r: any) => void;
    rowClass?: (r: any) => string;
    title?: string;
    maxHeight?: string;
  } = $props();

  let sortKey = $state<string | null>(null);
  let sortDir = $state<'asc' | 'desc' | null>(null);
  let widths = $state<Record<string, number>>({});

  $effect(() => {
    for (const c of columns) {
      if (widths[c.id] == null) widths[c.id] = c.width || 120;
    }
  });

  const sortedData = $derived.by(() => {
    if (!sortKey || !sortDir) return data;
    const col = columns.find(c => c.id === sortKey);
    if (!col) return data;
    const k = col.accessor || col.id;
    const arr = [...data];
    arr.sort((a, b) => {
      const av = a?.[k]; const bv = b?.[k];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const an = typeof av === 'number' ? av : (isNaN(+av) ? null : +av);
      const bn = typeof bv === 'number' ? bv : (isNaN(+bv) ? null : +bv);
      let cmp;
      if (an != null && bn != null) cmp = an - bn;
      else cmp = String(av).localeCompare(String(bv));
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return arr;
  });

  function toggleSort(colId: string) {
    if (!enableSort) return;
    const col = columns.find(c => c.id === colId);
    if (col?.enableSort === false) return;
    if (sortKey !== colId) { sortKey = colId; sortDir = 'asc'; }
    else if (sortDir === 'asc') { sortDir = 'desc'; }
    else { sortKey = null; sortDir = null; }
  }

  let resizing: { id: string; startX: number; startW: number } | null = null;

  function startResize(e: MouseEvent, id: string) {
    e.preventDefault();
    e.stopPropagation();
    resizing = { id, startX: e.clientX, startW: widths[id] || 120 };
    window.addEventListener('mousemove', onResize);
    window.addEventListener('mouseup', stopResize);
  }
  function onResize(e: MouseEvent) {
    if (!resizing) return;
    const dx = e.clientX - resizing.startX;
    widths[resizing.id] = Math.max(40, resizing.startW + dx);
  }
  function stopResize() {
    resizing = null;
    window.removeEventListener('mousemove', onResize);
    window.removeEventListener('mouseup', stopResize);
  }

  function cellValue(row: any, col: Col) {
    if (col.cell) return col.cell(row);
    return row?.[col.accessor || col.id];
  }

  function exportCsv() {
    const headers = columns.map(c => c.header).join(',');
    const lines = sortedData.map(r => columns.map(c => {
      let v = cellValue(r, c);
      if (v == null) v = '';
      const s = String(v).replace(/"/g, '""');
      return `"${s}"`;
    }).join(','));
    const csv = [headers, ...lines].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = exportFilename; a.click();
    URL.revokeObjectURL(url);
  }

  const totalWidth = $derived(columns.reduce((s, c) => s + (widths[c.id] || c.width || 120), 0));
</script>

<div class="border-2 stamp-shadow" style="border-color: var(--on-surface); background: var(--surface);">
  <div class="flex items-center justify-between p-3" style="background: var(--on-surface); color: var(--surface);">
    <span class="text-xs font-black uppercase tracking-wider">{title}</span>
    <div class="flex gap-2 items-center">
      <span class="text-[10px] font-mono">[{data.length} ROWS]</span>
      <button
        class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
        style="border: 2px solid white; background: transparent; color: white;"
        onclick={exportCsv}
      >↓ CSV</button>
    </div>
  </div>

  <div class="overflow-auto" style="max-height: {maxHeight}; border: 1px solid #999;">
    <table class="text-[11px]"
           style="border-collapse: separate; border-spacing: 0; width: {totalWidth}px; table-layout: fixed; font-family: 'Calibri', 'Segoe UI', sans-serif;">
      <colgroup>
        {#each columns as c (c.id)}
          <col style="width: {widths[c.id] || c.width || 120}px;" />
        {/each}
      </colgroup>
      <thead>
        <tr>
          {#each columns as c, i (c.id)}
            <th
              class="px-2 py-1.5 text-[10px] font-bold uppercase relative
                     {c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left'}
                     {enableSort && c.enableSort !== false ? 'cursor-pointer' : ''}"
              style="position: sticky; top: 0; z-index: {i === 0 && c.frozen ? 30 : 20};
                     {i === 0 && c.frozen ? 'left: 0;' : ''}
                     background: #d9d9d9;
                     color: #1f1f1f;
                     border-right: 1px solid #999;
                     border-bottom: 2px solid #666;
                     user-select: none;
                     letter-spacing: 0.05em;"
              onclick={() => toggleSort(c.id)}
            >
              <div class="flex items-center gap-1 overflow-hidden whitespace-nowrap
                          {c.align === 'right' ? 'justify-end' : c.align === 'center' ? 'justify-center' : 'justify-start'}">
                <span class="truncate">{c.header}</span>
                {#if sortKey === c.id && sortDir === 'asc'}<span>▲</span>
                {:else if sortKey === c.id && sortDir === 'desc'}<span>▼</span>
                {:else if enableSort && c.enableSort !== false}<span class="opacity-30">↕</span>{/if}
              </div>
              {#if enableResize}
                <div
                  role="separator"
                  aria-orientation="vertical"
                  onmousedown={(e) => startResize(e, c.id)}
                  class="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-blue-500"
                  style="background: transparent; user-select: none;"
                ></div>
              {/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each sortedData as row, ri (ri)}
          <tr
            class="cursor-pointer hover:bg-blue-50 {rowClass(row)}"
            onclick={() => onRowClick(row)}
          >
            {#each columns as c, ci (c.id)}
              <td
                class="px-2 py-1 truncate
                       {c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left'}"
                style="border-right: 1px solid #d4d4d4;
                       border-bottom: 1px solid #d4d4d4;
                       background: {ci === 0 && c.frozen ? '#ffffff' : (ri % 2 === 0 ? '#ffffff' : '#f9f9f9')};
                       {ci === 0 && c.frozen ? 'position: sticky; left: 0; z-index: 10;' : ''}
                       color: #1f1f1f;"
                title={String(cellValue(row, c) ?? '')}
              >{cellValue(row, c) ?? ''}</td>
            {/each}
          </tr>
        {:else}
          <tr><td colspan={columns.length} class="p-4 text-center" style="color: #888; background: #ffffff;">No data</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>
