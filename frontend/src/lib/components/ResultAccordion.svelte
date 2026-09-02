<script lang="ts">
  import AgentTerminal from './AgentTerminal.svelte';
  import PipelineVisualizer from './PipelineVisualizer.svelte';
  import KpiCard from './KpiCard.svelte';
  import DataTable from './DataTable.svelte';
  import Badge from './Badge.svelte';
  import Button from './Button.svelte';
  import PipelineTerminal from './PipelineTerminal.svelte';
  import PageMap from './PageMap.svelte';
  import PageDetail from './PageDetail.svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';
  import { getAccuracyColor, decisionColors, getPageTypeColor } from '$lib/colors';
  import { pipeLabel } from '$lib/utils/pipelineLabels';

  let {
    job = null as any,
    defaultOpen = false,
    pipelineSteps = [] as any[],
    pipelineCollapsed = $bindable(true),
    agentLines = [] as { text: string; type: string }[],
    agentSummary = null as any,
    vizSteps = [] as any[],
    vizSummary = null as any,
    data = null as any,
  } = $props();

  // V10/V11 fields — fall back to job if `data` not explicitly passed
  const v10 = $derived(data ?? job ?? null);

  // Doc-type header card info (computed)
  const docTypeInfo = $derived.by(() => {
    const d = data ?? job ?? {};
    const cls = d.page_classification ?? {};
    const s = cls.summary ?? {};
    const typed = s.TYPED ?? 0;
    const hw = s.HANDWRITTEN ?? 0;
    const att = s.ATTACHMENT ?? 0;
    const cost = d.cost ?? d.cost_usd ?? 0;
    const tokens_in = d.tokens_in ?? 0;
    const tokens_out = d.tokens_out ?? 0;
    const mode = d.mode ?? '';

    let label = 'Unknown';
    let icon = '❓';
    let color = 'zinc';
    let desc = '';
    if (hw === 0 && typed > 0) {
      label = 'Typed MACCS';
      icon = '📝';
      color = 'blue';
      desc = `${typed} typed page${typed > 1 ? 's' : ''}`;
    } else if (typed === 0 && hw > 0) {
      label = 'Handwritten CUSDEC-1';
      icon = '✍️';
      color = 'purple';
      desc = `${hw} handwritten page${hw > 1 ? 's' : ''}`;
    } else if (typed > 0 && hw > 0) {
      label = 'Mixed Document';
      icon = '🔀';
      color = 'amber';
      desc = `${typed} typed + ${hw} handwritten`;
    } else if (d.document_format) {
      label = d.document_format;
      icon = '📄';
      color = 'zinc';
    }
    if (att > 0) desc += `${desc ? ' + ' : ''}${att} attachment${att > 1 ? 's' : ''}`;

    return { label, icon, color, desc, cost, tokens_in, tokens_out, mode,
             total_tokens: tokens_in + tokens_out };
  });

  // ── Tabs ──
  let activeTab = $state<'results' | 'pagemap' | 'annotated' | 'log'>('results');
  let pdfMode = $state<'annotated' | 'original'>('annotated');

  // ── PDF viewer ──
  const pdfUrl = $derived(job?.job_id ? `/api/jobs/${job.job_id}/pdf?token=${auth.token}` : '');

  // ── Marked PDF ────────────────────────────────────────────────────────
  // This tab pointed an iframe straight at the marked-PDF route regardless of
  // whether the job had any coordinates. On a scanned declaration the route
  // correctly answers 404, and the browser rendered that JSON body inside the
  // viewer — a reviewer saw an error where the document should be and read it
  // as the pipeline having failed. Count first, and say why when there is
  // nothing.
  // The count is the server's. Counting `field_bboxes` here would overstate it:
  // a stored box whose value is gone is not a highlight in the file.
  let markCount = $state(0);
  let noMarksReason = $state('');
  $effect(() => {
    const id = job?.job_id;
    if (!id) { markCount = 0; noMarksReason = ''; return; }
    let cancelled = false;
    api.markedPdfStatus(id)
      .then((s) => {
        if (cancelled) return;
        markCount = s?.marks ?? 0;
        noMarksReason = s?.reason || '';
      })
      .catch(() => { if (!cancelled) { markCount = 0; noMarksReason = ''; } });
    return () => { cancelled = true; };
  });
  // Falling back to the original when nothing can be marked would be the one
  // thing this must never do: an unmarked file presented as the marked one.
  const markedSrc = $derived(
    job?.job_id && markCount > 0 ? api.markedPdfUrl(job.job_id) : '');

  // ── Confidence ──
  let confidence = $state<any>(null);
  let confidenceLoaded = $state(false);

  // ── Audit trail (corrections) ──
  let auditLog = $state<any[]>([]);

  async function loadAudit() {
    if (!job?.job_id) return;
    try {
      const res = await fetch(`/api/corrections/job/${job.job_id}`, {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (res.ok) auditLog = await res.text().then(t => JSON.parse(t));
    } catch {}
  }

  // ── Inline editing ──
  let editingCell = $state<{table: string, field: string, idx: number, value: string} | null>(null);
  let editValue = $state('');
  let editSaving = $state(false);

  function startEdit(table: string, field: string, idx: number, currentValue: any) {
    editingCell = { table, field, idx, value: String(currentValue ?? '') };
    editValue = String(currentValue ?? '');
  }

  async function saveEdit() {
    if (!editingCell || !job?.job_id) return;
    editSaving = true;
    try {
      await api.submitCorrection({
        job_id: job.job_id,
        table_key: editingCell.table === 'declaration' ? 'declaration' : 'product_items',
        field_key: editingCell.field,
        item_index: editingCell.table === 'items' ? editingCell.idx : null,
        original_value: editingCell.value,
        corrected_value: editValue,
      });
      // Reload confidence + audit after correction
      confidenceLoaded = false;
      loadConfidence();
      loadAudit();
    } catch (e) {
      console.error('Correction failed:', e);
    }
    editSaving = false;
    editingCell = null;
  }

  function cancelEdit() { editingCell = null; }

  function isEditing(table: string, field: string, idx: number): boolean {
    return editingCell?.table === table && editingCell?.field === field && editingCell?.idx === idx;
  }

  async function loadConfidence() {
    if (confidenceLoaded || !job?.job_id) return;
    try {
      confidence = await api.getJobConfidence(job.job_id);
      confidenceLoaded = true;
    } catch { confidence = null; }
  }

  // Auto-load confidence + audit + page data when job is available
  $effect(() => {
    if (job?.job_id && !confidenceLoaded) { loadConfidence(); loadAudit(); loadPageData(); }
  });

  // ── Field search across all pages (debounced) ──
  let fieldSearch = $state('');
  let fieldSearchQ = $state('');
  let _fsTimer: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    const v = fieldSearch;
    if (_fsTimer) clearTimeout(_fsTimer);
    _fsTimer = setTimeout(() => { fieldSearchQ = v; }, 250);
  });

  const fieldSearchResults = $derived(() => {
    if (!fieldSearchQ.trim() || pageData.length === 0) return [];
    const q = fieldSearchQ.toLowerCase().trim();
    const results: { page: number; pageType: string; source: string; field: string; value: string }[] = [];
    for (const pg of pageData) {
      const pn = pg.page_number;
      const pt = pg.page_type || 'unknown';
      if (pg.fields) {
        for (const [k, v] of Object.entries(pg.fields)) {
          if (k.toLowerCase().includes(q) || String(v).toLowerCase().includes(q)) {
            results.push({ page: pn, pageType: pt, source: 'field', field: k, value: String(v) });
          }
        }
      }
      if (pg.amounts && Array.isArray(pg.amounts)) {
        for (const amt of pg.amounts) {
          const label = amt.label || '';
          const val = `${amt.value ?? ''} ${amt.currency ?? ''}`.trim();
          if (label.toLowerCase().includes(q) || val.toLowerCase().includes(q)) {
            results.push({ page: pn, pageType: pt, source: 'amount', field: label, value: val });
          }
        }
      }
      for (const metaKey of ['doc_title', 'doc_issuer', 'doc_date', 'doc_reference', 'doc_country', 'explanation']) {
        const val = pg[metaKey];
        if (val && (metaKey.toLowerCase().includes(q) || String(val).toLowerCase().includes(q))) {
          results.push({ page: pn, pageType: pt, source: 'meta', field: metaKey.replace('doc_', ''), value: String(val) });
        }
      }
    }
    return results;
  });

  // Page type groups
  const pageTypeGroups = $derived(() => {
    const g: Record<string, number> = {};
    pageData.forEach(p => { const t = p.page_type || 'other'; g[t] = (g[t] || 0) + 1; });
    return Object.entries(g).sort((a, b) => b[1] - a[1]);
  });

  function confDot(level: string): string {
    if (level === 'high') return 'var(--success)';
    if (level === 'medium') return 'var(--warning)';
    return 'var(--error)';
  }

  // ── Page map (v2) ──
  let pageData = $state<any[]>([]);
  let pageDataLoaded = $state(false);
  let selectedPage = $state<any>(null);
  let selectedMapPage = $state<any>(null);

  async function loadPageData() {
    if (pageDataLoaded || !job?.job_id) return;
    try {
      pageData = await api.getJobPages(job.job_id);
      pageDataLoaded = true;
    } catch { pageData = []; }
  }

  function selectPage(page: any) { selectedPage = page; }

  function prevPage() {
    if (!selectedPage || !pageData.length) return;
    const idx = pageData.findIndex((p: any) => p.page_number === selectedPage.page_number);
    if (idx > 0) selectedPage = pageData[idx - 1];
  }

  function nextPage() {
    if (!selectedPage || !pageData.length) return;
    const idx = pageData.findIndex((p: any) => p.page_number === selectedPage.page_number);
    if (idx < pageData.length - 1) selectedPage = pageData[idx + 1];
  }

  const hasPipelineData = $derived(pipelineSteps.length > 0 || (job?.logs?.length > 0));

  const terminalStepsResolved = $derived(() => {
    if (pipelineSteps.length > 0) return pipelineSteps;
    if (job?.logs?.length > 0) {
      return job.logs.map((l: any) => ({
        step: l.step_number,
        name: l.step_name?.toUpperCase().replace(/ /g, '_') || `STEP_${l.step_number}`,
        status: l.status || 'done',
        duration: l.duration_seconds || 0,
        cost: 0,
        lines: l.message ? [{ text: l.message, type: 'detail' }] : [],
      }));
    }
    return [];
  });

  const terminalSummary = $derived({
    items: job?.items?.length || 0,
    accuracy: job?.accuracy_percent || 0,
    decision: (job?.accuracy_percent || 0) >= 90 ? 'ACCEPTED' : (job?.accuracy_percent || 0) >= 60 ? 'FIXED' : 'ESCALATED',
    duration: job?.processing_time_seconds || 0,
    cost: job?.cost_usd || 0,
    pipelineMode: 'ro_ed',
    crossValidation: job?.cross_validation || null,
  });

  let open = $state(defaultOpen);

  const accuracy = $derived(job?.accuracy_percent ?? 0);
  const items = $derived(job?.items ?? []);
  const decl = $derived(job?.declarations?.[0] ?? null);

  // ── New status fields (from backend) ──
  const documentFormat = $derived<string | null>(decl?.document_format ?? job?.document_format ?? null);
  const crossValPassed = $derived<boolean>(
    (decl?.cross_val_passed ?? job?.cross_val_passed ?? true) !== false
  );
  const verifiedFlag = $derived<boolean>(
    (decl?.verified ?? job?.verified ?? true) !== false
  );
  const sanityFlags = $derived<string[]>(() => {
    const raw = decl?.sanity_flags_json ?? job?.sanity_flags_json ?? null;
    if (!raw) return [];
    if (Array.isArray(raw)) return raw as string[];
    if (typeof raw === 'string') {
      try { const parsed = JSON.parse(raw); return Array.isArray(parsed) ? parsed : []; }
      catch { return []; }
    }
    return [];
  });
  let sanityOpen = $state(false);

  const decision = $derived(
    accuracy >= 90 ? 'ACCEPTED' : accuracy >= 60 ? 'FIXED' : accuracy >= 30 ? 'RETRY' : 'ESCALATED'
  );

  const itemColumns = [
    { key: 'item_name', label: 'Item Name' },
    { key: 'customs_duty_rate', label: 'Duty', align: 'right' as const },
    { key: 'quantity', label: 'Quantity' },
    { key: 'invoice_unit_price', label: 'Price' },
    { key: 'commercial_tax_percent', label: 'Tax %', align: 'right' as const },
    { key: 'exchange_rate', label: 'FX Rate' },
  ];

  const declColumns = [
    { key: 'declaration_no', label: 'Declaration No' },
    { key: 'declaration_date', label: 'Date' },
    { key: 'importer_name', label: 'Importer' },
    { key: 'consignor_name', label: 'Consignor' },
    { key: 'currency', label: 'Currency' },
    { key: 'freight_value_fmt', label: 'Freight', align: 'right' as const },
    { key: 'insurance_value_fmt', label: 'Insurance', align: 'right' as const },
    { key: 'adjustment_value_fmt', label: 'Adjustment', align: 'right' as const },
    { key: 'total_customs_value_fmt', label: 'Customs Value', align: 'right' as const },
    { key: 'import_export_customs_duty_fmt', label: 'Duty', align: 'right' as const },
    { key: 'commercial_tax_ct_fmt', label: 'Tax', align: 'right' as const },
    { key: 'advance_income_tax_at_fmt', label: 'Income Tax', align: 'right' as const },
    { key: 'security_fee_sf_fmt', label: 'Security', align: 'right' as const },
    { key: 'maccs_service_fee_mf_fmt', label: 'MACCS', align: 'right' as const },
  ];

  const declTableRows = $derived(() => {
    return (job?.declarations ?? []).map((d: any) => ({
      ...d,
      freight_value_fmt: d.freight_value?.toLocaleString() ?? '—',
      insurance_value_fmt: d.insurance_value?.toLocaleString() ?? '—',
      adjustment_value_fmt: d.adjustment_value?.toLocaleString() ?? '—',
      total_customs_value_fmt: d.total_customs_value?.toLocaleString() ?? '—',
      import_export_customs_duty_fmt: d.import_export_customs_duty?.toLocaleString() ?? '—',
      commercial_tax_ct_fmt: d.commercial_tax_ct?.toLocaleString() ?? '—',
      advance_income_tax_at_fmt: d.advance_income_tax_at?.toLocaleString() ?? '—',
      security_fee_sf_fmt: d.security_fee_sf?.toLocaleString() ?? '—',
      maccs_service_fee_mf_fmt: d.maccs_service_fee_mf?.toLocaleString() ?? '—',
    }));
  });

  async function downloadExcel() {
    if (!job?.job_id) return;
    try {
      const res = await fetch(`/api/jobs/${job.job_id}/download`, {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${job.pdf_name?.replace('.pdf', '') || job.job_id}_extracted.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {}
  }
</script>

{#if job}
  {#if docTypeInfo.label !== 'Unknown' || docTypeInfo.cost > 0}
    <div class="rounded-lg border-2 p-4 mb-4
      {docTypeInfo.color === 'blue' ? 'border-blue-500 bg-blue-50 dark:bg-blue-950/30' :
       docTypeInfo.color === 'purple' ? 'border-purple-500 bg-purple-50 dark:bg-purple-950/30' :
       docTypeInfo.color === 'amber' ? 'border-amber-500 bg-amber-50 dark:bg-amber-950/30' :
       'border-zinc-300 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-900'}">
      <div class="flex items-center gap-3 flex-wrap">
        <span class="text-3xl">{docTypeInfo.icon}</span>
        <div class="flex-1">
          <div class="font-bold text-lg">{docTypeInfo.label}</div>
          {#if docTypeInfo.desc}
            <div class="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">{docTypeInfo.desc}</div>
          {/if}
        </div>
        <div class="flex gap-4 text-sm">
          {#if docTypeInfo.mode}
            <div class="text-center">
              <div class="text-[10px] uppercase text-zinc-500">Pipeline</div>
              <div class="font-mono font-bold">{docTypeInfo.mode.toUpperCase()}</div>
            </div>
          {/if}
          {#if docTypeInfo.cost}
            <div class="text-center">
              <div class="text-[10px] uppercase text-zinc-500">Cost</div>
              <div class="font-mono font-bold">${Number(docTypeInfo.cost).toFixed(4)}</div>
            </div>
          {/if}
          {#if docTypeInfo.total_tokens}
            <div class="text-center">
              <div class="text-[10px] uppercase text-zinc-500">Tokens</div>
              <div class="font-mono font-bold">
                {(docTypeInfo.tokens_in / 1000).toFixed(1)}K / {(docTypeInfo.tokens_out / 1000).toFixed(1)}K
              </div>
            </div>
          {/if}
        </div>
      </div>
    </div>
  {/if}
  {#if v10?.needs_review}
    <div class="bg-amber-100 dark:bg-amber-950/50 border-l-4 border-amber-500 px-4 py-3 mb-4 rounded">
      <div class="flex items-center gap-2">
        <span class="text-amber-600 dark:text-amber-400">⚠️</span>
        <strong class="text-sm">Needs Review</strong>
      </div>
      <div class="text-xs mt-1 text-zinc-600 dark:text-zinc-400">
        Pipeline flagged this extraction — verify critical fields before approval.
      </div>
    </div>
  {/if}
  <div class="border-2 mb-3" style="border-color: var(--line);">
    <!-- Header (clickable) -->
    <button
      class="w-full text-left px-4 py-2.5 flex items-center gap-3 cursor-pointer transition-colors"
      style="background: {open ? 'var(--surface-container)' : 'white'};"
      onclick={() => open = !open}
    >
      <span class="material-symbols-outlined text-sm" style="color: var(--on-surface);">
        {open ? 'expand_more' : 'chevron_right'}
      </span>
      <span class="text-xs font-bold flex-1" style="color: var(--on-surface);">{job.pdf_name}</span>
      <span class="text-[10px] font-mono font-bold" style="color: {getAccuracyColor(accuracy)};">
        {accuracy.toFixed(1)}%
      </span>
      <Badge text={decision} variant={accuracy >= 90 ? 'success' : accuracy >= 60 ? 'warning' : 'critical'} />
      <span class="text-[9px] font-bold uppercase" style="color: var(--outline);">
        {items.length} items
      </span>
    </button>

    <!-- Body (expandable) -->
    {#if open}
      <div class="border-t-2 animate-slideDown" style="border-color: var(--line); background: var(--surface);">
        <!-- Tab bar -->
        <div class="flex gap-0 border-b" style="border-color: rgba(56,56,50,0.15);">
          {#each [['results', 'RESULTS'], ['annotated', 'MARKED PDF'], ['log', 'PIPELINE LOG']] as [key, label]}
            <button class="px-3 py-1.5 text-[10px] font-bold uppercase cursor-pointer"
              style="{activeTab === key ? 'background: var(--surface-container); color: var(--on-surface);' : 'color: var(--outline); background: var(--surface-container);'}"
              onclick={() => { activeTab = key as any; if (key === 'pagemap') loadPageData(); }}>
              {label}
            </button>
          {/each}
          <div class="flex-1"></div>
          <Button variant="secondary" size="sm" onclick={downloadExcel}>
            <span class="flex items-center gap-1">
              <span class="material-symbols-outlined text-xs">download</span> XLSX
            </span>
          </Button>
        </div>

        <div class="p-4 space-y-4">
          {#if activeTab === 'results'}
            <!-- Pipeline Flow Visualizer -->
            {#if vizSteps.length > 0}
              <PipelineVisualizer
                steps={vizSteps}
                filename={job.pdf_name}
                complete={true}
                summary={vizSummary}
              />
            {/if}

            <!-- Status Badges Row -->
            {#if documentFormat || !crossValPassed || !verifiedFlag || sanityFlags().length > 0}
              <div class="border-2 bg-white p-2 space-y-2" style="border-color: var(--line);">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[9px] font-medium uppercase" style="color: var(--on-surface);">STATUS</span>
                  {#if documentFormat === 'CUSDEC1'}
                    <span class="inline-block font-medium uppercase text-white" style="padding: 2px 6px; font-size: 9px; background: var(--info);">
                      OLD FORMAT — CUSDEC-1
                    </span>
                  {:else if documentFormat === 'MACCS'}
                    <Badge text="MACCS" variant="secondary" />
                  {:else if documentFormat}
                    <Badge text={documentFormat} variant="info" />
                  {/if}
                  {#if !crossValPassed}
                    <Badge text="DATA MISMATCH — REVIEW" variant="critical" />
                  {/if}
                  {#if !verifiedFlag}
                    <Badge text="UNVERIFIED" variant="warning" />
                  {/if}
                  {#if sanityFlags().length > 0}
                    <button
                      type="button"
                      class="inline-flex items-center gap-1 cursor-pointer font-medium uppercase text-white"
                      style="padding: 2px 6px; font-size: 9px; background: var(--warning); border: none;"
                      onclick={() => sanityOpen = !sanityOpen}
                    >
                      <span class="material-symbols-outlined" style="font-size: 11px;">
                        {sanityOpen ? 'expand_more' : 'chevron_right'}
                      </span>
                      {sanityFlags().length} SANITY FLAG{sanityFlags().length === 1 ? '' : 'S'}
                    </button>
                  {/if}
                </div>
                {#if sanityOpen && sanityFlags().length > 0}
                  <ul class="text-[10px] font-mono space-y-0.5 pl-2 border-l-2" style="border-color: var(--warning); color: var(--on-surface);">
                    {#each sanityFlags() as flag}
                      <li class="flex items-center gap-1">
                        <span class="material-symbols-outlined" style="font-size: 11px; color: var(--warning);">warning</span>
                        <span>{flag}</span>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            {/if}

            <!-- Pipeline mode badge -->
            {#if v10?.mode}
              <div class="flex items-center gap-2 flex-wrap">
                <span class="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[11px] bg-zinc-200 dark:bg-zinc-800">
                  Pipeline: <strong>{pipeLabel(v10.mode)}</strong>
                  {#if v10.v7_used && v10.v10_used}<span>(Atlas Swift + Vision hybrid)</span>{/if}
                </span>
                {#if v10.v7_used && !v10.v10_used}
                  <span class="inline-flex items-center px-2 py-1 rounded-full text-[11px] bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200">ATLAS SWIFT</span>
                {/if}
                {#if v10.v10_used && !v10.v7_used}
                  <span class="inline-flex items-center px-2 py-1 rounded-full text-[11px] bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">ATLAS VISION</span>
                {/if}
              </div>
            {/if}

            <!-- KPI Row -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
              <KpiCard title="ITEMS" value="{items.length}" icon="inventory_2" accent="var(--success)" />
              <KpiCard title="ACCURACY" value="{accuracy.toFixed(1)}%" progress={accuracy} accent={getAccuracyColor(accuracy)} />
              <KpiCard title="DECISION" value="{decision}" accent={decisionColors[decision] ?? 'var(--success)'} />
              <KpiCard title="TIME" value="{job.processing_time_seconds?.toFixed(1) ?? '—'}s" accent="var(--info)" />
            </div>

            <!-- Cost & Tokens KPI Row -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mt-3">
              <KpiCard title="COST" value="${job.cost_usd?.toFixed(3) ?? '—'}" accent="var(--warning)" />
              <KpiCard title="TOKENS_IN" value="{((job.tokens_in ?? 0) / 1000).toFixed(1)}K" accent="var(--info)"
                       subtitle="{(job.tokens_in ?? 0).toLocaleString()} in" />
              <KpiCard title="TOKENS_OUT" value="{((job.tokens_out ?? 0) / 1000).toFixed(1)}K" accent="var(--tertiary)"
                       subtitle="{(job.tokens_out ?? 0).toLocaleString()} out" />
              <KpiCard title="MODEL" value="{job.model_used || pipeLabel(job.pipeline_mode || job.pipeline_version) || 'V11'}" accent="var(--tertiary)"
                       subtitle="Pipeline: {pipeLabel(job.pipeline_mode || job.pipeline_version) || '—'}" />
            </div>

            <!-- Page Classification (V11) -->
            {#if v10?.page_classification?.pages?.length}
              <details class="my-3 border border-zinc-200 dark:border-zinc-700 rounded">
                <summary class="cursor-pointer px-3 py-2 text-sm font-semibold bg-zinc-50 dark:bg-zinc-900">
                  Page Routing ({v10.page_classification.summary?.TYPED || 0} typed,
                   {v10.page_classification.summary?.HANDWRITTEN || 0} handwritten,
                   {v10.page_classification.summary?.ATTACHMENT || 0} attachments)
                </summary>
                <div class="p-3 space-y-1">
                  {#each v10.page_classification.pages as p (p.page)}
                    <div class="flex items-center gap-2 text-xs">
                      <span class="font-mono text-zinc-500">Page {p.page}:</span>
                      <span class="px-2 py-0.5 rounded
                        {p.label === 'TYPED'
                           ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                           : p.label === 'HANDWRITTEN'
                              ? 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200'
                              : 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-300'}">
                        {p.label}
                      </span>
                      <span class="text-zinc-500 truncate flex-1">{p.reason || ''}</span>
                    </div>
                  {/each}
                </div>
              </details>
            {/if}

            <!-- Pipeline Trace (dev) -->
            {#if v10?.trace?.length}
              <details class="my-3 border border-zinc-200 dark:border-zinc-700 rounded">
                <summary class="cursor-pointer px-3 py-2 text-sm font-semibold bg-zinc-50 dark:bg-zinc-900">
                  Pipeline Trace ({v10.trace.length} phases)
                </summary>
                <pre class="p-3 text-[11px] overflow-x-auto bg-zinc-100 dark:bg-zinc-900">{JSON.stringify(v10.trace, null, 2)}</pre>
              </details>
            {/if}

            <!-- Confidence Summary -->
            {#if confidence?.summary}
              <div class="border-2 bg-white p-3" style="border-color: var(--line);">
                <div class="flex items-center justify-between mb-2">
                  <span class="text-[9px] font-medium uppercase" style="color: var(--on-surface);">FIELD CONFIDENCE</span>
                  <span class="text-[10px] font-bold" style="color: {confidence.summary.average_confidence >= 0.8 ? 'var(--success)' : confidence.summary.average_confidence >= 0.5 ? 'var(--warning)' : 'var(--error)'};">
                    {(confidence.summary.average_confidence * 100).toFixed(0)}% avg
                  </span>
                </div>
                <div class="flex items-center gap-1 h-3 w-full" style="background: var(--surface-container);">
                  {#if confidence.summary.total_fields > 0}
                    <div style="width: {confidence.summary.high / confidence.summary.total_fields * 100}%; height: 100%; background: var(--success);" title="{confidence.summary.high} high confidence"></div>
                    <div style="width: {confidence.summary.medium / confidence.summary.total_fields * 100}%; height: 100%; background: var(--warning);" title="{confidence.summary.medium} medium"></div>
                    <div style="width: {confidence.summary.low / confidence.summary.total_fields * 100}%; height: 100%; background: var(--error);" title="{confidence.summary.low} low"></div>
                  {/if}
                </div>
                <div class="flex gap-4 mt-1.5 text-[9px]" style="color: var(--outline);">
                  <span><span style="display:inline-block;width:6px;height:6px;background:var(--success);margin-right:2px;"></span> {confidence.summary.high} high</span>
                  <span><span style="display:inline-block;width:6px;height:6px;background:var(--warning);margin-right:2px;"></span> {confidence.summary.medium} medium</span>
                  <span><span style="display:inline-block;width:6px;height:6px;background:var(--error);margin-right:2px;"></span> {confidence.summary.low} low</span>
                  <span style="color: var(--on-surface); font-weight: bold;">{confidence.summary.total_fields} total fields</span>
                </div>
              </div>
            {/if}

            <!-- Cross-validation removed — not in current pipeline -->

            <!-- Quick insights -->
            <div class="border-2 bg-white" style="border-color: var(--line);">
              <div class="dark-bar text-xs">DOCUMENT_INSIGHTS</div>
              <div class="grid grid-cols-2 md:grid-cols-4 gap-0">
                {#if true}{@const dc = confidence?.declaration || {}}
                <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">IMPORTER {#if dc['Importer (Name)']}<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:{confDot(dc['Importer (Name)'].level)};vertical-align:middle;margin-left:3px;"></span>{/if}</div>
                  <div class="text-[11px] font-bold mt-0.5 truncate" style="color: var(--on-surface);">{decl?.importer_name || '—'}</div>
                </div>
                <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">CONSIGNOR {#if dc['Consignor (Name)']}<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:{confDot(dc['Consignor (Name)'].level)};vertical-align:middle;margin-left:3px;"></span>{/if}</div>
                  <div class="text-[11px] font-bold mt-0.5 truncate" style="color: var(--on-surface);">{decl?.consignor_name || '—'}</div>
                </div>
                <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">CUSTOMS VALUE {#if dc['Total Customs Value']}<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:{confDot(dc['Total Customs Value'].level)};vertical-align:middle;margin-left:3px;"></span>{/if}</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{decl?.total_customs_value?.toLocaleString() ?? '—'}</div>
                </div>
                <div class="p-3 border-b" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">INVOICE NO (CUSTOMS DECLARATION)</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{decl?.invoice_number_customs_declaration || '—'}</div>
                  <div class="text-[8px] font-medium uppercase mt-2" style="color: var(--outline);">INVOICE NO (COMMERCIAL INVOICE)</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{decl?.invoice_number_commercial_invoice || decl?.invoice_number || '—'}</div>
                </div>
                <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">INVOICE PRICE {#if dc['Invoice Price']}<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:{confDot(dc['Invoice Price'].level)};vertical-align:middle;margin-left:3px;"></span>{/if}</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{decl?.invoice_price?.toLocaleString() ?? '—'} {decl?.currency || ''}</div>
                </div>
                <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">EXCHANGE RATE</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{decl?.exchange_rate?.toLocaleString() ?? '—'}</div>
                </div>
                <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.15);">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">PAGES</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{job.total_pages ?? '—'}</div>
                </div>
                <div class="p-3">
                  <div class="text-[8px] font-medium uppercase" style="color: var(--outline);">PROCESSED BY</div>
                  <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{job.username || job.model_used || '—'} · {(job.created_at || job.processed_at)?.split('T')[0]?.split(' ')[0] || ''}</div>
                </div>
              {/if}
              </div>
            </div>

            <!-- Items table with confidence, definitions, split columns -->
            {#if items.length > 0}
              {@const ic = confidence?.items || []}
              {@const cols = [
                      {l:'ITEM NAME', ck:'Item name', def:'Full product name with country of origin', pg:'pg1'},
                      {l:'DUTY RATE', ck:'Customs duty rate', def:'Decimal (0.15 = 15%, 0.0 = FREE)', pg:'pg1'},
                      {l:'QTY', ck:'Quantity (1)', def:'Quantity number', pg:'inv'},
                      {l:'UNIT', ck:'', def:'KG, CTN, PCS, L', pg:'inv'},
                      {l:'UNIT PRICE', ck:'Invoice unit price', def:'Price per unit from invoice', pg:'inv'},
                      {l:'CURRENCY', ck:'', def:'Invoice currency (USD/THB/EUR)', pg:'inv'},
                      {l:'TAX %', ck:'Commercial tax %', def:'Decimal (0.05 = 5%)', pg:'pg1'},
                      {l:'FX RATE', ck:'Exchange Rate (1)', def:'Conversion rate to MMK', pg:'pg2'},
                      {l:'HS CODE', ck:'HS Code', def:'Full tariff code (10+ digits)', pg:'pg1'},
                      {l:'ORIGIN', ck:'Origin Country', def:'2-letter ISO code', pg:'pg1'},
                      {l:'CUSTOMS VALUE (MMK)', ck:'Customs Value (MMK)', def:'Value in local currency (MMK)', pg:'pg1'},
                    ]}
              {@const itemConfAvg = confidence?.summary?.average_confidence || 0.9}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <div class="flex items-center gap-2">
                    <span>PRODUCT_ITEMS</span>
                    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{itemConfAvg >= 0.8 ? 'var(--success)' : itemConfAvg >= 0.5 ? 'var(--warning)' : 'var(--error)'};"></span>
                  </div>
                  <span class="px-2 py-0.5" style="background: var(--surface); color: var(--on-surface);">{items.length} RECORDS</span>
                </div>
                <div class="overflow-x-auto bg-white" style="max-height: 400px;">
                  <table class="w-full text-[11px]">
                    <thead>
                      <!-- Row 1: Column headers -->
                      <tr style="background: var(--surface-container); color: var(--on-surface);">
                        {#each cols as col}
                          <th class="px-2 py-1.5 text-left text-[9px] font-medium uppercase">{col.l}</th>
                        {/each}
                      </tr>
                      <!-- Row 2: Definitions -->
                      <tr style="background: var(--surface-container-highest);">
                        {#each cols as col}
                          <td class="px-2 py-1 text-[7px]" style="color: var(--outline);">{col.def}</td>
                        {/each}
                      </tr>
                      <!-- Row 3: Page source -->
                      <tr style="background: var(--surface-container);">
                        {#each cols as col}
                          <td class="px-2 py-0.5 text-[7px] font-bold" style="color: var(--primary);">{col.pg}</td>
                        {/each}
                      </tr>
                      <!-- Row 4: Confidence -->
                      <tr style="background: var(--surface-container); border-bottom: 1px solid var(--on-surface);">
                        {#each cols as col}
                          <td class="px-2 py-0.5 text-[7px]">
                            {#if col.ck && ic[0]?.[col.ck]}
                              <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{confDot(ic[0][col.ck].level)};margin-right:3px;"></span>
                            {:else}
                              <span style="color: var(--outline);">—</span>
                            {/if}
                          </td>
                        {/each}
                      </tr>
                    </thead>
                    <tbody>
                      {#each items as item, idx}
                        {@const qtyStr = String(item.quantity || item.Quantity || '')}
                        {@const qtyNum = qtyStr.replace(/[A-Za-z]/g, '').trim()}
                        {@const qtyUnit = qtyStr.replace(/[\d.,\s]/g, '').trim()}
                        {@const priceStr = String(item.invoice_unit_price || item['Invoice unit price'] || '')}
                        {@const priceNum = priceStr.replace(/[A-Za-z]/g, '').trim()}
                        {@const priceCcy = priceStr.replace(/[\d.,\s]/g, '').trim()}
                        <tr style="border-bottom: 1px solid rgba(56,56,50,0.08);">
                          <!-- Item Name -->
                          <td class="px-2 py-1.5 cursor-pointer" style="font-size: 10px; max-width: 300px;"
                            ondblclick={() => startEdit('items', 'Item name', idx, item.item_name)}
                            title="Double-click to edit">
                            {item.item_name || '—'}
                          </td>
                          <!-- Duty -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {item.customs_duty_rate ?? '—'}
                          </td>
                          <!-- Qty Number -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {qtyNum || '—'}
                          </td>
                          <!-- Qty Unit -->
                          <td class="px-2 py-1.5" style="font-size: 10px; font-weight: bold;">
                            {qtyUnit || '—'}
                          </td>
                          <!-- Price Number -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {priceNum || '—'}
                          </td>
                          <!-- Price Currency (fallback to declaration currency) -->
                          <td class="px-2 py-1.5" style="font-size: 10px; font-weight: bold;">
                            {priceCcy || decl?.currency || '—'}
                          </td>
                          <!-- Tax % -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {item.commercial_tax_percent ?? '—'}
                          </td>
                          <!-- FX Rate -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {item.exchange_rate ?? '—'}
                          </td>
                          <!-- HS Code -->
                          <td class="px-2 py-1.5" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {item.hs_code || '—'}
                          </td>
                          <!-- Origin -->
                          <td class="px-2 py-1.5 text-center" style="font-size: 10px; font-weight: bold;">
                            {item.origin_country || '—'}
                          </td>
                          <!-- Customs Value -->
                          <td class="px-2 py-1.5 text-right" style="font-size: 10px; font-family: 'JetBrains Mono', ui-monospace, monospace;">
                            {typeof item.customs_value_mmk === 'number' ? item.customs_value_mmk?.toLocaleString() : item.customs_value_mmk || '—'}
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              </div>
            {/if}

            <!-- Declaration table with confidence -->
            {#if job.declarations?.length > 0}
              {@const dc = confidence?.declaration || {}}
              {@const declRow = job.declarations[0]}
              {@const dCols = [
                  {k:'declaration_no', l:'DECL NO', ck:'Declaration No', def:'Unique 12-digit ID', pg:'pg1'},
                  {k:'declaration_date', l:'DATE', ck:'Declaration Date', def:'YYYY-MM-DD format', pg:'pg1'},
                  {k:'importer_name', l:'IMPORTER', ck:'Importer (Name)', def:'Buyer name verbatim', pg:'pg1'},
                  {k:'consignor_name', l:'CONSIGNOR', ck:'Consignor (Name)', def:'Seller/shipper verbatim', pg:'pg1'},
                  {k:'invoice_number_customs_declaration', l:'INVOICE NO (CUSTOMS DECLARATION)', ck:'Invoice Number (Customs Declaration)', def:'Customs form value WITH section prefix (e.g. A - AM-PD-001/2025)', pg:'pg1'},
                  {k:'invoice_number_commercial_invoice', l:'INVOICE NO (COMMERCIAL INVOICE)', ck:'Invoice Number (Commercial Invoice)', def:'Bare invoice ID from supplier invoice (no prefix)', pg:'inv'},
                  {k:'invoice_number', l:'INVOICE NO (LEGACY)', ck:'Invoice Number', def:'Back-compat alias (mirrors commercial)', pg:'pg1'},
                  {k:'invoice_price', l:'INV PRICE', ck:'Invoice Price', def:'CIF amount in inv currency', pg:'pg1'},
                  {k:'currency', l:'CURRENCY', ck:'Currency', def:'Invoice currency (not MMK)', pg:'inv'},
                  {k:'exchange_rate', l:'FX RATE', ck:'Exchange Rate', def:'Conversion rate to MMK', pg:'pg2'},
                  {k:'currency_2', l:'CURRENCY 2', ck:'Currency 2', def:'Same as invoice currency', pg:'pg1'},
                  {k:'total_customs_value', l:'CUSTOMS VALUE', ck:'Total Customs Value', def:'Total in MMK (largest)', pg:'pg1'},
                  {k:'import_export_customs_duty', l:'DUTY (CD)', ck:'Import/Export Customs Duty', def:'Import duty amount', pg:'pg1'},
                  {k:'commercial_tax_ct', l:'TAX (CT)', ck:'Commercial Tax (CT)', def:'Commercial tax (exempt=0)', pg:'pg1'},
                  {k:'advance_income_tax_at', l:'INCOME TAX', ck:'Advance Income Tax (AT)', def:'Advance income tax', pg:'pg1'},
                  {k:'security_fee_sf', l:'SECURITY FEE', ck:'Security Fee (SF)', def:'Security fee (optional)', pg:'pg1'},
                  {k:'maccs_service_fee_mf', l:'MACCS FEE', ck:'MACCS Service Fee (MF)', def:'MACCS service fee', pg:'pg1'},
                  {k:'exemption_reduction', l:'EXEMPTION', ck:'Exemption/Reduction', def:'Tax exemption amount', pg:'pg1'},
                  {k:'created_at', l:'PROCESSED', ck:'Processed', def:'Processing timestamp', pg:'—'},
                ]}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <div class="flex items-center gap-2">
                    <span>CUSTOMS_DECLARATION</span>
                    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{( confidence?.summary?.average_confidence || 0.9) >= 0.8 ? 'var(--success)' : (confidence?.summary?.average_confidence || 0.9) >= 0.5 ? 'var(--warning)' : 'var(--error)'};"></span>
                  </div>
                  <span class="px-2 py-0.5" style="background: var(--surface); color: var(--on-surface);">1 RECORD</span>
                </div>
                <div class="overflow-x-auto bg-white">
                  <table class="w-full text-[11px]">
                    <thead>
                      <!-- Row 1: Column headers -->
                      <tr style="background: var(--surface-container); color: var(--on-surface);">
                        {#each dCols as col}
                          <th class="px-2 py-1.5 text-left text-[9px] font-medium uppercase">{col.l}</th>
                        {/each}
                      </tr>
                      <!-- Row 2: Definitions -->
                      <tr style="background: var(--surface-container-highest);">
                        {#each dCols as col}
                          <td class="px-2 py-1 text-[7px]" style="color: var(--outline);">{col.def}</td>
                        {/each}
                      </tr>
                      <!-- Row 3: Page source -->
                      <tr style="background: var(--surface-container);">
                        {#each dCols as col}
                          <td class="px-2 py-0.5 text-[7px] font-bold" style="color: var(--primary);">{col.pg}</td>
                        {/each}
                      </tr>
                      <!-- Row 4: Confidence -->
                      <tr style="background: var(--surface-container); border-bottom: 1px solid var(--on-surface);">
                        {#each dCols as col}
                          <td class="px-2 py-0.5 text-[7px]">
                            {#if dc[col.ck]}
                              <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{confDot(dc[col.ck].level)};margin-right:3px;"></span>
                            {:else}
                              <span style="color: var(--outline);">—</span>
                            {/if}
                          </td>
                        {/each}
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        {#each dCols as col}
                          <td class="px-2 py-1.5 cursor-pointer" style="font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px;"
                            ondblclick={() => startEdit('declaration', col.ck, 0, declRow[col.k])}
                            title="Double-click to edit">
                            {#if isEditing('declaration', col.ck, 0)}
                              <input type="text" bind:value={editValue}
                                class="w-full px-1 py-0.5 border text-[10px] font-mono"
                                style="border-color: var(--primary); outline: none; background: var(--primary-container);"
                                onkeydown={(e) => { if (e.key === 'Enter') saveEdit(); if (e.key === 'Escape') cancelEdit(); }}
                                onblur={saveEdit}
                              />
                            {:else}
                              {typeof declRow[col.k] === 'number' ? declRow[col.k]?.toLocaleString() : declRow[col.k] || '—'}
                            {/if}
                          </td>
                        {/each}
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            {/if}


            <!-- Audit Trail (corrections made on this job) -->
            {#if auditLog.length > 0}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <span>CORRECTIONS_LOG</span>
                  <span>{auditLog.length} corrections (feeds into AI learning)</span>
                </div>
                <div class="overflow-x-auto bg-white">
                  <table style="border-collapse: collapse; font-size: 11px; width: 100%;">
                    <thead>
                      <tr style="background: var(--surface-container); color: var(--on-surface);">
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">TIME</th>
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">TABLE</th>
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">FIELD</th>
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">ORIGINAL</th>
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">CORRECTED</th>
                        <th style="padding: 6px 10px; text-align: left; font-size: 9px; font-weight: 900; white-space: nowrap;">USER</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each auditLog as c}
                        <tr style="border-bottom: 1px solid rgba(56,56,50,0.08);">
                          <td style="padding: 5px 10px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px; white-space: nowrap; color: var(--outline);">{c.created_at?.split(' ')[1]?.slice(0,5) || c.created_at || ''}</td>
                          <td style="padding: 5px 10px; font-size: 9px; font-weight: bold;">{c.table_key}</td>
                          <td style="padding: 5px 10px; font-size: 10px; font-weight: bold; color: var(--secondary);">{c.field_key}</td>
                          <td style="padding: 5px 10px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; text-decoration: line-through; color: var(--error);">{c.original_value || '—'}</td>
                          <td style="padding: 5px 10px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; font-weight: bold; color: var(--success);">{c.corrected_value}</td>
                          <td style="padding: 5px 10px; font-size: 9px; color: var(--outline);">{c.username || '—'}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              </div>
            {/if}

            <!-- Document Summary -->
            {#if pageData.length > 0}
              {@const shipPages = pageData.filter(p => ['delivery_order','bill_of_lading','Delivery Order (D/O)','Bill of Lading'].some(t => (p.page_type||'').toLowerCase().includes(t.toLowerCase())))}
              {@const invPages = pageData.filter(p => (p.page_type||'').toLowerCase().includes('invoice'))}
              {@const insPages = pageData.filter(p => (p.page_type||'').toLowerCase().includes('insurance'))}
              {@const certPages = pageData.filter(p => ['certificate','fda','import_license','import licence'].some(t => (p.page_type||'').toLowerCase().includes(t.toLowerCase())))}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <span>DOCUMENT_SUMMARY</span>
                  <span>{pageData.length} PAGES ANALYZED</span>
                </div>
                <div class="grid grid-cols-2 md:grid-cols-4 gap-0 bg-white">
                  <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">DOCUMENT TYPES</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{pageTypeGroups().length} types across {pageData.length} pages</div>
                  </div>
                  <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">SHIPPING</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{shipPages.length > 0 ? `${shipPages.length} docs` : '—'}</div>
                  </div>
                  <div class="p-3 border-r border-b" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">INVOICES</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{invPages.length > 0 ? `${invPages.length} invoices` : '—'}</div>
                  </div>
                  <div class="p-3 border-b" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">CERTIFICATES</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{certPages.length > 0 ? `${certPages.length} docs` : '—'}</div>
                  </div>
                  <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">INSURANCE</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{insPages.length > 0 ? `${insPages.length} docs` : '—'}</div>
                  </div>
                  <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">AVG CONFIDENCE</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{(pageData.reduce((s, p) => s + (p.confidence || 0), 0) / pageData.length).toFixed(2)}</div>
                  </div>
                  <div class="p-3 border-r" style="border-color: rgba(56,56,50,0.1);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">SKIPPED PAGES</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{pageData.filter(p => (p.page_type||'').includes('blank') || (p.page_type||'').includes('stamps')).length} junk pages</div>
                  </div>
                  <div class="p-3">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">TOTAL FIELDS</div>
                    <div class="text-[11px] font-bold mt-0.5" style="color: var(--on-surface);">{pageData.reduce((s, p) => s + Object.keys(p.fields || {}).length, 0)} extracted</div>
                  </div>
                </div>
              </div>
            {/if}

            <!-- Field Search -->
            {#if pageData.length > 0}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <span>FIELD_SEARCH</span>
                  <span>SEARCH ACROSS ALL {pageData.length} PAGES</span>
                </div>
                <div class="bg-white p-3">
                  <div class="flex gap-2 items-center">
                    <span class="material-symbols-outlined text-sm" style="color: var(--outline);">search</span>
                    <input type="text" placeholder="Search any field or value across all pages..."
                      bind:value={fieldSearch}
                      class="flex-1 text-xs px-3 py-2 focus:outline-none"
                      style="border: 1px solid var(--line); border-radius: var(--radius-sm); background: white;" />
                    {#if fieldSearch}
                      <button class="text-[9px] font-medium uppercase px-2 py-1 cursor-pointer" style="border: 1px solid var(--outline); color: var(--outline);" onclick={() => fieldSearch = ''}>CLEAR</button>
                    {/if}
                  </div>
                  {#if fieldSearch.trim() && fieldSearchResults().length > 0}
                    {@const results = fieldSearchResults()}
                    {@const groupedByPage = results.reduce((acc, r) => { (acc[r.page] = acc[r.page] || []).push(r); return acc; }, {} as Record<number, typeof results>)}
                    <div class="mt-3 flex items-center justify-between mb-2">
                      <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">{results.length} matches across {Object.keys(groupedByPage).length} pages</span>
                      <button class="text-[9px] font-medium uppercase px-2 py-1 cursor-pointer border"
                        style="border-color: var(--line); color: var(--on-surface);"
                        onclick={() => { const text = results.map(r => `Page ${r.page} | ${r.field}: ${r.value}`).join('\n'); navigator.clipboard.writeText(text); }}
                      >COPY ALL</button>
                    </div>
                    <div class="space-y-2 max-h-[300px] overflow-y-auto">
                      {#each Object.entries(groupedByPage).sort((a, b) => Number(a[0]) - Number(b[0])) as [pageNum, matches]}
                        {@const pt = matches[0].pageType}
                        {@const ptc = getPageTypeColor(pt)}
                        <div class="border" style="border-color: rgba(56,56,50,0.15);">
                          <div class="flex items-center gap-2 px-2 py-1.5" style="background: var(--surface-container);">
                            <span class="w-5 h-5 flex items-center justify-center text-[8px] font-bold text-white" style="background: {ptc.bg};">{pageNum}</span>
                            <span class="text-[8px] font-medium uppercase" style="color: {ptc.bg};">{pt.replace(/_/g, ' ')}</span>
                            <span class="text-[8px] font-mono" style="color: var(--outline);">{matches.length} matches</span>
                          </div>
                          <div class="px-2 py-1">
                            {#each matches as match}
                              <div class="flex gap-2 py-0.5 border-b items-center" style="border-color: rgba(56,56,50,0.05);">
                                <span class="text-[7px] font-medium uppercase px-1 py-0.5 flex-shrink-0" style="color: var(--outline); background: var(--surface-container);">{match.source}</span>
                                <span class="text-[10px] font-bold flex-shrink-0" style="color: var(--secondary); min-width: 120px;">{match.field}</span>
                                <span class="text-[10px] font-mono flex-1 select-all" style="color: var(--on-surface);">{match.value}</span>
                                <button class="flex-shrink-0 px-1 py-0.5 text-[8px] font-bold uppercase cursor-pointer border opacity-40 hover:opacity-100"
                                  style="border-color: var(--outline); color: var(--outline);"
                                  onclick={(e) => { navigator.clipboard.writeText(match.value); const btn = e.currentTarget; btn.textContent = 'COPIED'; setTimeout(() => btn.textContent = 'COPY', 1000); }}
                                >COPY</button>
                              </div>
                            {/each}
                          </div>
                        </div>
                      {/each}
                    </div>
                  {:else if fieldSearch.trim()}
                    <div class="mt-3 text-center py-4">
                      <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">NO MATCHES FOR "{fieldSearch}"</span>
                    </div>
                  {/if}
                </div>
              </div>
            {/if}

            <!-- Document Map -->
            {#if pageData.length > 0}
              <div class="border-2" style="border-color: var(--line);">
                <div class="dark-bar flex justify-between items-center text-xs">
                  <span>DOCUMENT MAP</span>
                  <span>{pageData.length} PAGES</span>
                </div>
                <div class="bg-white p-3">
                  <div class="flex flex-wrap gap-1 mb-3">
                    {#each pageTypeGroups() as [type, count]}
                      {@const c = getPageTypeColor(type)}
                      <span class="px-1.5 py-0.5 text-[7px] font-medium uppercase text-white" style="background: {c.bg};">{type.replace(/_/g,' ')} ({count})</span>
                    {/each}
                  </div>
                  <div class="flex flex-wrap gap-1 mb-3">
                    {#each pageData as pg}
                      {@const c = getPageTypeColor(pg.page_type || 'unknown')}
                      <button class="w-8 h-8 flex items-center justify-center text-[9px] font-bold text-white cursor-pointer border transition-transform"
                        style="background: {c.bg}; border-color: {selectedMapPage?.page_number === pg.page_number ? 'var(--on-surface)' : c.bg}; transform: {selectedMapPage?.page_number === pg.page_number ? 'scale(1.2)' : 'scale(1)'};"
                        title="{pg.page_type}: {pg.explanation?.slice(0,60)}"
                        onclick={() => selectedMapPage = selectedMapPage?.page_number === pg.page_number ? null : pg}
                      >{pg.page_number}</button>
                    {/each}
                  </div>
                  <div class="space-y-0 border-t pt-2" style="border-color: rgba(56,56,50,0.1);">
                    {#each pageData as pg}
                      {@const c = getPageTypeColor(pg.page_type || 'unknown')}
                      <button class="w-full text-left flex items-start gap-2 px-1 py-1 cursor-pointer transition-colors"
                        style="{selectedMapPage?.page_number === pg.page_number ? 'background: var(--surface-container);' : ''}"
                        onclick={() => selectedMapPage = selectedMapPage?.page_number === pg.page_number ? null : pg}
                      >
                        <span class="flex-shrink-0 w-6 h-5 flex items-center justify-center text-[8px] font-bold text-white" style="background: {c.bg};">{pg.page_number}</span>
                        <span class="flex-shrink-0 text-[8px] font-medium uppercase w-24 truncate" style="color: {c.bg};">{pg.page_type?.replace(/_/g, ' ') || '?'}</span>
                        <span class="text-[9px] flex-1 truncate" style="color: var(--on-surface);">{pg.explanation || '—'}</span>
                        <span class="text-[8px] font-mono flex-shrink-0" style="color: var(--outline);">{pg.fields ? Object.keys(pg.fields).length : 0}f</span>
                        <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{(pg.confidence || 0) >= 0.9 ? 'var(--success)' : (pg.confidence || 0) >= 0.7 ? 'var(--warning)' : 'var(--error)'};flex-shrink:0;"></span>
                        <span class="material-symbols-outlined text-sm flex-shrink-0" style="color: var(--outline);">
                          {selectedMapPage?.page_number === pg.page_number ? 'expand_less' : 'expand_more'}
                        </span>
                      </button>
                      {#if selectedMapPage?.page_number === pg.page_number}
                        <div class="ml-8 mb-2 border-l-2 pl-3 animate-slideDown" style="border-color: {c.bg};">
                          <PageDetail
                            page={selectedMapPage}
                            jobId={job.job_id}
                            totalPages={pageData.length}
                            onprev={() => { const idx = pageData.findIndex(p => p.page_number === selectedMapPage.page_number); if (idx > 0) selectedMapPage = pageData[idx-1]; }}
                            onnext={() => { const idx = pageData.findIndex(p => p.page_number === selectedMapPage.page_number); if (idx < pageData.length-1) selectedMapPage = pageData[idx+1]; }}
                            onclose={() => selectedMapPage = null}
                          />
                        </div>
                      {/if}
                    {/each}
                  </div>
                </div>
              </div>
            {/if}

          {:else if activeTab === 'pagemap'}
            <!-- Page Map (v2) -->
            {#if selectedPage}
              <PageDetail
                page={selectedPage}
                jobId={job.job_id}
                totalPages={pageData.length}
                onprev={prevPage}
                onnext={nextPage}
                onclose={() => selectedPage = null}
              />
            {:else}
              <PageMap pages={pageData} onselect={selectPage} />
            {/if}

          {:else if activeTab === 'annotated'}
            <!-- PDF Viewer with annotated/original toggle -->
            <div class="border-2" style="border-color: var(--line);">
              <div class="dark-bar flex justify-between items-center text-xs">
                <div class="flex items-center gap-3">
                  <span>PDF — {job.pdf_name}</span>
                  <div class="flex gap-0" style="border: 1px solid var(--primary-container);">
                    <button class="px-2 py-0.5 text-[8px] font-bold cursor-pointer"
                      style="{pdfMode === 'annotated' ? 'background: var(--primary-container); color: var(--on-surface);' : 'color: var(--primary-container);'}"
                      disabled={markCount === 0}
                      title={markCount === 0 ? (noMarksReason || 'Nothing could be marked on this document') : `${markCount} values highlighted`}
                      onclick={() => pdfMode = 'annotated'}>MARKED{markCount > 0 ? ` (${markCount})` : ''}</button>
                    <button class="px-2 py-0.5 text-[8px] font-bold cursor-pointer"
                      style="{pdfMode === 'original' ? 'background: var(--primary-container); color: var(--on-surface);' : 'color: var(--primary-container);'}"
                      onclick={() => pdfMode = 'original'}>ORIGINAL</button>
                  </div>
                </div>
                {#if pdfMode === 'annotated' && markCount > 0}
                  <!-- The colours the backend actually writes: header amber
                       (0.98,0.75,0.14), items blue (0.15,0.39,0.92). The legend
                       said green/amber, which matched neither. -->
                  <div class="flex items-center gap-3 text-[8px]">
                    <span><span style="display:inline-block;width:8px;height:8px;background:#FABF24;margin-right:3px;"></span> Declaration</span>
                    <span><span style="display:inline-block;width:8px;height:8px;background:#2664EB;margin-right:3px;"></span> Items</span>
                    <a href={markedSrc} target="_blank" rel="noopener"
                      style="color: var(--primary-container); text-decoration: underline;">OPEN / DOWNLOAD</a>
                  </div>
                {/if}
              </div>
              {#if pdfMode === 'annotated' && markCount === 0}
                <div class="px-3 py-6 text-center text-[11px] font-mono"
                  style="color: var(--on-surface-muted); background: var(--surface-container-low);">
                  NO MARKED PDF — {noMarksReason || 'no positions could be measured on this document'}
                </div>
              {:else}
                <iframe
                  src={pdfMode === 'annotated' ? markedSrc : pdfUrl}
                  title="PDF Viewer"
                  style="width: 100%; height: 700px; border: none;"
                ></iframe>
              {/if}
            </div>

          {:else if activeTab === 'log'}
            <!-- Pipeline Log -->
            {#if agentLines.length > 0}
              <AgentTerminal
                filename={job.pdf_name} lines={agentLines}
                running={false} summary={agentSummary} />
            {:else if hasPipelineData}
              <PipelineTerminal filename={job.pdf_name} steps={terminalStepsResolved()} complete={true}
                summary={terminalSummary} collapsed={false} />
            {:else}
              <div class="text-center p-8 text-xs uppercase" style="color: var(--outline);">
                No pipeline log available for this job
              </div>
            {/if}
          {/if}
        </div>
      </div>
    {/if}
  </div>
{/if}
