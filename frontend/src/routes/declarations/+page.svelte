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
  let dateField = $state('declaration_date');   // which of the several dates the range applies to
  let selectedImporter = $state('');
  let selectedCurrency = $state('');
  let selectedStatus = $state('');              // '' | 'complete' | 'no_total' | 'no_invoice' | 'no_taxes'

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

  // ── These documents carry several dates and they disagree, so the range applies
  // to whichever one is chosen. Only offer a date that is actually in the data —
  // the rest are listed but disabled so the gap is visible rather than silent.
  const DATE_FIELDS = [
    { key: 'declaration_date',   label: 'Declaration date' },
    { key: 'release_order_date', label: 'Release-order date' },
    { key: 'arrival_date',       label: 'Arrival date' },
    { key: 'completion_date',    label: 'Completion date' },
    { key: 'created_at',         label: 'Processed date' },
  ];
  const dateFieldsPresent = $derived(
    new Set(DATE_FIELDS.map(f => f.key).filter(k => declarations.some(d => d?.[k])))
  );

  // ── Dates in the data are TEXT and NOT one format: mostly ISO (2025-06-25), but
  // also 2024/04/01 and day-first 12/10/2025. Normalise to YYYY-MM-DD; return ''
  // when it cannot be parsed so the caller can keep (never drop) the row.
  const _p2 = (n: number | string) => String(n).padStart(2, '0');
  const MONTHS: Record<string, number> = {
    jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
    jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
  };
  function normDate(raw: any): string {
    if (raw == null) return '';
    const s = String(raw).trim();
    if (!s) return '';
    let m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
    if (m) return `${m[1]}-${_p2(m[2])}-${_p2(m[3])}`;
    m = s.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})/);   // day-first customs forms
    if (m) {
      let d = Number(m[1]), mo = Number(m[2]);
      if (mo > 12 && d <= 12) { const t = d; d = mo; mo = t; }
      if (mo < 1 || mo > 12 || d < 1 || d > 31) return '';
      return `${m[3]}-${_p2(mo)}-${_p2(d)}`;
    }
    m = s.match(/^(\d{1,2})[\s-]([A-Za-z]{3,})[\s-](\d{4})/);
    if (m) { const mo = MONTHS[m[2].slice(0, 3).toLowerCase()]; return mo ? `${m[3]}-${_p2(mo)}-${_p2(m[1])}` : ''; }
    m = s.match(/^([A-Za-z]{3,})[\s-](\d{1,2}),?[\s-](\d{4})/);
    if (m) { const mo = MONTHS[m[1].slice(0, 3).toLowerCase()]; return mo ? `${m[3]}-${_p2(mo)}-${_p2(m[2])}` : ''; }
    return '';
  }
  /** True unless the value parses AND falls outside the range. Unparseable/blank stays. */
  function inDateRange(raw: any, from: string, to: string): boolean {
    if (!from && !to) return true;
    const d = normDate(raw);
    if (!d) return true;
    if (from && d < from) return false;
    if (to && d > to) return false;
    return true;
  }

  const todayISO = () => new Date().toISOString().slice(0, 10);
  function daysAgoISO(n: number) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }
  function setRange(days: number) {
    const from = days === 0 ? todayISO() : daysAgoISO(days);
    const to = todayISO();
    if (dateFrom === from && dateTo === to) { dateFrom = ''; dateTo = ''; }
    else { dateFrom = from; dateTo = to; }
  }
  const rangeActive = (days: number) =>
    dateFrom === (days === 0 ? todayISO() : daysAgoISO(days)) && dateTo === todayISO();

  const blank = (v: any) => v == null || String(v).trim() === '';
  const noTotal = (d: any) => blank(d.total_customs_value);
  const noInvoice = (d: any) =>
    blank(d.invoice_number) && blank(d.invoice_number_customs_declaration) && blank(d.invoice_number_commercial_invoice);
  const noTaxes = (d: any) =>
    [d.import_export_customs_duty, d.commercial_tax_ct, d.advance_income_tax_at,
     d.security_fee_sf, d.maccs_service_fee_mf].every(blank);

  // Dropdown values come from the data, never a hardcoded list.
  const allImporters = $derived(
    [...new Set(declarations.map(d => d.importer_name).filter(Boolean))].sort()
  );
  const allCurrencies = $derived(
    [...new Set(declarations.flatMap(d => [d.currency, d.currency_2]).filter(Boolean))].sort()
  );

  const anyFilter = $derived(
    !!(searchQuery || dateFrom || dateTo || selectedImporter || selectedCurrency || selectedStatus)
  );

  const filteredDeclarations = $derived(() => {
    // Structural filters run on the RAW rows — the display map below turns nulls
    // into "—", which would make every "missing X" test read as present.
    let raw = declarations;
    if (selectedImporter) raw = raw.filter(d => d.importer_name === selectedImporter);
    if (selectedCurrency) {
      raw = raw.filter(d => d.currency === selectedCurrency || d.currency_2 === selectedCurrency);
    }
    if (selectedStatus === 'complete') {
      raw = raw.filter(d => !noTotal(d) && !noInvoice(d) && !noTaxes(d));
    } else if (selectedStatus === 'no_total') {
      raw = raw.filter(noTotal);
    } else if (selectedStatus === 'no_invoice') {
      raw = raw.filter(noInvoice);
    } else if (selectedStatus === 'no_taxes') {
      raw = raw.filter(noTaxes);
    }
    if (dateFrom || dateTo) {
      raw = raw.filter(d => inDateRange(d[dateField], dateFrom, dateTo));
    }

    let result = raw.map(d => ({
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
      const q = searchQuery.toLowerCase().trim();
      result = result.filter(d =>
        Object.values(d).some(v => v != null && String(v).toLowerCase().includes(q))
      );
    }
    return result;
  });

  const declRows = $derived(filteredDeclarations());

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
    dateField = 'declaration_date';
    selectedImporter = '';
    selectedCurrency = '';
    selectedStatus = '';
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
<div class="fbar">
  <div class="fsearch">
    <label class="cl-lbl" for="decl-search">Search</label>
    <div class="fsearch-in">
      <span class="material-symbols-outlined fsearch-ic" aria-hidden="true">search</span>
      <input id="decl-search" type="search" class="cl-inp"
             placeholder="Declaration no, importer, invoice no — or any column..."
             bind:value={searchQuery} />
    </div>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-datefield">Date to filter on</label>
    <select id="decl-datefield" bind:value={dateField} class="cl-inp">
      {#each DATE_FIELDS as f}
        <option value={f.key} disabled={!dateFieldsPresent.has(f.key)}>
          {f.label}{dateFieldsPresent.has(f.key) ? '' : ' — not in this data'}
        </option>
      {/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-from">From</label>
    <input id="decl-from" type="date" bind:value={dateFrom} class="cl-inp" />
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-to">To</label>
    <input id="decl-to" type="date" bind:value={dateTo} class="cl-inp" />
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-importer">Importer</label>
    <select id="decl-importer" bind:value={selectedImporter} class="cl-inp">
      <option value="">All importers</option>
      {#each allImporters as imp}<option value={imp}>{imp}</option>{/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-currency">Currency</label>
    <select id="decl-currency" bind:value={selectedCurrency} class="cl-inp">
      <option value="">All currencies</option>
      {#each allCurrencies as c}<option value={c}>{c}</option>{/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="decl-status">Status</label>
    <select id="decl-status" bind:value={selectedStatus} class="cl-inp">
      <option value="">All records</option>
      <option value="complete">Complete</option>
      <option value="no_total">Missing customs value</option>
      <option value="no_invoice">Missing invoice number</option>
      <option value="no_taxes">Missing all taxes</option>
    </select>
  </div>
  <div class="fend">
    <button class="cl-btn sm" onclick={clearFilters} disabled={!anyFilter}>Reset</button>
    <span class="fcount" aria-live="polite">showing {declRows.length} of {declarations.length}</span>
    <button class="cl-btn sm" onclick={downloadExcel}>
      <span class="inline-flex items-center gap-1">
        <span class="material-symbols-outlined text-xs">download</span> Download XLSX
      </span>
    </button>
  </div>
</div>

<!-- Quick filters -->
<div class="fchips" role="group" aria-label="Quick filters">
  <button class="fchip" aria-pressed={rangeActive(7)} onclick={() => setRange(7)}>Last 7 days</button>
  <button class="fchip" aria-pressed={rangeActive(30)} onclick={() => setRange(30)}>Last 30 days</button>
  <button class="fchip" aria-pressed={selectedStatus === 'no_total'}
          onclick={() => selectedStatus = selectedStatus === 'no_total' ? '' : 'no_total'}>Missing customs value</button>
  <button class="fchip" aria-pressed={selectedStatus === 'no_taxes'}
          onclick={() => selectedStatus = selectedStatus === 'no_taxes' ? '' : 'no_taxes'}>Missing all taxes</button>
  <button class="fchip" aria-pressed={selectedStatus === 'no_invoice'}
          onclick={() => selectedStatus = selectedStatus === 'no_invoice' ? '' : 'no_invoice'}>Missing invoice number</button>
  <button class="fchip" aria-pressed={selectedCurrency === 'USD'}
          onclick={() => selectedCurrency = selectedCurrency === 'USD' ? '' : 'USD'}
          hidden={!allCurrencies.includes('USD')}>USD only</button>
</div>

{#if loading}
  <div class="skeleton h-64 w-full"></div>
{:else}
  {@const rows = declRows}
  <div class="cl-panel">
    <div class="cl-hd">
      <span class="dot">◉</span>Customs Declarations
      <span class="ct">showing {rows.length} of {declarations.length} records</span>
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
              <td colspan={columns.length} class="fempty">
                {#if declarations.length === 0}
                  <div class="fempty-t">No declarations yet</div>
                  <div class="fempty-s">Extract a customs document and its declaration lands here.</div>
                {:else}
                  <div class="fempty-t">No declarations match these filters</div>
                  <div class="fempty-s">{declarations.length} records are loaded — try a wider date range or a different date field.</div>
                  <button class="cl-btn sm" onclick={clearFilters}>Reset filters</button>
                {/if}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  </div>
{/if}

<style>
  /* Filter bar — shared shape across History / Declarations / Items.
     Colours come from tokens only; the older names are fallbacks so the bar
     stays readable while app.css is being re-tokenised. */
  .fbar {
    display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px 12px;
    padding: 12px; margin-bottom: 8px;
    background: var(--surface, #fff);
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .fsearch { flex: 1 1 240px; min-width: 190px; }
  .ffield { display: flex; flex-direction: column; }
  .ffield .cl-inp { min-width: 132px; }
  .fsearch-in { position: relative; }
  .fsearch-ic {
    position: absolute; inset-inline-start: 8px; top: 50%; transform: translateY(-50%);
    font-size: 16px; pointer-events: none;
    color: var(--ink-4, var(--on-surface-subtle));
  }
  .fsearch-in input { padding-inline-start: 28px; }
  .fend { display: flex; align-items: center; gap: 10px; margin-inline-start: auto; }
  .fcount {
    font-size: 11px; font-weight: 500; white-space: nowrap;
    color: var(--ink-3, var(--on-surface-muted));
  }
  .fbar button:disabled { opacity: 0.45; cursor: default; }

  .fchips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
  .fchip {
    font-size: 11px; font-weight: 500; line-height: 1;
    padding: 5px 10px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--line);
    background: var(--surface, #fff);
    color: var(--ink-3, var(--on-surface-muted));
    transition: background 150ms ease, color 150ms ease, border-color 150ms ease;
  }
  .fchip:hover {
    background: var(--hover, var(--sunk, var(--surface-container-low)));
    color: var(--ink-2, var(--on-surface));
  }
  .fchip[aria-pressed='true'] {
    background: var(--accent-weak, var(--primary-soft));
    color: var(--accent, var(--primary));
    border-color: var(--accent, var(--primary));
  }
  .fchip:focus-visible { outline: 2px solid var(--accent, var(--primary)); outline-offset: 1px; }

  .fempty { padding: 40px 16px; text-align: center; }
  .fempty-t { font-size: 13px; font-weight: 600; color: var(--ink-2, var(--on-surface)); }
  .fempty-s {
    font-size: 12px; margin: 4px 0 12px;
    color: var(--ink-3, var(--on-surface-muted));
  }
</style>
