<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import DataTable from '$lib/components/DataTable.svelte';
  import Button from '$lib/components/Button.svelte';

  let declarations = $state<any[]>([]);
  let loading = $state(true);
  let searchQuery = $state('');
  let dateFrom = $state('');
  let dateTo = $state('');

  const columns = [
    { key: 'job_id', label: 'Job' },
    { key: 'declaration_no', label: 'Declaration No' },
    { key: 'declaration_date', label: 'Date' },
    { key: 'importer_name', label: 'Importer' },
    { key: 'consignor_name', label: 'Consignor' },
    { key: 'invoice_number_customs_declaration', label: 'Invoice Number (Customs Declaration)' },
    { key: 'invoice_number_commercial_invoice', label: 'Invoice Number (Commercial Invoice)' },
    { key: 'invoice_number', label: 'Invoice Number' },
    { key: 'invoice_price', label: 'Invoice Price', align: 'right' as const },
    { key: 'freight_value', label: 'Freight', align: 'right' as const },
    { key: 'insurance_value', label: 'Insurance', align: 'right' as const },
    { key: 'adjustment_value', label: 'Adjustment', align: 'right' as const },
    { key: 'currency', label: 'Currency' },
    { key: 'exchange_rate', label: 'Exchange Rate', align: 'right' as const },
    { key: 'currency_2', label: 'Currency 2' },
    { key: 'total_customs_value', label: 'Customs Value', align: 'right' as const },
    { key: 'import_export_customs_duty', label: 'Duty', align: 'right' as const },
    { key: 'commercial_tax_ct', label: 'Tax', align: 'right' as const },
    { key: 'advance_income_tax_at', label: 'Income Tax', align: 'right' as const },
    { key: 'security_fee_sf', label: 'Security', align: 'right' as const },
    { key: 'maccs_service_fee_mf', label: 'MACCS', align: 'right' as const },
    { key: 'exemption_reduction', label: 'Exemption/Reduction', align: 'right' as const },
    { key: 'document_format', label: 'Format' },
    { key: 'verified_display', label: 'Verified' },
    { key: 'created_at', label: 'Processed' },
  ];

  const fmtNum = (v: any) => v != null ? Number(v).toLocaleString() : '—';

  const filteredDeclarations = $derived(() => {
    let result = declarations.map(d => ({
      ...d,
      invoice_price: fmtNum(d.invoice_price),
      freight_value: fmtNum(d.freight_value),
      insurance_value: fmtNum(d.insurance_value),
      adjustment_value: fmtNum(d.adjustment_value),
      exchange_rate: d.exchange_rate != null ? d.exchange_rate : '—',
      total_customs_value: fmtNum(d.total_customs_value),
      import_export_customs_duty: fmtNum(d.import_export_customs_duty),
      commercial_tax_ct: fmtNum(d.commercial_tax_ct),
      advance_income_tax_at: fmtNum(d.advance_income_tax_at),
      security_fee_sf: fmtNum(d.security_fee_sf),
      maccs_service_fee_mf: fmtNum(d.maccs_service_fee_mf),
      exemption_reduction: fmtNum(d.exemption_reduction),
      document_format: d.document_format || '—',
      verified_display: d.verified === false ? '✗' : d.verified === true ? '✓' : '—',
    }));
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(d =>
        Object.values(d).some(v => v != null && String(v).toLowerCase().includes(q))
      );
    }
    if (dateFrom) {
      result = result.filter(d => (d.declaration_date || d.created_at || '').split(' ')[0] >= dateFrom);
    }
    if (dateTo) {
      result = result.filter(d => (d.declaration_date || d.created_at || '').split(' ')[0] <= dateTo);
    }
    return result;
  });

  async function downloadExcel() {
    try {
      const res = await fetch('/api/data/declarations/download', {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'all_declarations.xlsx';
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
    try { declarations = await api.listDeclarations(); } catch {}
    loading = false;
  });
</script>


<ChapterHeading
  icon="receipt_long"
  title="DECLARATION_DATA"
  subtitle="Consolidated customs declarations across all jobs"
  question="What are the customs values and duties?"
/>

<!-- Filters -->
<div class="flex flex-wrap gap-3 items-end mb-5">
  <div class="flex-1 min-w-[200px]">
    <label class="cl-lbl" for="decl-search">Search</label>
    <input id="decl-search" type="text" placeholder="Search any column..."
           bind:value={searchQuery} class="cl-inp" />
  </div>
  <div>
    <label class="cl-lbl" for="decl-from">From</label>
    <input id="decl-from" type="date" bind:value={dateFrom} class="cl-inp" />
  </div>
  <div>
    <label class="cl-lbl" for="decl-to">To</label>
    <input id="decl-to" type="date" bind:value={dateTo} class="cl-inp" />
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
  {@const rows = filteredDeclarations()}
  <div class="cl-panel">
    <div class="cl-hd">
      <span class="dot">◉</span>Customs Declarations
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
