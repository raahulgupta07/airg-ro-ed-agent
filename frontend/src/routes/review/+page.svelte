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


<ChapterHeading icon="checklist" title="REVIEW_QUEUE" subtitle="Triage extractions, bulk approve, and route exceptions" />

<!-- KPI strip -->
<div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
  <KpiCard title="PENDING" value="{stats.pending_review ?? '—'}" icon="rate_review" accent="var(--warning)" />
  <KpiCard title="APPROVED" value="{stats.approved ?? '—'}" icon="check_circle" accent="var(--success)" />
  <KpiCard title="REJECTED" value="{stats.rejected ?? '—'}" icon="block" accent="var(--error)" />
  <KpiCard title="AUTO_APPROVED_TODAY" value="{stats.auto_approved_today ?? '—'}" icon="bolt" accent="var(--info)" />
  <KpiCard title="DRAFT" value="{stats.draft ?? '—'}" icon="edit_note" accent="#888" />
</div>

<!-- Filter bar -->
<div class="cl-panel mb-3">
  <div class="cl-hd"><span class="dot">◉</span>Filters</div>
  <div class="cl-bd flex flex-wrap items-end gap-3">
    <div>
      <label class="cl-lbl">Status</label>
      <select bind:value={filters.status} class="cl-inp cursor-pointer" style="width: 170px;">
        <option value="pending_review">Pending review</option>
        <option value="approved">Approved</option>
        <option value="rejected">Rejected</option>
        <option value="draft">Draft</option>
      </select>
    </div>
    <div>
      <label class="cl-lbl">Importer</label>
      <input type="text" bind:value={filters.importer} placeholder="substring" class="cl-inp" style="width: 160px;" />
    </div>
    <div>
      <label class="cl-lbl">From</label>
      <input type="date" bind:value={filters.date_from} class="cl-inp" />
    </div>
    <div>
      <label class="cl-lbl">To</label>
      <input type="date" bind:value={filters.date_to} class="cl-inp" />
    </div>
    <div>
      <label class="cl-lbl">Min edits</label>
      <input type="number" min="0" bind:value={filters.min_edits} class="cl-inp" style="width: 90px;" />
    </div>
    <button class="cl-btn primary sm" onclick={applyFilters}>Apply</button>
    <button class="cl-btn sm" onclick={resetFilters}>Reset</button>
  </div>
</div>

<!-- Bulk action bar -->
<div class="flex items-center gap-3 mb-3 px-3 py-2 cl-panel" style="box-shadow: none; {selected.size > 0 ? 'background: var(--primary-tint);' : ''}">
  <button class="cl-btn sm" onclick={selectAllVisible}>
    {selected.size === queue.length && queue.length > 0 ? '☑' : '☐'}
    Select all
  </button>
  <span class="text-xs font-mono" style="color: var(--on-surface-muted);">
    {selected.size} of {queue.length} selected
  </span>
  <div class="flex-1"></div>
  <button class="cl-btn primary sm" disabled={selected.size === 0 || bulkBusy} onclick={bulkApprove}>
    ✓ Approve selected
  </button>
  <button class="cl-btn sm" disabled={selected.size === 0 || bulkBusy} onclick={openRejectModal}>
    ✗ Reject selected
  </button>
  <button class="cl-btn sm" onclick={refresh}>↻ Refresh</button>
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
  <div class="fixed bottom-4 right-4 px-4 py-2 text-xs font-medium cl-panel z-50"
    style="background: var(--surface-container); color: var(--on-surface);">{toastMsg}</div>
{/if}

{#if showRejectModal}
  <div class="fixed inset-0 z-50 flex items-center justify-center" style="background: rgba(31,30,29,0.4);">
    <div class="cl-panel w-[440px]" style="box-shadow: var(--shadow-lg);">
      <div class="cl-hd"><span class="dot">◉</span>Reject selected — {selected.size} job(s)</div>
      <div class="cl-bd">
        <label class="cl-lbl">Reason / notes (required)</label>
        <textarea bind:value={rejectNotes} rows="4" class="cl-inp" style="font-family: 'Inter', sans-serif; resize: vertical;"></textarea>
        <div class="flex justify-end gap-2 mt-3">
          <button class="cl-btn sm" onclick={() => showRejectModal = false}>Cancel</button>
          <button class="cl-btn primary sm" disabled={bulkBusy || !rejectNotes.trim()} onclick={bulkReject}>Reject</button>
        </div>
      </div>
    </div>
  </div>
{/if}
