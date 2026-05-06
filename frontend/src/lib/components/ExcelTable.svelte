<script lang="ts">
  /** Excel-style table for Svelte 5 — native, no external deps.
   *  Sticky header, click-sort asc/desc, drag-resize cols, frozen first col,
   *  zebra rows, cell borders, CSV export, brutalist theme.
   *  Optional: inline editing, page-jump badges, row actions (▲▼🗑), add row. */

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
    // ── New: inline editing ──
    editable = false,
    onCellEdit = (_row: any, _col: Col, _newValue: any) => {},
    isCellEdited = (_r: any, _c: Col) => false,
    // ── New: page jump badge ──
    pageRefAccessor = (_row: any): number | null | undefined => undefined,
    onPageJump = undefined as undefined | ((page: number) => void),
    // ── New: row actions ──
    enableRowActions = false,
    onRowMoveUp = (_idx: number) => {},
    onRowMoveDown = (_idx: number) => {},
    onRowDelete = (_idx: number) => {},
    // ── New: add row ──
    enableAddRow = false,
    onAddRow = () => {},
  }: {
    columns: Col[]; data: any[];
    enableSort?: boolean; enableResize?: boolean;
    exportFilename?: string;
    onRowClick?: (r: any) => void;
    rowClass?: (r: any) => string;
    title?: string;
    maxHeight?: string;
    editable?: boolean;
    onCellEdit?: (row: any, col: Col, newValue: any) => any;
    isCellEdited?: (r: any, c: Col) => boolean;
    pageRefAccessor?: (row: any) => number | null | undefined;
    onPageJump?: (page: number) => void;
    enableRowActions?: boolean;
    onRowMoveUp?: (idx: number) => void;
    onRowMoveDown?: (idx: number) => void;
    onRowDelete?: (idx: number) => void;
    enableAddRow?: boolean;
    onAddRow?: () => void;
  } = $props();

  let sortKey = $state<string | null>(null);
  let sortDir = $state<'asc' | 'desc' | null>(null);
  let widths = $state<Record<string, number>>({});

  // ── Inline editing state ──
  let editingCell = $state<{ rowIdx: number; colId: string } | null>(null);
  let editValue = $state<string>('');

  function autofocus(node: HTMLInputElement) {
    node.focus();
    node.select();
  }

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

  // ── Inline-edit helpers ──
  function startEdit(rowIdx: number, colId: string, current: any) {
    editingCell = { rowIdx, colId };
    editValue = current == null ? '' : String(current);
  }
  function cancelEdit() {
    editingCell = null;
    editValue = '';
  }
  function saveCell() {
    if (!editingCell) return;
    const { rowIdx, colId } = editingCell;
    const row = sortedData[rowIdx];
    const col = columns.find(c => c.id === colId);
    if (!row || !col) { cancelEdit(); return; }
    const before = cellValue(row, col);
    const after = editValue;
    editingCell = null;
    if (String(before ?? '') === String(after ?? '')) return;
    try {
      onCellEdit(row, col, after);
    } catch {
      // parent handles revert
    }
  }
  function moveNext() {
    if (!editingCell) return;
    const { rowIdx, colId } = editingCell;
    const editableCols = columns.filter(c => !c.cell || c.accessor); // only those backed by accessor
    const idxInRow = editableCols.findIndex(c => c.id === colId);
    if (idxInRow < 0) return;
    let nextRowIdx = rowIdx;
    let nextColIdx = idxInRow + 1;
    if (nextColIdx >= editableCols.length) {
      nextColIdx = 0;
      nextRowIdx = rowIdx + 1;
    }
    if (nextRowIdx >= sortedData.length) return;
    const nextCol = editableCols[nextColIdx];
    const nextRow = sortedData[nextRowIdx];
    startEdit(nextRowIdx, nextCol.id, cellValue(nextRow, nextCol));
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

  // Extra width contributed by jump-badge column + row-actions column (rendered inside last data cell area).
  // We render badges/actions inline within their columns to keep totalWidth simple.
  const totalWidth = $derived(columns.reduce((s, c) => s + (widths[c.id] || c.width || 120), 0)
    + (enableRowActions ? 90 : 0));
</script>

<div class="border-2 stamp-shadow" style="border-color: var(--on-surface); background: var(--surface);">
  <div class="flex items-center justify-between p-3" style="background: var(--on-surface); color: var(--surface);">
    <span class="text-xs font-black uppercase tracking-wider">{title}</span>
    <div class="flex gap-2 items-center">
      <span class="text-[10px] font-mono">[{data.length} ROWS]</span>
      {#if enableAddRow}
        <button
          class="text-[8px] font-black uppercase px-2 py-1 cursor-pointer"
          style="border: 2px solid white; background: transparent; color: white;"
          onclick={onAddRow}
        >+ ADD</button>
      {/if}
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
        {#if enableRowActions}
          <col style="width: 90px;" />
        {/if}
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
          {#if enableRowActions}
            <th
              class="px-2 py-1.5 text-[10px] font-bold uppercase text-center"
              style="position: sticky; top: 0; z-index: 20;
                     background: #d9d9d9; color: #1f1f1f;
                     border-bottom: 2px solid #666;
                     user-select: none;"
            >ACTIONS</th>
          {/if}
        </tr>
      </thead>
      <tbody>
        {#each sortedData as row, ri (ri)}
          <tr
            class="hover:bg-blue-50 {rowClass(row)}"
            onclick={() => onRowClick(row)}
          >
            {#each columns as c, ci (c.id)}
              {@const isEditingThis = editable && editingCell?.rowIdx === ri && editingCell?.colId === c.id}
              {@const edited = isCellEdited(row, c)}
              {@const isLast = ci === columns.length - 1 && !enableRowActions}
              {@const pageRef = isLast ? pageRefAccessor(row) : null}
              <td
                class="px-2 py-1
                       {c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left'}
                       {editable ? 'cursor-text' : 'cursor-pointer'}"
                style="border-right: 1px solid #d4d4d4;
                       border-bottom: 1px solid #d4d4d4;
                       background: {ci === 0 && c.frozen ? '#ffffff' : (ri % 2 === 0 ? '#ffffff' : '#f9f9f9')};
                       {ci === 0 && c.frozen ? 'position: sticky; left: 0; z-index: 10;' : ''}
                       color: #1f1f1f;
                       {edited ? 'box-shadow: inset 0 0 0 2px #3b82f6;' : ''}
                       overflow: hidden;
                       white-space: nowrap;
                       text-overflow: ellipsis;"
                title={String(cellValue(row, c) ?? '')}
              >
                {#if isEditingThis}
                  <input
                    bind:value={editValue}
                    class="w-full px-1 py-0.5 text-[11px]"
                    style="border: 2px solid #3b82f6; outline: none; background: white; color: #1f1f1f; font-family: inherit;"
                    onkeydown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); saveCell(); }
                      else if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
                      else if (e.key === 'Tab') { e.preventDefault(); saveCell(); moveNext(); }
                    }}
                    onblur={saveCell}
                    onclick={(e) => e.stopPropagation()}
                    use:autofocus
                  />
                {:else if editable}
                  <div class="flex items-center justify-between gap-1">
                    <button
                      class="flex-1 text-left bg-transparent border-0 p-0 m-0 cursor-text truncate
                             {c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left'}"
                      style="color: inherit; font: inherit;"
                      onclick={(e) => { e.stopPropagation(); startEdit(ri, c.id, cellValue(row, c)); }}
                    >{cellValue(row, c) ?? ''}</button>
                    {#if isLast && pageRef}
                      <button
                        class="text-[8px] font-mono font-bold cursor-pointer"
                        style="color: #2563eb; background: transparent; border: none; padding: 0 2px;"
                        onclick={(e) => { e.stopPropagation(); onPageJump?.(pageRef); }}
                        title="Jump to page {pageRef}"
                      >📍p{pageRef}</button>
                    {/if}
                  </div>
                {:else}
                  <div class="flex items-center justify-between gap-1">
                    <span class="truncate {c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left'}">{cellValue(row, c) ?? ''}</span>
                    {#if isLast && pageRef}
                      <button
                        class="text-[8px] font-mono font-bold cursor-pointer"
                        style="color: #2563eb; background: transparent; border: none; padding: 0 2px;"
                        onclick={(e) => { e.stopPropagation(); onPageJump?.(pageRef); }}
                        title="Jump to page {pageRef}"
                      >📍p{pageRef}</button>
                    {/if}
                  </div>
                {/if}
              </td>
            {/each}
            {#if enableRowActions}
              {@const pageRef = pageRefAccessor(row)}
              <td
                class="px-1 py-1 text-center whitespace-nowrap"
                style="border-right: 1px solid #d4d4d4;
                       border-bottom: 1px solid #d4d4d4;
                       background: {ri % 2 === 0 ? '#ffffff' : '#f9f9f9'};"
              >
                <div class="flex items-center justify-center gap-0.5">
                  {#if pageRef}
                    <button
                      class="text-[8px] font-mono font-bold cursor-pointer"
                      style="color: #2563eb; background: transparent; border: none; padding: 0 2px;"
                      onclick={(e) => { e.stopPropagation(); onPageJump?.(pageRef); }}
                      title="Jump to page {pageRef}"
                    >📍p{pageRef}</button>
                  {/if}
                  <button
                    class="px-0.5 text-[10px] font-bold cursor-pointer disabled:opacity-30"
                    style="background: transparent; border: none; color: #1f1f1f;"
                    title="Move up"
                    disabled={ri === 0}
                    onclick={(e) => { e.stopPropagation(); onRowMoveUp(ri); }}
                  >▲</button>
                  <button
                    class="px-0.5 text-[10px] font-bold cursor-pointer disabled:opacity-30"
                    style="background: transparent; border: none; color: #1f1f1f;"
                    title="Move down"
                    disabled={ri === sortedData.length - 1}
                    onclick={(e) => { e.stopPropagation(); onRowMoveDown(ri); }}
                  >▼</button>
                  <button
                    class="px-0.5 text-[12px] cursor-pointer"
                    style="background: transparent; border: none; color: #ef4444;"
                    title="Delete row"
                    onclick={(e) => { e.stopPropagation(); onRowDelete(ri); }}
                  >🗑</button>
                </div>
              </td>
            {/if}
          </tr>
        {:else}
          <tr><td colspan={columns.length + (enableRowActions ? 1 : 0)} class="p-4 text-center" style="color: #888; background: #ffffff;">No data</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>
