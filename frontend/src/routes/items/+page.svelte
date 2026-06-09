<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import DataTable from '$lib/components/DataTable.svelte';
  import Button from '$lib/components/Button.svelte';

  let items = $state<any[]>([]);
  let loading = $state(true);
  let searchQuery = $state('');
  let dateFrom = $state('');
  let dateTo = $state('');

  const columns = [
    { key: 'job_id', label: 'Job' },
    { key: 'item_name', label: 'Item Name' },
    { key: 'customs_duty_rate', label: 'Duty Rate', align: 'right' as const },
    { key: 'quantity', label: 'Quantity' },
    { key: 'invoice_unit_price', label: 'Invoice Price', align: 'right' as const },
    { key: 'cif_unit_price', label: 'CIF Price', align: 'right' as const },
    { key: 'commercial_tax_percent', label: 'Tax %', align: 'right' as const },
    { key: 'exchange_rate', label: 'Exchange Rate' },
    { key: 'hs_code', label: 'HS Code' },
    { key: 'origin_country', label: 'Origin Country' },
    { key: 'customs_value_mmk', label: 'Customs Value (MMK)', align: 'right' as const },
    { key: 'created_at', label: 'Processed' },
  ];

  const filteredItems = $derived(() => {
    let result = items;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(i =>
        Object.values(i).some(v => v != null && String(v).toLowerCase().includes(q))
      );
    }
    if (dateFrom) {
      result = result.filter(i => (i.created_at || '').split(' ')[0] >= dateFrom);
    }
    if (dateTo) {
      result = result.filter(i => (i.created_at || '').split(' ')[0] <= dateTo);
    }
    return result;
  });

  async function downloadExcel() {
    try {
      const res = await fetch('/api/data/items/download', {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'all_product_items.xlsx';
      a.click();
      URL.revokeObjectURL(url);
    } catch {}
  }

  function clearFilters() {
    searchQuery = '';
    dateFrom = '';
    dateTo = '';
  }

  onMount(async () => {
    try { items = await api.listItems(); } catch {}
    loading = false;
  });
</script>


<ChapterHeading
  icon="inventory_2"
  title="PRODUCT_ITEMS"
  subtitle="Consolidated product items across all extraction jobs"
  question="What products have been imported?"
/>

<!-- Filters -->
<div class="flex flex-wrap gap-3 items-end mb-5">
  <div class="flex-1 min-w-[200px]">
    <label class="cl-lbl" for="item-search">Search</label>
    <input id="item-search" type="text" placeholder="Search any column..."
           bind:value={searchQuery} class="cl-inp" />
  </div>
  <div>
    <label class="cl-lbl" for="item-from">From</label>
    <input id="item-from" type="date" bind:value={dateFrom} class="cl-inp" />
  </div>
  <div>
    <label class="cl-lbl" for="item-to">To</label>
    <input id="item-to" type="date" bind:value={dateTo} class="cl-inp" />
  </div>
  {#if searchQuery || dateFrom || dateTo}
    <button class="cl-btn sm" onclick={clearFilters}>Clear</button>
  {/if}
  <button class="cl-btn sm" onclick={downloadExcel}>
    <span class="inline-flex items-center gap-1">
      <span class="material-symbols-outlined text-xs">download</span> Download XLSX
    </span>
  </button>
</div>

{#if loading}
  <div class="skeleton h-64 w-full"></div>
{:else}
  {@const rows = filteredItems()}
  <div class="cl-panel">
    <div class="cl-hd">
      <span class="dot">◉</span>Product Items
      <span class="ct">{rows.length} {rows.length === 1 ? 'record' : 'records'}</span>
    </div>
    <div class="overflow-x-auto custom-scrollbar" style="max-height: 500px; overflow-y: auto;">
      <table class="cl-table">
        <thead class="sticky top-0 z-[1]">
          <tr>
            {#each columns as col}
              <th style="text-align: {col.align || 'left'}; white-space: nowrap;">{col.label}</th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each rows as row}
            <tr>
              {#each columns as col}
                <td class="mono" style="text-align: {col.align || 'left'};">{row[col.key] ?? '—'}</td>
              {/each}
            </tr>
          {/each}
          {#if rows.length === 0}
            <tr>
              <td colspan={columns.length} class="px-4 py-12 text-center" style="color: var(--on-surface-subtle);">No data</td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  </div>
{/if}
