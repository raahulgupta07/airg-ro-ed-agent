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
  let hsChapter = $state('');
  let originCountry = $state('');
  let selectedCurrency = $state('');
  let sortBy = $state('');   // '' keeps the order the API returned
  let gapFilter = $state(''); // '' | 'no_hs' | 'no_value' — quick "what's missing" chips

  // An item row carries no declaration number or currency — both live on the
  // declaration. Load them once and key by job_id. Fail-safe: no map, no match.
  let declByJob = $state<Record<string, { no: string; currency: string }>>({});

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

  /** HS chapter = the first two digits of the HS code. '' when there is no code. */
  const chapterOf = (i: any) => {
    const m = String(i.hs_code ?? '').replace(/\D/g, '');
    return m.length >= 2 ? m.slice(0, 2) : '';
  };
  /** Quantities are free text ("120 CTN") — take the leading number, else NaN. */
  const qtyNum = (v: any) => {
    const n = parseFloat(String(v ?? '').replace(/,/g, '').match(/-?\d+(\.\d+)?/)?.[0] ?? '');
    return Number.isFinite(n) ? n : NaN;
  };
  const currencyOf = (i: any) => declByJob[i.job_id]?.currency ?? '';
  const declNoOf = (i: any) => declByJob[i.job_id]?.no ?? '';

  // Dropdown values come from the data, never a hardcoded list.
  const allChapters = $derived([...new Set(items.map(chapterOf).filter(Boolean))].sort());
  const allOrigins = $derived([...new Set(items.map(i => i.origin_country).filter(Boolean))].sort());
  const allCurrencies = $derived([...new Set(items.map(currencyOf).filter(Boolean))].sort());

  const anyFilter = $derived(
    !!(searchQuery || dateFrom || dateTo || hsChapter || originCountry || selectedCurrency || sortBy || gapFilter)
  );

  const filteredItems = $derived(() => {
    let result = items;
    if (searchQuery) {
      const q = searchQuery.toLowerCase().trim();
      result = result.filter(i =>
        String(i.item_name ?? '').toLowerCase().includes(q) ||
        String(i.hs_code ?? '').toLowerCase().includes(q) ||
        declNoOf(i).toLowerCase().includes(q) ||
        Object.values(i).some(v => v != null && String(v).toLowerCase().includes(q))
      );
    }
    if (hsChapter) result = result.filter(i => chapterOf(i) === hsChapter);
    if (originCountry) result = result.filter(i => i.origin_country === originCountry);
    if (selectedCurrency) result = result.filter(i => currencyOf(i) === selectedCurrency);
    if (gapFilter === 'no_hs') result = result.filter(i => String(i.hs_code ?? '').trim() === '');
    if (gapFilter === 'no_value') result = result.filter(i => i.customs_value_mmk == null);
    if (dateFrom || dateTo) result = result.filter(i => inDateRange(i.created_at, dateFrom, dateTo));

    if (sortBy) {
      // Rows with no sortable value sink to the bottom rather than being dropped.
      const num = (v: any) => (typeof v === 'number' && Number.isFinite(v) ? v : NaN);
      const cmpNum = (a: number, b: number, desc: boolean) => {
        const an = Number.isNaN(a), bn = Number.isNaN(b);
        if (an && bn) return 0;
        if (an) return 1;
        if (bn) return -1;
        return desc ? b - a : a - b;
      };
      result = [...result].sort((x, y) => {
        if (sortBy === 'value_desc') return cmpNum(num(x.customs_value_mmk), num(y.customs_value_mmk), true);
        if (sortBy === 'value_asc') return cmpNum(num(x.customs_value_mmk), num(y.customs_value_mmk), false);
        if (sortBy === 'qty_desc') return cmpNum(qtyNum(x.quantity), qtyNum(y.quantity), true);
        if (sortBy === 'name') return String(x.item_name ?? '').localeCompare(String(y.item_name ?? ''));
        return 0;
      });
    }
    return result;
  });

  const itemRows = $derived(filteredItems());

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
    hsChapter = '';
    originCountry = '';
    selectedCurrency = '';
    sortBy = '';
    gapFilter = '';
  }

  onMount(async () => {
    try { items = await api.listItems(); } catch {}
    loading = false;
    // Background: declaration no + currency per job. Never blocks the table.
    api.listDeclarations().then(ds => {
      const m: Record<string, { no: string; currency: string }> = {};
      for (const d of ds ?? []) {
        if (d?.job_id) m[d.job_id] = { no: String(d.declaration_no ?? ''), currency: String(d.currency ?? '') };
      }
      declByJob = m;
    }).catch(() => {});
  });
</script>


<ChapterHeading
  icon="inventory_2"
  title="PRODUCT_ITEMS"
  subtitle="Consolidated product items across all extraction jobs"
  question="What products have been imported?"
/>

<!-- Filters -->
<div class="fbar">
  <div class="fsearch">
    <label class="cl-lbl" for="item-search">Search</label>
    <div class="fsearch-in">
      <span class="material-symbols-outlined fsearch-ic" aria-hidden="true">search</span>
      <input id="item-search" type="search" class="cl-inp"
             placeholder="Product name, HS code, declaration — or any column..."
             bind:value={searchQuery} />
    </div>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-from">From</label>
    <input id="item-from" type="date" bind:value={dateFrom} class="cl-inp" />
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-to">To</label>
    <input id="item-to" type="date" bind:value={dateTo} class="cl-inp" />
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-hs">HS chapter</label>
    <select id="item-hs" bind:value={hsChapter} class="cl-inp">
      <option value="">All chapters</option>
      {#each allChapters as ch}<option value={ch}>Chapter {ch}</option>{/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-origin">Origin</label>
    <select id="item-origin" bind:value={originCountry} class="cl-inp">
      <option value="">All origins</option>
      {#each allOrigins as o}<option value={o}>{o}</option>{/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-currency">Currency</label>
    <select id="item-currency" bind:value={selectedCurrency} class="cl-inp"
            disabled={allCurrencies.length === 0}>
      <option value="">All currencies</option>
      {#each allCurrencies as c}<option value={c}>{c}</option>{/each}
    </select>
  </div>
  <div class="ffield">
    <label class="cl-lbl" for="item-sort">Sort</label>
    <select id="item-sort" bind:value={sortBy} class="cl-inp">
      <option value="">Default order</option>
      <option value="value_desc">Customs value, high to low</option>
      <option value="value_asc">Customs value, low to high</option>
      <option value="qty_desc">Quantity, high to low</option>
      <option value="name">Product name, A to Z</option>
    </select>
  </div>
  <div class="fend">
    <button class="cl-btn sm" onclick={clearFilters} disabled={!anyFilter}>Reset</button>
    <span class="fcount" aria-live="polite">showing {itemRows.length} of {items.length}</span>
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
  <button class="fchip" aria-pressed={sortBy === 'value_desc'}
          onclick={() => sortBy = sortBy === 'value_desc' ? '' : 'value_desc'}>Highest value first</button>
  <button class="fchip" aria-pressed={gapFilter === 'no_hs'}
          onclick={() => gapFilter = gapFilter === 'no_hs' ? '' : 'no_hs'}>Missing HS code</button>
  <button class="fchip" aria-pressed={gapFilter === 'no_value'}
          onclick={() => gapFilter = gapFilter === 'no_value' ? '' : 'no_value'}>Missing customs value</button>
</div>

{#if loading}
  <div class="skeleton h-64 w-full"></div>
{:else}
  {@const rows = itemRows}
  <div class="cl-panel">
    <div class="cl-hd">
      <span class="dot">◉</span>Product Items
      <span class="ct">showing {rows.length} of {items.length} records</span>
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
                {#if items.length === 0}
                  <div class="fempty-t">No product items yet</div>
                  <div class="fempty-s">Extract a customs document and its line items land here.</div>
                {:else}
                  <div class="fempty-t">No items match these filters</div>
                  <div class="fempty-s">{items.length} items are loaded — widen the dates or clear the search.</div>
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
