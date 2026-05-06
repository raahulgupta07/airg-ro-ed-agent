<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import Button from '$lib/components/Button.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import PageMap from '$lib/components/PageMap.svelte';
  import PageDetail from '$lib/components/PageDetail.svelte';
  import ResultAccordion from '$lib/components/ResultAccordion.svelte';
  import ReviewSplitView from '$lib/components/ReviewSplitView.svelte';
  import { getAccuracyColor, getPageTypeColor } from '$lib/colors';

  let jobs = $state<any[]>([]);
  let loading = $state(true);
  let searchQuery = $state('');
  let dateFrom = $state('');
  let dateTo = $state('');
  let selectedUser = $state('');

  // Screen 2 state
  let selectedJobId = $state<string | null>(null);
  let selectedJob = $state<any>(null);
  let loadingDetail = $state(false);

  // Page map
  let pageData = $state<any[]>([]);
  let pageDataLoaded = $state(false);
  let selectedPageDetail = $state<any>(null);

  // History detail tab
  let historyTab = $state<'data' | 'log'>('data');

  // Field search across all pages
  let fieldSearch = $state('');

  const fieldSearchResults = $derived(() => {
    if (!fieldSearch.trim() || pageData.length === 0) return [];
    const q = fieldSearch.toLowerCase().trim();
    const results: { page: number; pageType: string; source: string; field: string; value: string }[] = [];

    for (const pg of pageData) {
      const pn = pg.page_number;
      const pt = pg.page_type || 'unknown';

      // Search fields (key-value pairs)
      if (pg.fields) {
        for (const [k, v] of Object.entries(pg.fields)) {
          if (k.toLowerCase().includes(q) || String(v).toLowerCase().includes(q)) {
            results.push({ page: pn, pageType: pt, source: 'field', field: k, value: String(v) });
          }
        }
      }

      // Search amounts
      if (pg.amounts && Array.isArray(pg.amounts)) {
        for (const amt of pg.amounts) {
          const label = amt.label || '';
          const val = `${amt.value ?? ''} ${amt.currency ?? ''}`.trim();
          if (label.toLowerCase().includes(q) || val.toLowerCase().includes(q)) {
            results.push({ page: pn, pageType: pt, source: 'amount', field: label, value: val });
          }
        }
      }

      // Search tables (headers + cells)
      if (pg.items && Array.isArray(pg.items)) {
        for (const table of pg.items) {
          if (Array.isArray(table)) {
            // flat items array
            for (const [k2, v2] of Object.entries(table)) {
              if (String(k2).toLowerCase().includes(q) || String(v2).toLowerCase().includes(q)) {
                results.push({ page: pn, pageType: pt, source: 'table', field: String(k2), value: String(v2) });
              }
            }
          } else if (typeof table === 'object' && table !== null) {
            for (const [k2, v2] of Object.entries(table)) {
              if (String(k2).toLowerCase().includes(q) || String(v2).toLowerCase().includes(q)) {
                results.push({ page: pn, pageType: pt, source: 'table', field: String(k2), value: String(v2) });
              }
            }
          }
        }
      }

      // Search document metadata
      for (const metaKey of ['doc_title', 'doc_issuer', 'doc_date', 'doc_reference', 'doc_country', 'explanation']) {
        const val = pg[metaKey];
        if (val && (metaKey.toLowerCase().includes(q) || String(val).toLowerCase().includes(q))) {
          results.push({ page: pn, pageType: pt, source: 'meta', field: metaKey.replace('doc_', ''), value: String(val) });
        }
      }
    }

    return results;
  });

  const allUsers = $derived([...new Set(jobs.map(j => j.username).filter(Boolean))]);

  const filteredJobs = $derived(() => {
    let result = jobs;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(j => j.pdf_name?.toLowerCase().includes(q) || j.job_id?.toLowerCase().includes(q));
    }
    if (selectedUser) result = result.filter(j => j.username === selectedUser);
    if (dateFrom) result = result.filter(j => (j.created_at || '').split(' ')[0] >= dateFrom);
    if (dateTo) result = result.filter(j => (j.created_at || '').split(' ')[0] <= dateTo);
    return result;
  });

  let detailError = $state('');

  async function openJob(jobId: string) {
    selectedJobId = jobId;
    loadingDetail = true;
    detailError = '';
    pageDataLoaded = false;
    pageData = [];
    selectedPageDetail = null;

    try {
      selectedJob = await api.getJob(jobId);
      // Load page data in background
      api.getJobPages(jobId).then(p => { pageData = p; pageDataLoaded = true; }).catch(() => {});
    } catch (e: any) {
      detailError = e?.message || 'Failed to load job details';
    }
    loadingDetail = false;

    const url = new URL(window.location.href);
    url.searchParams.set('job', jobId);
    window.history.replaceState({}, '', url.toString());
  }

  function backToList() {
    selectedJobId = null;
    selectedJob = null;
    const url = new URL(window.location.href);
    url.searchParams.delete('job');
    window.history.replaceState({}, '', url.toString());
  }

  async function downloadExcel() {
    if (!selectedJob?.job_id) return;
    try {
      const res = await fetch(`/api/jobs/${selectedJob.job_id}/download`, {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${selectedJob.pdf_name?.replace('.pdf', '')}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {}
  }

  let showPdf = $state(false);
  let declView = $state<'cards' | 'table'>('cards');

  function ptColor(pageType: string): string {
    return getPageTypeColor(pageType || 'unknown').bg;
  }

  const pageTypeGroups = $derived(() => {
    const g: Record<string,number> = {};
    pageData.forEach(p => { const t = p.page_type||'other'; g[t]=(g[t]||0)+1; });
    return Object.entries(g).sort((a,b)=>b[1]-a[1]);
  });

  onMount(async () => {
    try { jobs = await api.listJobs(200); } catch {}
    loading = false;
    const params = new URLSearchParams(window.location.search);
    const jobParam = params.get('job');
    if (jobParam) openJob(jobParam);
  });
</script>


{#if loading}
  <div class="skeleton h-64 w-full"></div>

<!-- ═══════════════════════════════════════════════ -->
<!-- SCREEN 1: JOB LIST (full width table)          -->
<!-- ═══════════════════════════════════════════════ -->
{:else if !selectedJobId}
  <ChapterHeading icon="history" title="EXTRACTION_HISTORY" subtitle="Review past extraction jobs" question="Click any job to view details" />

  <!-- Filters -->
  <div class="flex flex-wrap gap-3 items-end mb-4 p-3 border-2 bg-white" style="border-color: var(--on-surface);">
    <div class="flex-1 min-w-[180px]">
      <div class="tag-label mb-1 text-[8px]">SEARCH</div>
      <input type="text" placeholder="Document name or number..." bind:value={searchQuery}
             class="w-full text-xs font-bold uppercase px-3 py-1.5 focus:outline-none"
             style="border: 2px solid var(--on-surface); background: white;" />
    </div>
    <div>
      <div class="tag-label mb-1 text-[8px]">USER</div>
      <select bind:value={selectedUser} class="text-[10px] font-mono font-bold uppercase px-2 py-1.5 focus:outline-none"
              style="border: 2px solid var(--on-surface); background: white;">
        <option value="">ALL USERS</option>
        {#each allUsers as u}<option value={u}>{u}</option>{/each}
      </select>
    </div>
    <div>
      <div class="tag-label mb-1 text-[8px]">FROM</div>
      <input type="date" bind:value={dateFrom} class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
             style="border: 2px solid var(--on-surface); background: white;" />
    </div>
    <div>
      <div class="tag-label mb-1 text-[8px]">TO</div>
      <input type="date" bind:value={dateTo} class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
             style="border: 2px solid var(--on-surface); background: white;" />
    </div>
    <div class="text-[10px] font-mono font-bold" style="color: var(--outline);">{filteredJobs().length} / {jobs.length} JOBS</div>
  </div>

  <!-- Jobs Table -->
  <div class="border-2" style="border-color: var(--on-surface);">
    <div class="dark-bar flex justify-between items-center text-xs">
      <span>JOBS</span>
      <span>{filteredJobs().length} TOTAL</span>
    </div>
    <div class="overflow-x-auto bg-white">
      <table class="w-full text-[11px]">
        <thead>
          <tr style="background: var(--surface-container);">
            <th class="px-3 py-2 text-left whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 40px;">#</th>
            <th class="px-3 py-2 text-left whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline);">PDF NAME</th>
            <th class="px-3 py-2 text-left whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 90px;">USER</th>
            <th class="px-3 py-2 text-left whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 110px;">DATE</th>
            <th class="px-3 py-2 text-right whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 70px;">ITEMS</th>
            <th class="px-3 py-2 text-right whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 90px;">ACCURACY</th>
            <th class="px-3 py-2 text-right whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 70px;">PAGES</th>
            <th class="px-3 py-2 text-right whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 70px;">TIME</th>
            <th class="px-3 py-2 text-right whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 80px;">COST</th>
            <th class="px-3 py-2 text-center whitespace-nowrap text-[9px] font-bold uppercase" style="color: var(--outline); width: 80px;">STATUS</th>
          </tr>
        </thead>
        <tbody>
          {#each filteredJobs() as job, i}
            {@const acc = job.accuracy_percent ?? 0}
            <tr class="border-t cursor-pointer transition-colors"
                style="border-color: rgba(56,56,50,0.1);"
                onmouseenter={(e) => (e.currentTarget as HTMLElement).style.background = 'var(--surface-container)'}
                onmouseleave={(e) => (e.currentTarget as HTMLElement).style.background = 'white'}
                onclick={() => openJob(job.job_id)}>
              <td class="px-3 py-2 font-mono text-[9px]" style="color: var(--outline);">{i+1}</td>
              <td class="px-3 py-2 font-bold" style="color: var(--on-surface);">{job.pdf_name}</td>
              <td class="px-3 py-2"><span class="text-[8px] font-black uppercase px-1 py-0.5 text-white" style="background: var(--secondary);">{job.username ?? '?'}</span></td>
              <td class="px-3 py-2 font-mono text-[9px]" style="color: var(--outline);">{job.created_at?.split(' ')[0] ?? ''}</td>
              <td class="px-3 py-2 text-right font-bold" style="color: var(--on-surface);">{job.items?.length ?? '—'}</td>
              <td class="px-3 py-2 text-right font-mono font-bold" style="color: {getAccuracyColor(acc)};">{acc.toFixed(1)}%</td>
              <td class="px-3 py-2 text-right font-mono" style="color: var(--outline);">{job.total_pages ?? '—'}</td>
              <td class="px-3 py-2 text-right font-mono" style="color: var(--outline);">{job.processing_time_seconds?.toFixed(0) ?? '—'}s</td>
              <td class="px-3 py-2 text-right font-mono" style="color: #fbbf24;">${(job.cost_usd || 0).toFixed(3)}</td>
              <td class="px-3 py-2 text-center">
                <Badge text={job.status === 'COMPLETED' ? '✓' : '✗'} variant={job.status === 'COMPLETED' ? 'success' : 'critical'} />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if filteredJobs().length === 0}
        <div class="p-8 text-center text-sm font-bold uppercase" style="color: var(--outline);">NO JOBS</div>
      {/if}
    </div>
  </div>

<!-- ═══════════════════════════════════════════════ -->
<!-- SCREEN 2: JOB DETAIL (full width)              -->
<!-- ═══════════════════════════════════════════════ -->
{:else}
  {#if loadingDetail}
    <div class="flex items-center gap-3 p-12 justify-center">
      <div class="agent-spinner" style="border-color: var(--secondary); border-top-color: transparent;"></div>
      <span class="text-sm font-bold uppercase" style="color: var(--on-surface);">LOADING...</span>
    </div>
  {:else if detailError}
    <div class="flex flex-col items-center gap-4 p-12 justify-center">
      <span class="material-symbols-outlined text-3xl" style="color: var(--tertiary);">error</span>
      <span class="text-sm font-bold uppercase" style="color: var(--on-surface);">FAILED TO LOAD</span>
      <span class="text-[10px] font-mono" style="color: var(--outline);">{detailError}</span>
      <div class="flex gap-3">
        <button class="text-[10px] font-bold uppercase px-3 py-2 border-2 cursor-pointer"
          style="border-color: var(--primary); color: var(--primary); background: transparent;"
          onclick={() => { if (selectedJobId) openJob(selectedJobId); }}>
          RETRY
        </button>
        <button class="text-[10px] font-bold uppercase px-3 py-2 border-2 cursor-pointer"
          style="border-color: var(--on-surface); color: var(--on-surface); background: transparent;"
          onclick={backToList}>
          BACK TO LIST
        </button>
      </div>
    </div>
  {:else if selectedJob}
    {@const acc = selectedJob.accuracy_percent ?? 0}
    {@const items = selectedJob.items ?? []}
    {@const decl = selectedJob.declarations?.[0]}
    {@const decision = acc >= 90 ? 'ACCEPTED' : acc >= 60 ? 'FIXED' : 'ESCALATED'}

    <!-- Header bar -->
    <div class="flex items-center justify-between mb-4 p-3 border-2 bg-white" style="border-color: var(--on-surface);">
      <div class="flex items-center gap-3">
        <button class="flex items-center gap-1 text-[10px] font-black uppercase cursor-pointer px-2 py-1 border"
          style="border-color: var(--on-surface); color: var(--on-surface);" onclick={backToList}>
          <span class="material-symbols-outlined text-sm">arrow_back</span> HISTORY
        </button>
        <span class="text-sm font-bold" style="color: var(--on-surface);">{selectedJob.pdf_name}</span>
        <span class="text-[8px] font-black uppercase px-1 py-0.5 text-white" style="background: var(--secondary);">{selectedJob.username ?? '?'}</span>
        <Badge text={selectedJob.status} variant={selectedJob.status === 'COMPLETED' ? 'success' : 'critical'} />
      </div>
      <div class="flex items-center gap-2">
        <span class="text-[9px] font-mono" style="color: var(--outline);">{selectedJob.created_at?.split(' ')[0] ?? ''}</span>
        <Button variant="secondary" size="sm" onclick={() => showPdf = !showPdf}>
          <span class="flex items-center gap-1">
            <span class="material-symbols-outlined text-xs">picture_as_pdf</span> {showPdf ? 'HIDE' : 'PDF'}
          </span>
        </Button>
        <Button variant="secondary" size="sm" onclick={downloadExcel}>
          <span class="flex items-center gap-1">
            <span class="material-symbols-outlined text-xs">download</span> XLSX
          </span>
        </Button>
      </div>
    </div>

    {@const _ti = selectedJob.tokens_in ?? 0}
    {@const _to = selectedJob.tokens_out ?? 0}
    {@const _model = selectedJob.model_used || (selectedJob.pipeline_mode || '').toUpperCase() || '—'}
    <!-- KPI Row 1: 6 across -->
    <div class="grid grid-cols-6 gap-2 mb-2">
      <KpiCard title="ITEMS" value="{items.length}" accent="#007518" />
      <KpiCard title="ACCURACY" value="{acc.toFixed(1)}%" progress={acc} accent={getAccuracyColor(acc)} />
      <KpiCard title="DECISION" value="{decision}" accent={getAccuracyColor(acc)} />
      <KpiCard title="PAGES" value="{selectedJob.total_pages ?? '—'}" accent="#006f7c" />
      <KpiCard title="TIME" value="{selectedJob.processing_time_seconds?.toFixed(0) ?? '—'}s" accent="#006f7c" />
      <KpiCard title="COST" value="${selectedJob.cost_usd?.toFixed(3) ?? '—'}" accent="#8b6914" />
    </div>
    <!-- KPI Row 2: tokens + model + processed -->
    <div class="grid grid-cols-4 gap-2 mb-4">
      <KpiCard title="TOKENS_IN" value="{(_ti/1000).toFixed(1)}K" accent="#1d4ed8" subtitle="{_ti.toLocaleString()}" />
      <KpiCard title="TOKENS_OUT" value="{(_to/1000).toFixed(1)}K" accent="#7c3aed" subtitle="{_to.toLocaleString()}" />
      <KpiCard title="TOKENS_TOTAL" value="{((_ti+_to)/1000).toFixed(1)}K" accent="#6d28d9" subtitle="$/1k: ${_ti+_to > 0 ? (((selectedJob.cost_usd||0)*1000)/(_ti+_to)).toFixed(4) : '—'}" />
      <KpiCard title="MODEL" value="{_model}" accent="#7e22ce" subtitle="{selectedJob.pipeline_mode || ''}" />
    </div>

    <!-- Tab bar -->
    <div class="flex gap-0 mb-4 border-2" style="border-color: var(--on-surface); background: var(--surface-container-highest);">
      {#each [['data','DATA'],['log','PIPELINE LOG']] as [key, label]}
        <button class="px-3 py-2 text-[11px] font-bold uppercase tracking-tight cursor-pointer"
          style="{historyTab === key ? 'background: var(--on-surface); color: var(--surface);' : 'color: var(--outline);'}"
          onclick={() => historyTab = key as any}
        >{label}</button>
      {/each}
    </div>

    {#if historyTab === 'data'}
    <!-- PDF Viewer (collapsible) -->
    {#if showPdf}
      <div class="border-2 mb-4" style="border-color: var(--on-surface);">
        <div class="dark-bar flex items-center justify-between text-xs">
          <span>ORIGINAL_PDF — {selectedJob.pdf_name}</span>
          <button class="text-[10px] font-bold uppercase cursor-pointer" style="color: var(--primary-container);" onclick={() => showPdf = false}>CLOSE</button>
        </div>
        <iframe src="/api/jobs/{selectedJob.job_id}/pdf?token={auth.token}" title="PDF" style="width: 100%; height: 600px; border: none;"></iframe>
      </div>
    {/if}

    <!-- V11 jobs use side-by-side review; others stay on ResultAccordion -->
    {#if selectedJob.pipeline_mode === 'v11'}
      <ReviewSplitView jobId={selectedJob.job_id} job={selectedJob} />
    {:else}
      <ResultAccordion job={selectedJob} defaultOpen={true} />
    {/if}

    {/if}

    {#if historyTab === 'log'}
      <!-- Pipeline Log — reads detailed logs from DB processing_logs -->
      <div style="border: 2px solid #383832; box-shadow: 4px 4px 0px 0px #383832; background: #0a0a0f; padding: 16px; font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 11px; line-height: 1.6; color: #9ca3af; max-height: 700px; overflow-y: auto;">
        <!-- Command line -->
        <div>
          <span style="color: #22c55e;">❯</span>
          <span style="color: #9ca3af;"> ro-ed extract</span>
          <span style="color: #eab308;"> "{selectedJob.pdf_name}"</span>
        </div>
        <div style="color: #1e1e2e; margin: 4px 0;">────────────────────────────────────────────────────────────</div>

        <!-- Detailed logs from DB -->
        {#if selectedJob.logs?.length > 0}
          {#each selectedJob.logs as log}
            {@const lines = (log.message || '').split('\n')}
            {#each lines as line}
              {#if line.trim()}
                {#if line.includes('STEP') || line.includes('Starting') || line.includes('═══')}
                  <div style="color: #e5e7eb; font-weight: bold; margin-top: 8px;">{line}</div>
                {:else if line.includes('✅') || line.includes('Done') || line.includes('complete') || line.includes('found')}
                  <div style="color: #22c55e;">{line}</div>
                {:else if line.includes('❌') || line.includes('FAILED') || line.includes('not found')}
                  <div style="color: #ef4444;">{line}</div>
                {:else if line.includes('⚠') || line.includes('warning')}
                  <div style="color: #eab308;">{line}</div>
                {:else if line.includes('───')}
                  <div style="color: #1e1e2e;">{line}</div>
                {:else if line.startsWith('  ') || line.startsWith('   ')}
                  <div style="color: #6b7280; padding-left: 4px;">{line}</div>
                {:else}
                  <div style="color: #9ca3af;">{line}</div>
                {/if}
              {/if}
            {/each}
          {/each}
        {:else}
          <!-- Fallback: generate summary from job data -->
          <div style="color: #9ca3af;">📎 File: {selectedJob.pdf_name} ({(selectedJob.pdf_size / 1024 / 1024).toFixed(1)} MB)</div>
          <div style="color: #e5e7eb; font-weight: bold; margin-top: 6px;">🤖 RO-ED AI Agent</div>
          <div style="color: #9ca3af;">📄 Processed {selectedJob.total_pages} pages</div>
          <div style="color: #22c55e;">✅ Extracted {items.length} items, 16 declaration fields</div>
          <div style="color: #9ca3af;">📊 Accuracy: {acc.toFixed(1)}% | Time: {selectedJob.processing_time_seconds?.toFixed(1)}s | Cost: ${selectedJob.cost_usd?.toFixed(3)}</div>
        {/if}

        <!-- Completion box -->
        <div style="margin-top: 8px; border: 1px solid #14532d; background: #052e16; padding: 8px 12px;">
          <div style="color: #22c55e; font-weight: bold;">✅ EXTRACTION COMPLETE</div>
          <div style="margin-top: 4px; color: #4b5563; font-size: 10px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px 12px;">
            <div>Items <span style="color: #d1d5db; font-weight: bold;">{items.length}</span></div>
            <div>Accuracy <span style="color: #22c55e; font-weight: bold;">{acc.toFixed(1)}%</span></div>
            <div>Status <span style="color: #22c55e;">{decision}</span></div>
            <div>Time <span style="color: #9ca3af;">{selectedJob.processing_time_seconds?.toFixed(1)}s</span></div>
            <div>Cost <span style="color: #eab308;">${selectedJob.cost_usd?.toFixed(3)}</span></div>
            <div>Pages <span style="color: #9ca3af;">{selectedJob.total_pages}</span></div>
          </div>
        </div>
      </div>
    {/if}

  {/if}
{/if}
