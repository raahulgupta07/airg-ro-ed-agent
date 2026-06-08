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
<div class="flex flex-wrap gap-3 items-end mb-4">
  <div class="flex-1 min-w-[200px]">
    <input type="text" placeholder="Search any column..."
           bind:value={searchQuery}
           class="w-full text-xs font-bold uppercase px-3 py-2 focus:outline-none"
           style="border: 1px solid var(--on-surface); background: white; color: var(--on-surface);" />
  </div>
  <div>
    <div class="tag-label mb-1 text-[8px]">FROM</div>
    <input type="date" bind:value={dateFrom}
           class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
           style="border: 1px solid var(--on-surface); background: white; color: var(--on-surface);" />
  </div>
  <div>
    <div class="tag-label mb-1 text-[8px]">TO</div>
    <input type="date" bind:value={dateTo}
           class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
           style="border: 1px solid var(--on-surface); background: white; color: var(--on-surface);" />
  </div>
  {#if searchQuery || dateFrom || dateTo}
    <button class="text-[8px] font-medium uppercase px-2 py-1.5 cursor-pointer"
            style="border: 1px solid var(--outline); color: var(--outline); background: transparent;"
            onclick={clearFilters}>CLEAR</button>
  {/if}
  <Button variant="secondary" size="sm" onclick={downloadExcel}>
    <span class="flex items-center gap-1">
      <span class="material-symbols-outlined text-xs">download</span> DOWNLOAD_XLSX
    </span>
  </Button>
</div>

{#if loading}
  <div class="skeleton h-64 w-full"></div>
{:else}
  <DataTable title="PRODUCT_ITEMS" count={filteredItems().length} {columns} rows={filteredItems()} />
{/if}
