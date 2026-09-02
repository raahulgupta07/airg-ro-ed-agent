<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import Button from '$lib/components/Button.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import ResultAccordion from '$lib/components/ResultAccordion.svelte';
  import ReviewSplitView from '$lib/components/ReviewSplitView.svelte';
  import { getAccuracyColor, getPageTypeColor } from '$lib/colors';

  let jobs = $state<any[]>([]);
  let loading = $state(true);
  let searchQuery = $state('');
  let dateFrom = $state('');
  let dateTo = $state('');
  let selectedUser = $state('');
  let selectedEngine = $state('');
  let selectedStatus = $state('');
  let confBand = $state('');           // '90' | '70' | 'low'

  // Declaration numbers live on /data/declarations, not on the job row — load them
  // once so "search by declaration no" works here too. Fail-safe: no map, no match.
  let declNoByJob = $state<Record<string, string>>({});

  // Screen 2 state
  let selectedJobId = $state<string | null>(null);
  let selectedJob = $state<any>(null);
  let loadingDetail = $state(false);

  // Page map
  let pageData = $state<any[]>([]);
  let pageDataLoaded = $state(false);

  // History detail tab
  let historyTab = $state<'data' | 'log'>('data');

  // Field search across all pages (debounced — the scan below touches every
  // page/field/amount, so we don't want to run it on every keystroke).
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
    // year-first: 2025-06-25 / 2025/06/25 / 2025.06.25 (optional time after)
    let m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
    if (m) return `${m[1]}-${_p2(m[2])}-${_p2(m[3])}`;
    // day-first (Myanmar customs forms): 12/10/2025 · 01-04-2024
    m = s.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})/);
    if (m) {
      let d = Number(m[1]), mo = Number(m[2]);
      if (mo > 12 && d <= 12) { const t = d; d = mo; mo = t; }  // was month-first
      if (mo < 1 || mo > 12 || d < 1 || d > 31) return '';
      return `${m[3]}-${_p2(mo)}-${_p2(d)}`;
    }
    // 25 Jun 2025 · Jun 25, 2025
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

  const engineOf = (j: any) => (j.model_used || j.pipeline_mode || '').trim();
  const bandOf = (a: number) => (a >= 90 ? '90' : a >= 70 ? '70' : 'low');

  // Dropdown values come from the data, never a hardcoded list.
  const allUsers = $derived([...new Set(jobs.map(j => j.username).filter(Boolean))].sort());
  const allEngines = $derived([...new Set(jobs.map(engineOf).filter(Boolean))].sort());
  const allStatuses = $derived([...new Set(jobs.map(j => j.status).filter(Boolean))].sort());

  const anyFilter = $derived(
    !!(searchQuery || dateFrom || dateTo || selectedUser || selectedEngine || selectedStatus || confBand)
  );

  function resetFilters() {
    searchQuery = '';
    dateFrom = '';
    dateTo = '';
    selectedUser = '';
    selectedEngine = '';
    selectedStatus = '';
    confBand = '';
  }

  const filteredJobs = $derived(() => {
    let result = jobs;
    if (searchQuery) {
      const q = searchQuery.toLowerCase().trim();
      result = result.filter(j =>
        j.pdf_name?.toLowerCase().includes(q) ||
        j.job_id?.toLowerCase().includes(q) ||
        (declNoByJob[j.job_id] || '').toLowerCase().includes(q)
      );
    }
    if (selectedUser) result = result.filter(j => j.username === selectedUser);
    if (selectedEngine) result = result.filter(j => engineOf(j) === selectedEngine);
    if (selectedStatus) result = result.filter(j => j.status === selectedStatus);
    if (confBand) result = result.filter(j => bandOf(j.accuracy_percent ?? 0) === confBand);
    if (dateFrom || dateTo) result = result.filter(j => inDateRange(j.created_at, dateFrom, dateTo));
    return result;
  });

  let detailError = $state('');

  async function openJob(jobId: string) {
    selectedJobId = jobId;
    loadingDetail = true;
    detailError = '';
    pageDataLoaded = false;
    pageData = [];

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

  // ── Marked PDF ────────────────────────────────────────────────────────
  // The original with every extracted value highlighted. The count comes from
  // the server, not from `field_bboxes` on the payload: a stored box whose
  // value is gone is not a mark, so counting boxes here overstates what the
  // file will actually contain.
  let markCount = $state(0);
  let noMarksReason = $state('');
  $effect(() => {
    const id = selectedJob?.job_id;
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
    // Background: declaration numbers for search. Never blocks the table.
    api.listDeclarations().then(ds => {
      const m: Record<string, string> = {};
      for (const d of ds ?? []) if (d?.job_id && d?.declaration_no) m[d.job_id] = String(d.declaration_no);
      declNoByJob = m;
    }).catch(() => {});
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

  {@const jobRows = filteredJobs()}

  <!-- Filters -->
  <div class="fbar">
    <div class="fsearch">
      <label class="cl-lbl" for="hist-search">Search</label>
      <div class="fsearch-in">
        <span class="material-symbols-outlined fsearch-ic" aria-hidden="true">search</span>
        <input id="hist-search" type="search" class="cl-inp"
               placeholder="File name, declaration no or job id..."
               bind:value={searchQuery} />
      </div>
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-from">From</label>
      <input id="hist-from" type="date" bind:value={dateFrom} class="cl-inp" />
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-to">To</label>
      <input id="hist-to" type="date" bind:value={dateTo} class="cl-inp" />
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-engine">Engine</label>
      <select id="hist-engine" bind:value={selectedEngine} class="cl-inp">
        <option value="">All engines</option>
        {#each allEngines as e}<option value={e}>{e}</option>{/each}
      </select>
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-status">Status</label>
      <select id="hist-status" bind:value={selectedStatus} class="cl-inp">
        <option value="">All statuses</option>
        {#each allStatuses as s}<option value={s}>{s}</option>{/each}
      </select>
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-conf">Confidence</label>
      <select id="hist-conf" bind:value={confBand} class="cl-inp">
        <option value="">Any confidence</option>
        <option value="90">90% and above</option>
        <option value="70">70–90%</option>
        <option value="low">Below 70%</option>
      </select>
    </div>
    <div class="ffield">
      <label class="cl-lbl" for="hist-user">User</label>
      <select id="hist-user" bind:value={selectedUser} class="cl-inp">
        <option value="">All users</option>
        {#each allUsers as u}<option value={u}>{u}</option>{/each}
      </select>
    </div>
    <div class="fend">
      <button class="cl-btn sm" onclick={resetFilters} disabled={!anyFilter}>Reset</button>
      <span class="fcount" aria-live="polite">showing {jobRows.length} of {jobs.length}</span>
    </div>
  </div>

  <!-- Quick filters -->
  <div class="fchips" role="group" aria-label="Quick filters">
    <button class="fchip" aria-pressed={rangeActive(0)} onclick={() => setRange(0)}>Today</button>
    <button class="fchip" aria-pressed={rangeActive(7)} onclick={() => setRange(7)}>Last 7 days</button>
    <button class="fchip" aria-pressed={rangeActive(30)} onclick={() => setRange(30)}>Last 30 days</button>
    <button class="fchip" aria-pressed={confBand === 'low'}
            onclick={() => confBand = confBand === 'low' ? '' : 'low'}>Needs review (below 70%)</button>
    <button class="fchip" aria-pressed={confBand === '90'}
            onclick={() => confBand = confBand === '90' ? '' : '90'}>High confidence (90%+)</button>
    <button class="fchip" aria-pressed={selectedStatus === 'COMPLETED'}
            onclick={() => selectedStatus = selectedStatus === 'COMPLETED' ? '' : 'COMPLETED'}>Completed only</button>
    {#if auth.user?.username}
      {@const me = auth.user.username}
      <button class="fchip" aria-pressed={selectedUser === me}
              onclick={() => selectedUser = selectedUser === me ? '' : me}>My jobs</button>
    {/if}
  </div>

  <!-- Jobs Table -->
  <div class="cl-panel">
    <div class="cl-hd">
      <span class="dot">◉</span>Jobs
      <span class="ct">showing {jobRows.length} of {jobs.length} jobs</span>
    </div>
    <div class="overflow-x-auto custom-scrollbar">
      <table class="cl-table">
        <thead>
          <tr>
            <th style="width: 40px;">#</th>
            <th>PDF Name</th>
            <th style="width: 90px;">User</th>
            <th style="width: 150px;">Timestamp</th>
            <th style="width: 70px; text-align: right;">Items</th>
            <th style="width: 90px; text-align: right;">Accuracy</th>
            <th style="width: 70px; text-align: right;">Pages</th>
            <th style="width: 70px; text-align: right;">Time</th>
            <th style="width: 80px; text-align: right;">Cost</th>
            <th style="width: 80px; text-align: center;">Status</th>
          </tr>
        </thead>
        <tbody>
          {#each jobRows as job, i}
            {@const acc = job.accuracy_percent ?? 0}
            <tr class="cursor-pointer" onclick={() => openJob(job.job_id)}>
              <td class="mono" style="color: var(--on-surface-subtle);">{i+1}</td>
              <td class="k">{job.pdf_name}</td>
              <td><span class="pill muted">{job.username ?? '?'}</span></td>
              <td class="mono" style="color: var(--on-surface-muted);">{job.created_at ?? ''}</td>
              <td style="text-align: right; font-weight: 600;">{job.items?.length ?? '—'}</td>
              <td class="mono" style="text-align: right; font-weight: 600; color: {getAccuracyColor(acc)};">{acc.toFixed(1)}%</td>
              <td class="mono" style="text-align: right; color: var(--on-surface-muted);">{job.total_pages ?? '—'}</td>
              <td class="mono" style="text-align: right; color: var(--on-surface-muted);">{job.processing_time_seconds?.toFixed(0) ?? '—'}s</td>
              <td class="mono" style="text-align: right; color: var(--warning);">${(job.cost_usd || 0).toFixed(3)}</td>
              <td style="text-align: center;">
                <span class="pill {job.status === 'COMPLETED' ? 'ok' : 'err'}">{job.status === 'COMPLETED' ? '✓' : '✗'}</span>
              </td>
            </tr>
          {/each}
          {#if jobRows.length === 0}
            <tr>
              <td colspan="10" class="fempty">
                {#if jobs.length === 0}
                  <div class="fempty-t">No extraction jobs yet</div>
                  <div class="fempty-s">Run a document on the Agent page and it will appear here.</div>
                {:else}
                  <div class="fempty-t">No jobs match these filters</div>
                  <div class="fempty-s">{jobs.length} jobs are loaded — widen the dates or clear the search.</div>
                  <button class="cl-btn sm" onclick={resetFilters}>Reset filters</button>
                {/if}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
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
        <button class="cl-btn sm primary"
          onclick={() => { if (selectedJobId) openJob(selectedJobId); }}>
          Retry
        </button>
        <button class="cl-btn sm" onclick={backToList}>
          Back to list
        </button>
      </div>
    </div>
  {:else if selectedJob}
    {@const acc = selectedJob.accuracy_percent ?? 0}
    {@const items = selectedJob.items ?? []}
    {@const decl = selectedJob.declarations?.[0]}
    {@const decision = acc >= 90 ? 'ACCEPTED' : acc >= 60 ? 'FIXED' : 'ESCALATED'}
    {@const _ti = selectedJob.tokens_in ?? 0}
    {@const _to = selectedJob.tokens_out ?? 0}
    {@const _model = selectedJob.model_used || (selectedJob.pipeline_mode || '').toUpperCase() || '—'}

    <!-- Header bar — file + status + inline stats + actions, one row -->
    <div class="flex items-center gap-3 flex-wrap mb-4 cl-panel" style="padding: 10px 12px;">
      <button class="cl-btn sm flex items-center gap-1" onclick={backToList}>
        <span class="material-symbols-outlined text-sm">arrow_back</span> History
      </button>
      <span class="text-sm font-bold" style="color: var(--on-surface);">{selectedJob.pdf_name}</span>
      <span class="pill {selectedJob.status === 'COMPLETED' ? 'ok' : 'err'}">{selectedJob.status}</span>

      <span class="text-[11px] font-mono flex items-center gap-2 flex-wrap" style="color: var(--on-surface-muted);">
        <span><b style="color: var(--on-surface);">{items.length}</b> items</span>·
        <span style="color: {getAccuracyColor(acc)}; font-weight: 700;">{acc.toFixed(1)}%</span>·
        <span>{selectedJob.total_pages ?? '—'} pg</span>·
        <span>{selectedJob.processing_time_seconds?.toFixed(0) ?? '—'}s</span>·
        <span>${selectedJob.cost_usd?.toFixed(3) ?? '—'}</span>·
        <span>TOK {((_ti+_to)/1000).toFixed(1)}k</span>·
        <span class="truncate max-w-[180px]" title={_model}>{_model}</span>
      </span>

      <span class="flex-1"></span>
      <span class="text-[9px] font-mono" style="color: var(--outline);">{selectedJob.created_at?.split(' ')[0] ?? ''}</span>
      <Button variant="secondary" size="sm" onclick={() => showPdf = !showPdf}>
        <span class="flex items-center gap-1">
          <span class="material-symbols-outlined text-xs">picture_as_pdf</span> {showPdf ? 'HIDE' : 'PDF'}
        </span>
      </Button>
      {#if markCount > 0}
        <a href={api.markedPdfUrl(selectedJob.job_id)} target="_blank" rel="noopener"
          class="cl-btn sm no-underline"
          title="Open the PDF with all {markCount} extracted values highlighted on it">
          <span class="flex items-center gap-1">
            <span class="material-symbols-outlined text-xs">highlight</span> MARKED ({markCount})
          </span>
        </a>
      {:else}
        <!-- Disabled, not hidden: the absence is a property of the document, and
             a button that simply is not there reads as a missing feature. -->
        <span class="cl-btn sm" style="opacity: 0.45; cursor: not-allowed;"
          title={noMarksReason || 'No positions could be measured on this document, so nothing can be marked'}>
          <span class="flex items-center gap-1">
            <span class="material-symbols-outlined text-xs">highlight</span> MARKED —
          </span>
        </span>
      {/if}
      <Button variant="secondary" size="sm" onclick={downloadExcel}>
        <span class="flex items-center gap-1">
          <span class="material-symbols-outlined text-xs">download</span> XLSX
        </span>
      </Button>
    </div>

    <!-- Tab bar -->
    <div class="flex gap-0 mb-4 overflow-hidden" style="border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--surface-container-low);">
      {#each [['data','DATA'],['log','PIPELINE LOG']] as [key, label]}
        <button class="px-4 py-2 text-[11px] font-bold uppercase tracking-tight cursor-pointer"
          style="{historyTab === key ? 'background: var(--surface-container-lowest); color: var(--on-surface);' : 'color: var(--on-surface-muted); background: transparent;'}"
          onclick={() => historyTab = key as any}
        >{label}</button>
      {/each}
    </div>

    {#if historyTab === 'data'}
    <!-- PDF Viewer (collapsible) -->
    {#if showPdf}
      <div class="cl-panel mb-4">
        <div class="cl-hd">
          <span class="dot">◉</span>Original PDF — {selectedJob.pdf_name}
          <button class="ct cursor-pointer" style="border: none; background: none;" onclick={() => showPdf = false}>Close</button>
        </div>
        <iframe src="/api/jobs/{selectedJob.job_id}/pdf?token={auth.token}" title="PDF" style="width: 100%; height: 600px; border: none;"></iframe>
      </div>
    {/if}

    <!-- V11 jobs use side-by-side review; others stay on ResultAccordion -->
    {#if selectedJob.pipeline_mode === 'v11'}
      <ReviewSplitView jobId={selectedJob.job_id} job={selectedJob} slim />
    {:else}
      <ResultAccordion job={selectedJob} defaultOpen={true} />
    {/if}

    {/if}

    {#if historyTab === 'log'}
      <!-- Pipeline Log — reads detailed logs from DB processing_logs -->
      <div style="border: 1px solid var(--on-surface); box-shadow: var(--shadow-sm); background: #0a0a0f; padding: 16px; font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 11px; line-height: 1.6; color: #9ca3af; max-height: 700px; overflow-y: auto;">
        <!-- Command line -->
        <div>
          <span style="color: var(--success);">❯</span>
          <span style="color: #9ca3af;"> cityagent extract</span>
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
                  <div style="color: var(--success);">{line}</div>
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
          <div style="color: #e5e7eb; font-weight: bold; margin-top: 6px;">🤖 City Agent ROVER</div>
          <div style="color: #9ca3af;">📄 Processed {selectedJob.total_pages} pages</div>
          <div style="color: var(--success);">✅ Extracted {items.length} items, 16 declaration fields</div>
          <div style="color: #9ca3af;">📊 Accuracy: {acc.toFixed(1)}% | Time: {selectedJob.processing_time_seconds?.toFixed(1)}s | Cost: ${selectedJob.cost_usd?.toFixed(3)}</div>
        {/if}

        <!-- Completion box -->
        <div style="margin-top: 8px; border: 1px solid #14532d; background: #052e16; padding: 8px 12px;">
          <div style="color: var(--success); font-weight: bold;">✅ EXTRACTION COMPLETE</div>
          <div style="margin-top: 4px; color: #4b5563; font-size: 10px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px 12px;">
            <div>Items <span style="color: #d1d5db; font-weight: bold;">{items.length}</span></div>
            <div>Accuracy <span style="color: var(--success); font-weight: bold;">{acc.toFixed(1)}%</span></div>
            <div>Status <span style="color: var(--success);">{decision}</span></div>
            <div>Time <span style="color: #9ca3af;">{selectedJob.processing_time_seconds?.toFixed(1)}s</span></div>
            <div>Cost <span style="color: #eab308;">${selectedJob.cost_usd?.toFixed(3)}</span></div>
            <div>Pages <span style="color: #9ca3af;">{selectedJob.total_pages}</span></div>
          </div>
        </div>
      </div>
    {/if}

  {/if}
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
