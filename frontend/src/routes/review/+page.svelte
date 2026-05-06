<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import Button from '$lib/components/Button.svelte';
  import ExcelTable from '$lib/components/ExcelTable.svelte';
  import { getAccuracyColor } from '$lib/colors';

  type ReviewJob = {
    job_id: string;
    pdf_name: string;
    review_status: string;
    accuracy_percent?: number;
    edits_count?: number;
    created_at?: string;
    importer_name?: string;
    items_count?: number;
    username?: string;
  };

  let loading = $state(true);
  let stats = $state<any>({});
  let queue = $state<ReviewJob[]>([]);
  let filters = $state({
    status: 'pending_review',
    importer: '',
    date_from: '',
    date_to: '',
    min_edits: '',
  });
  let selected = $state<Set<string>>(new Set());
  let bulkBusy = $state(false);
  let toastMsg = $state('');
  let showRejectModal = $state(false);
  let rejectNotes = $state('');

  function toast(msg: string) {
    toastMsg = msg;
    setTimeout(() => { toastMsg = ''; }, 2400);
  }

  async function loadStats() {
    try { stats = await api.reviewStats(); } catch { stats = {}; }
  }

  async function loadQueue() {
    loading = true;
    try {
      const params: any = { status: filters.status, limit: 200 };
      if (filters.importer) params.importer = filters.importer;
      if (filters.date_from) params.date_from = filters.date_from;
      if (filters.date_to) params.date_to = filters.date_to;
      if (filters.min_edits) params.min_edits = filters.min_edits;
      const res = await api.reviewQueue(params);
      queue = res.jobs || [];
      // Drop any selections that no longer match
      const ids = new Set(queue.map(j => j.job_id));
      const next = new Set<string>();
      selected.forEach(id => { if (ids.has(id)) next.add(id); });
      selected = next;
    } catch (e: any) {
      console.error(e);
      queue = [];
    }
    loading = false;
  }

  async function refresh() {
    await Promise.all([loadStats(), loadQueue()]);
  }

  function applyFilters() { loadQueue(); }
  function resetFilters() {
    filters = { status: 'pending_review', importer: '', date_from: '', date_to: '', min_edits: '' };
    loadQueue();
  }

  function toggleSelect(jobId: string) {
    const next = new Set(selected);
    if (next.has(jobId)) next.delete(jobId); else next.add(jobId);
    selected = next;
  }

  function selectAllVisible() {
    if (selected.size === queue.length && queue.length > 0) {
      selected = new Set();
    } else {
      selected = new Set(queue.map(j => j.job_id));
    }
  }

  async function bulkApprove() {
    if (selected.size === 0) return;
    if (!confirm(`Approve ${selected.size} job(s)?`)) return;
    bulkBusy = true;
    try {
      const r = await api.reviewBulkApprove([...selected]);
      toast(`Approved ${r.approved?.length || 0} · failed ${r.failed?.length || 0}`);
      selected = new Set();
      await refresh();
    } catch (e: any) {
      toast(`Error: ${e.message || e}`);
    }
    bulkBusy = false;
  }

  function openRejectModal() {
    if (selected.size === 0) return;
    rejectNotes = '';
    showRejectModal = true;
  }

  async function bulkReject() {
    if (!rejectNotes.trim()) { toast('Notes required'); return; }
    bulkBusy = true;
    try {
      const r = await api.reviewBulkReject([...selected], rejectNotes.trim());
      toast(`Rejected ${r.rejected?.length || 0} · failed ${r.failed?.length || 0}`);
      selected = new Set();
      showRejectModal = false;
      await refresh();
    } catch (e: any) {
      toast(`Error: ${e.message || e}`);
    }
    bulkBusy = false;
  }

  function openJob(jobId: string) {
    goto(`/review/${jobId}`);
  }

  // ── ExcelTable column config ──
  const columns = [
    {
      id: 'sel',
      header: '',
      width: 40,
      enableSort: false,
      cell: (r: ReviewJob) => selected.has(r.job_id) ? '☑' : '☐',
      align: 'center' as const,
    },
    {
      id: 'review_status',
      header: 'STATUS',
      accessor: 'review_status',
      width: 110,
      cell: (r: ReviewJob) => (r.review_status || '').toUpperCase(),
    },
    { id: 'job_id', header: 'JOB_ID', accessor: 'job_id', width: 180 },
    { id: 'pdf_name', header: 'FILE', accessor: 'pdf_name', width: 220 },
    {
      id: 'importer_name', header: 'IMPORTER', accessor: 'importer_name', width: 160,
      cell: (r: ReviewJob) => r.importer_name || '—',
    },
    { id: 'items_count', header: 'ITEMS', accessor: 'items_count', width: 70, align: 'right' as const,
      cell: (r: ReviewJob) => r.items_count ?? 0 },
    { id: 'edits_count', header: 'EDITS', accessor: 'edits_count', width: 70, align: 'right' as const,
      cell: (r: ReviewJob) => r.edits_count ?? 0 },
    {
      id: 'accuracy_percent',
      header: 'CONF',
      accessor: 'accuracy_percent',
      width: 80,
      align: 'right' as const,
      cell: (r: ReviewJob) => `${(r.accuracy_percent ?? 0).toFixed(0)}%`,
    },
    {
      id: 'created_at', header: 'CREATED', accessor: 'created_at', width: 120,
      cell: (r: ReviewJob) => (r.created_at || '').slice(0, 16),
    },
  ];

  function rowClick(row: ReviewJob, evt?: MouseEvent) {
    // Click on first col → toggle select; otherwise open job
    // ExcelTable doesn't expose col, so we rely on the explicit checkbox cell display
    // Use shift/ctrl to toggle, plain click opens the job
    if (evt && (evt.ctrlKey || evt.metaKey || evt.shiftKey)) {
      toggleSelect(row.job_id);
    } else {
      openJob(row.job_id);
    }
  }

  onMount(refresh);
</script>

<div class="w-full max-w-6xl mx-auto mb-4 p-4 rounded border-2 border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30">
  <div class="flex items-start gap-3 flex-wrap">
    <span class="text-2xl">⚠️</span>
    <div class="flex-1 min-w-[200px]">
      <div class="font-bold text-base">Review Queue</div>
      <div class="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
        Triage extracted jobs. Approve, reject, or open a single job for side-by-side correction.
        Hold ⌘/Ctrl + click rows to multi-select.
      </div>
    </div>
  </div>
</div>

<ChapterHeading icon="checklist" title="REVIEW_QUEUE" subtitle="Triage extractions, bulk approve, and route exceptions" />

<!-- KPI strip -->
<div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
  <KpiCard title="PENDING" value="{stats.pending_review ?? '—'}" icon="rate_review" accent="#ff9d00" />
  <KpiCard title="APPROVED" value="{stats.approved ?? '—'}" icon="check_circle" accent="#007518" />
  <KpiCard title="REJECTED" value="{stats.rejected ?? '—'}" icon="block" accent="#be2d06" />
  <KpiCard title="AUTO_APPROVED_TODAY" value="{stats.auto_approved_today ?? '—'}" icon="bolt" accent="#006f7c" />
  <KpiCard title="DRAFT" value="{stats.draft ?? '—'}" icon="edit_note" accent="#888" />
</div>

<!-- Filter bar -->
<div class="border-2 stamp-shadow mb-3" style="border-color: var(--on-surface); background: var(--surface);">
  <div class="dark-bar text-xs">FILTERS</div>
  <div class="bg-white p-2 flex flex-wrap items-center gap-2">
    <span class="text-[8px] font-black uppercase opacity-60 px-1">STATUS</span>
    <select bind:value={filters.status}
      class="text-[10px] font-mono px-2 py-1.5 cursor-pointer"
      style="border: 2px solid var(--on-surface); background: white;">
      <option value="pending_review">PENDING_REVIEW</option>
      <option value="approved">APPROVED</option>
      <option value="rejected">REJECTED</option>
      <option value="draft">DRAFT</option>
    </select>

    <span class="text-[8px] font-black uppercase opacity-60 px-1">IMPORTER</span>
    <input type="text" bind:value={filters.importer} placeholder="substring"
      class="text-[10px] font-mono px-2 py-1.5"
      style="border: 2px solid var(--on-surface); background: white; width: 160px;" />

    <span class="text-[8px] font-black uppercase opacity-60 px-1">FROM</span>
    <input type="date" bind:value={filters.date_from}
      class="text-[10px] font-mono px-2 py-1.5"
      style="border: 2px solid var(--on-surface); background: white;" />

    <span class="text-[8px] font-black uppercase opacity-60 px-1">TO</span>
    <input type="date" bind:value={filters.date_to}
      class="text-[10px] font-mono px-2 py-1.5"
      style="border: 2px solid var(--on-surface); background: white;" />

    <span class="text-[8px] font-black uppercase opacity-60 px-1">MIN_EDITS</span>
    <input type="number" min="0" bind:value={filters.min_edits}
      class="text-[10px] font-mono px-2 py-1.5"
      style="border: 2px solid var(--on-surface); background: white; width: 70px;" />

    <Button size="sm" variant="primary" onclick={applyFilters}>APPLY</Button>
    <Button size="sm" variant="secondary" onclick={resetFilters}>RESET</Button>
  </div>
</div>

<!-- Bulk action bar -->
<div class="flex items-center gap-3 mb-2 p-2 border-2"
  style="border-color: var(--on-surface); background: {selected.size > 0 ? '#fff8d8' : 'transparent'};">
  <button class="text-[10px] font-bold uppercase px-2 py-1 cursor-pointer"
    style="border: 2px solid var(--on-surface); background: white;"
    onclick={selectAllVisible}>
    {selected.size === queue.length && queue.length > 0 ? '☑' : '☐'}
    SELECT_ALL
  </button>
  <span class="text-[10px] font-mono opacity-70">
    {selected.size} of {queue.length} selected
  </span>
  <div class="flex-1"></div>
  <Button size="sm" variant="primary" disabled={selected.size === 0 || bulkBusy} onclick={bulkApprove}>
    ✓ APPROVE_SELECTED
  </Button>
  <Button size="sm" variant="secondary" disabled={selected.size === 0 || bulkBusy} onclick={openRejectModal}>
    ✗ REJECT_SELECTED
  </Button>
  <Button size="sm" variant="ghost" onclick={refresh}>↻ REFRESH</Button>
</div>

{#if loading}
  <div class="flex items-center justify-center p-12">
    <div class="agent-spinner" style="border-color: var(--secondary); border-top-color: transparent;"></div>
    <span class="ml-3 text-sm font-bold uppercase">LOADING...</span>
  </div>
{:else}
  <ExcelTable
    title="REVIEW_QUEUE"
    columns={columns}
    data={queue}
    maxHeight="520px"
    exportFilename="review_queue.csv"
    rowClass={(r: ReviewJob) => selected.has(r.job_id) ? 'bg-yellow-100' : ''}
    onRowClick={(r: any) => rowClick(r)}
  />
  <div class="text-[10px] font-mono opacity-60 mt-2">
    Click a row to open the side-by-side reviewer. Ctrl/⌘+click to toggle selection.
  </div>
{/if}

{#if toastMsg}
  <div class="fixed bottom-4 right-4 px-4 py-2 text-xs font-bold uppercase border-2 stamp-shadow z-50"
    style="background: var(--on-surface); color: var(--surface);">{toastMsg}</div>
{/if}

{#if showRejectModal}
  <div class="fixed inset-0 z-50 flex items-center justify-center" style="background: rgba(0,0,0,0.5);">
    <div class="border-2 stamp-shadow w-[420px] p-4" style="background: var(--surface); border-color: var(--on-surface);">
      <div class="dark-bar text-xs mb-3">REJECT_SELECTED — {selected.size} job(s)</div>
      <label class="text-[10px] font-bold uppercase opacity-70">REASON / NOTES (required)</label>
      <textarea bind:value={rejectNotes} rows="4"
        class="w-full text-xs font-mono p-2 mt-1"
        style="border: 2px solid var(--on-surface); background: white;"></textarea>
      <div class="flex justify-end gap-2 mt-3">
        <Button size="sm" variant="ghost" onclick={() => showRejectModal = false}>CANCEL</Button>
        <Button size="sm" variant="primary" disabled={bulkBusy || !rejectNotes.trim()} onclick={bulkReject}>
          REJECT
        </Button>
      </div>
    </div>
  </div>
{/if}
