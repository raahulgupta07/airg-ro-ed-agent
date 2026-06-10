<script lang="ts">
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api, extractPDF, normalizeExtractResult } from '$lib/api';
  import type { PipelineKey } from '$lib/pipelineConfig';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import Button from '$lib/components/Button.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import QueueItem from '$lib/components/QueueItem.svelte';
  import RecentJobs from '$lib/components/RecentJobs.svelte';
  import ResultAccordion from '$lib/components/ResultAccordion.svelte';
  import ReviewSplitView from '$lib/components/ReviewSplitView.svelte';
  import PipelineVisualizer from '$lib/components/PipelineVisualizer.svelte';
  import AgentTerminal from '$lib/components/AgentTerminal.svelte';
  import { getAccuracyColor } from '$lib/colors';


  // ── Types ──
  type FileEntry = {
    file: File;
    filename: string;
    size: number;
    savedPath: string;
    isDuplicate: boolean;
    canReprocess: boolean;
    existingJob: any;
    status: 'queued' | 'processing' | 'done' | 'error' | 'stopped' | 'duplicate';
    progress: number;
    stepLabel: string;
    jobId: string;
    accuracy: number;
    itemsCount: number;
    cost: number;
    duration: number;
    gateLog: string[];
  };

  type StepMsg = { step: number; name: string; status: string; detail?: string; duration?: number; cost?: number };

  // ── State ──
  let fileInput: HTMLInputElement;
  let queue = $state<FileEntry[]>([]);
  let selectedIndex = $state<number>(-1);
  let pipelineSteps = $state<StepMsg[]>([]);
  let running = $state(false);
  let batchSummary = $state<any>(null);
  let jobResults = $state<Record<string, any>>({});
  let loadingResult = $state(false);
  let terminalLogs = $state<{ time: string; agent: string; text: string; type: string }[]>([]);
  let terminalSteps = $state<any[]>([]);
  let vizSteps = $state<any[]>([]);
  let vizSummary = $state<any>(null);
  let resultTab = $state<'results' | 'log'>('results');
  let showReprocessConfirm = $state(false);
  let terminalComplete = $state(false);
  let terminalSummary = $state<any>(null);
  let terminalCollapsed = $state(false);
  let agentLines = $state<{ text: string; type: string }[]>([]);
  let pipelineMode = $state('ro_ed');
  let selectedPipeline = $state<PipelineKey>('v11');
  // V12: typed-page engine — 'classic' (V7 Veritas) | 'presto' (V12 fast) | 'auto'
  let selectedEngine = $state<'auto' | 'presto' | 'classic' | 'atlas'>('atlas');
  // Engine availability — super-admin controls this in Settings; default ATLAS only.
  const ENGINE_OPTIONS: [string,string,string][] = [
    ['auto','AUTO','admin default'],['classic','ATLAS CLASSIC','Gen 1 · legacy'],
    ['presto','ATLAS V14-1 SWIFT','V14-1 · fast typed'],['atlas','ATLAS V14','V14 · flagship'],
  ];
  let enabledEngines = $state<string[]>(['atlas']);
  const visibleEngines = $derived(ENGINE_OPTIONS.filter(o => enabledEngines.includes(o[0])));
  // V11 review mode toggle: VIEW (read-only ResultAccordion) | REVIEW (editable split view)
  let reviewMode = $state<'view' | 'review'>('view');
  function reviewToast(msg: string) {
    // ReviewSplitView shows its own toast; this is just a no-op safety hook
    void msg;
  }
  const isProcessing = $derived(running);

  // Route extraction through V11 (queue-based HTTP path).
  async function runExtract(file: File, jobId?: string) {
    const raw = await extractPDF(file, selectedPipeline, auth.token, jobId, selectedEngine);
    return normalizeExtractResult(raw);
  }

  // Stable client-side job id (used to subscribe to V11 SSE stream BEFORE
  // the HTTP /api/extract-v11 call returns). Sent as `job_id` form field so
  // the backend uses the same id and the SSE endpoint resolves correctly.
  function makeJobId(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
      return (crypto as any).randomUUID();
    }
    return 'v11-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  // Job id of the currently-streaming V11 run (drives AgentTerminal SSE).
  let streamingJobId = $state<string | null>(null);

  // Explicit view mode — bypasses derived reactivity issues
  let viewMode = $state<'idle' | 'pipeline' | 'results' | 'batch'>('idle');

  // ── Persist queue to localStorage (survives refresh, tab close, navigation) ──
  const QUEUE_KEY = 'ro_ed_agent_queue';
  const SEL_KEY = 'ro_ed_agent_sel';

  function saveQueueState() {
    try {
      const serializable = queue.map(q => ({
        filename: q.filename, size: q.size, savedPath: q.savedPath,
        isDuplicate: q.isDuplicate, canReprocess: q.canReprocess,
        existingJob: q.existingJob, status: q.status, progress: q.progress,
        stepLabel: q.stepLabel, jobId: q.jobId, accuracy: q.accuracy,
        itemsCount: q.itemsCount, cost: q.cost, duration: q.duration, gateLog: q.gateLog,
      }));
      localStorage.setItem(QUEUE_KEY, JSON.stringify(serializable));
      localStorage.setItem(SEL_KEY, String(selectedIndex));
    } catch {}
  }

  function restoreQueueState(): boolean {
    try {
      const saved = localStorage.getItem(QUEUE_KEY);
      if (!saved) return false;
      const parsed = JSON.parse(saved) as any[];
      if (!parsed.length) return false;
      queue = parsed.map(q => ({ ...q, file: new File([], q.filename) }));
      const selIdx = parseInt(localStorage.getItem(SEL_KEY) || '-1');
      selectedIndex = selIdx >= 0 && selIdx < queue.length ? selIdx : 0;
      // Load results for completed jobs
      for (const entry of queue) {
        if (entry.jobId && entry.status === 'done') loadJobResult(entry.jobId);
      }
      return true;
    } catch { return false; }
  }

  // Save queue whenever it changes — skip until after mount to avoid clearing before restore
  let mounted = $state(false);
  $effect(() => {
    if (!mounted) return;
    if (queue.length > 0) {
      saveQueueState();
    } else {
      try { localStorage.removeItem(QUEUE_KEY); localStorage.removeItem(SEL_KEY); } catch {}
    }
  });

  // Pipeline mode fixed — no persistence needed

  // ── Derived ──
  const selectedFile = $derived(queue[selectedIndex] ?? null);
  const selectedJob = $derived(selectedFile?.jobId ? jobResults[selectedFile.jobId] : null);
  const doneCount = $derived(queue.filter(f => f.status === 'done').length);
  const totalCount = $derived(queue.length);

  // ── Upload ──
  async function handleFiles(e: Event) {
    const input = e.target as HTMLInputElement;
    if (!input.files?.length) return;

    const newFiles = Array.from(input.files).filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (!newFiles.length) return;

    // Upload all files
    const form = new FormData();
    for (const f of newFiles) form.append('files', f);

    const res = await fetch('/api/jobs/upload-batch', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${auth.token}` },
      body: form,
    });
    const uploads: any[] = await res.text().then(t => JSON.parse(t));

    for (let i = 0; i < uploads.length; i++) {
      const u = uploads[i];
      if (u.error) continue;
      queue.push({
        file: newFiles[i],
        filename: u.filename,
        size: u.file_size,
        savedPath: u.saved_path,
        isDuplicate: u.is_duplicate,
        canReprocess: u.can_reprocess,
        existingJob: u.existing_job,
        status: u.is_duplicate ? 'duplicate' : 'queued',
        progress: 0,
        stepLabel: '',
        jobId: '',
        accuracy: 0,
        itemsCount: 0,
        cost: 0,
        duration: 0,
        gateLog: [],
      });
    }
    queue = [...queue];

    // Auto-select first if none selected
    if (selectedIndex < 0 && queue.length > 0) selectedIndex = 0;

    // Reset file input
    input.value = '';
  }

  // ── Execute ──
  function startPipeline() {
    const filesToProcess = queue.filter(f => f.status === 'queued' || f.status === 'duplicate');
    if (!filesToProcess.length) return;

    // Mark duplicates as queued (reprocess)
    for (const f of queue) {
      if (f.status === 'duplicate') f.status = 'queued';
    }
    queue = [...queue];

    running = true;
    viewMode = 'pipeline';
    batchSummary = null;
    pipelineSteps = [];
    terminalLogs = [];
    terminalSteps = [];
    terminalComplete = false;
    terminalSummary = null;
    terminalCollapsed = false;
    agentLines = [];
    vizSteps = [];
    vizSummary = null;
    streamingJobId = null;

    runHttpPipeline(filesToProcess);
  }

  // ── HTTP pipeline path (V11 queue-based) ──
  // V11 is now QUEUE-BASED: /api/extract-v11 returns 202 + {job_id, stream_id}
  // and we poll /api/extract-v11/status/{stream_id} until finished/failed.
  // V10 (and others) keep the old synchronous flow.
  async function runHttpPipeline(filesToProcess: FileEntry[]) {
    for (const entry of filesToProcess) {
      const queueIdx = queue.indexOf(entry);
      if (queueIdx < 0) continue;
      const preAllocId = selectedPipeline === 'v11' ? makeJobId() : '';
      queue[queueIdx] = {
        ...entry,
        status: 'processing',
        stepLabel: selectedPipeline === 'v11'
          ? 'QUEUED — waiting for worker...'
          : `${selectedPipeline.toUpperCase()} extracting...`,
        progress: selectedPipeline === 'v11' ? 5 : 30,
        jobId: preAllocId || entry.jobId,
      };
      queue = [...queue];
      selectedIndex = queueIdx;
      if (preAllocId) streamingJobId = preAllocId;
      try {
        // Submit. V11 returns 202 + {job_id, stream_id}; V10 returns full result.
        const submitRes = await runExtract(entry.file, preAllocId || undefined);

        if (selectedPipeline === 'v11') {
          // ── Queue-based path: poll status until terminal state ──
          const streamId: string = submitRes?.stream_id || preAllocId;
          const dbJobId: string = submitRes?.job_id || preAllocId;

          let pollAttempts = 0;
          const MAX_POLL = 600;  // 600 × 3s = 30 min ceiling
          let terminal = false;

          while (pollAttempts < MAX_POLL) {
            await new Promise(r => setTimeout(r, 3000));
            pollAttempts++;
            try {
              const headers: Record<string, string> = {};
              if (auth.token) headers['Authorization'] = `Bearer ${auth.token}`;
              const r = await fetch(`/api/extract-v11/status/${streamId}`, { headers });
              const txt = await r.text();
              const statusRes = JSON.parse(txt);

              const sLabel = statusRes.status === 'queued'
                ? `QUEUED (pos: ${statusRes.queue_position ?? '?'})`
                : statusRes.status === 'started' ? 'EXTRACTING...'
                : statusRes.status === 'finished' ? 'DONE'
                : statusRes.status === 'failed' ? 'FAILED'
                : (statusRes.status?.toUpperCase?.() || 'PROCESSING');
              const sProgress = statusRes.status === 'started' ? 50
                : statusRes.status === 'finished' ? 95
                : statusRes.status === 'failed' ? 100
                : 10;
              queue[queueIdx] = { ...queue[queueIdx], stepLabel: sLabel, progress: sProgress };
              queue = [...queue];

              if (statusRes.status === 'finished') {
                // Worker creates a fresh DB row inside V11._save_to_db.
                // Prefer the worker's result.job_id (real data row); fall back to pre-created dbJobId.
                const realJobId = statusRes?.result?.job_id || dbJobId;
                const job = await api.getJob(realJobId);
                jobResults[realJobId] = job;
                jobResults = { ...jobResults };
                queue[queueIdx] = {
                  ...queue[queueIdx],
                  status: 'done',
                  jobId: realJobId,
                  accuracy: job?.accuracy_percent || 0,
                  itemsCount: job?.items?.length || 0,
                  cost: job?.cost_usd || 0,
                  duration: job?.processing_time_seconds || 0,
                  progress: 100,
                  stepLabel: '',
                };
                queue = [...queue];
                viewMode = 'results';
                terminal = true;
                break;
              } else if (statusRes.status === 'failed') {
                queue[queueIdx] = {
                  ...queue[queueIdx],
                  status: 'error',
                  progress: 100,
                  stepLabel: (statusRes.error || 'WORKER FAILED').slice(0, 80),
                };
                queue = [...queue];
                terminal = true;
                break;
              }
            } catch {
              // network blip — keep polling
            }
          }

          if (!terminal && pollAttempts >= MAX_POLL) {
            queue[queueIdx] = {
              ...queue[queueIdx],
              status: 'error',
              progress: 100,
              stepLabel: 'TIMEOUT — check /history',
            };
            queue = [...queue];
          }
        } else {
          // ── Legacy sync path (V10 etc.): result returned directly ──
          const job = submitRes;
          const jobId = job?.job_id || preAllocId || `${selectedPipeline}-${Date.now()}`;
          jobResults[jobId] = job;
          jobResults = { ...jobResults };
          queue[queueIdx] = {
            ...queue[queueIdx],
            status: 'done',
            jobId,
            accuracy: job?.accuracy_percent || 0,
            itemsCount: job?.items?.length || 0,
            cost: job?.cost_usd || 0,
            duration: job?.processing_time_seconds || 0,
            progress: 100,
            stepLabel: '',
          };
          queue = [...queue];
          viewMode = 'results';
        }
      } catch (e: any) {
        queue[queueIdx] = {
          ...queue[queueIdx],
          status: 'error',
          progress: 100,
          stepLabel: e?.message?.slice(0, 80) || 'EXTRACT FAILED',
        };
        queue = [...queue];
      }
    }
    running = false;
    // Stop SSE stream once HTTP path resolves (DONE/FAIL events should have
    // already closed it; this is a safety net for connections still open).
    streamingJobId = null;
  }

  // ── Stop ──
  function stopPipeline() {
    // V11 queue-based path — no WebSocket. Stopping is not yet wired through
    // the queue. Mark UI as not running so the user can re-execute.
    running = false;
  }

  // ── Load job results ──
  let loadError = $state('');

  async function loadJobResult(jobId: string) {
    if (jobResults[jobId]) { loadingResult = false; return; }
    loadingResult = true;
    loadError = '';

    // Retry up to 3 times with increasing delay
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const job = await api.getJob(jobId);
        if (job) {
          jobResults[jobId] = job;
          jobResults = { ...jobResults };
          loadingResult = false;
          return;
        }
      } catch (e: any) {
        console.error(`loadJobResult attempt ${attempt}/3 failed:`, e?.message || e);
        if (attempt < 3) {
          await new Promise(r => setTimeout(r, attempt * 1000)); // 1s, 2s delay
        } else {
          loadError = e?.message || 'Failed to load results';
        }
      }
    }
    loadingResult = false;
  }

  // ── Select file in queue ──
  function selectFile(idx: number) {
    selectedIndex = idx;
    pipelineSteps = [];
    const entry = queue[idx];
    if (entry?.status === 'done' || entry?.status === 'error') {
      viewMode = 'results';
    } else if (entry?.status === 'processing') {
      viewMode = 'pipeline';
    } else {
      viewMode = 'idle';
    }
    if (entry?.jobId) loadJobResult(entry.jobId);
    if (entry?.existingJob?.job_id) loadJobResult(entry.existingJob.job_id);
  }

  // ── View existing duplicate result ──
  function viewDuplicateResult(idx: number) {
    const entry = queue[idx];
    if (entry?.existingJob?.job_id) {
      entry.jobId = entry.existingJob.job_id;
      entry.status = 'done';
      entry.accuracy = entry.existingJob.accuracy_percent || 0;
      queue = [...queue];
      selectedIndex = idx;
      loadJobResult(entry.existingJob.job_id);
    }
  }

  // On mount: restore queue (V11 SSE handles live updates).
  onMount(async () => {
    // Load which engines the admin enabled + the default selection.
    try {
      const cfg = await api.getEngines();
      if (cfg?.enabled?.length) enabledEngines = cfg.enabled;
      if (cfg?.default) selectedEngine = cfg.default;
    } catch {}

    // Try restoring saved queue state first
    const restored = restoreQueueState();

    if (restored) {
      // Fix stale processing items
      let changed = false;
      for (const entry of queue) {
        if (entry.status === 'processing') {
          // Mark all stale processing entries — they'll be re-checked via poll if they have jobId
          if (!entry.jobId) {
            entry.status = 'error';
            entry.stepLabel = 'INTERRUPTED — re-upload to retry';
            changed = true;
          }
        }
      }
      // Remove entries stuck in processing with no jobId (truly orphaned)
      const before = queue.length;
      queue = queue.filter(q => !(q.status === 'error' && q.stepLabel?.includes('INTERRUPTED')));
      if (queue.length !== before) changed = true;
      if (changed) queue = [...queue];
      // If queue is now empty after cleanup, clear storage and skip restore
      if (queue.length === 0) {
        try { localStorage.removeItem(QUEUE_KEY); localStorage.removeItem(SEL_KEY); } catch {}
        // Fall through to check DB for in-progress jobs
      }

      // V11 SSE handles live status; nothing to poll on restore.
      // Load results for completed items
      for (const entry of queue) {
        if (entry.status === 'done' && entry.jobId && !jobResults[entry.jobId]) {
          loadJobResult(entry.jobId);
        }
      }
      return;
    }

    // Otherwise check for in-progress jobs from DB
    try {
      const res = await fetch('/api/jobs/processing', {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (res.ok) {
        const processing = await res.text().then(t => JSON.parse(t));
        if (processing.length > 0) {
          for (const job of processing) {
            queue.push({
              file: new File([], job.pdf_name),
              filename: job.pdf_name,
              size: job.pdf_size || 0,
              savedPath: '',
              isDuplicate: false,
              canReprocess: false,
              existingJob: null,
              status: 'processing',
              progress: 50,
              stepLabel: 'PROCESSING...',
              jobId: job.job_id,
              accuracy: 0,
              itemsCount: 0,
              cost: 0,
              duration: 0,
              gateLog: [],
            });
          }
          queue = [...queue];
          if (queue.length > 0) selectedIndex = 0;
        }
      }
    } catch {}

    // Enable queue persistence now that restore is done
    mounted = true;
  });


  // ── Clear ──
  function clearAll() {
    // Clear storage FIRST to prevent $effect from re-saving
    try { localStorage.removeItem(QUEUE_KEY); localStorage.removeItem(SEL_KEY); } catch {}
    queue = [];
    selectedIndex = -1;
    pipelineSteps = [];
    batchSummary = null;
    jobResults = {};
    terminalLogs = [];
    viewMode = 'idle';
  }
</script>

<!-- Hidden file input (shared across both states) -->
<input type="file" accept=".pdf" multiple class="hidden" bind:this={fileInput} onchange={handleFiles} />

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- STATE 1: EMPTY — Full-width hero drop zone                -->
<!-- ═══════════════════════════════════════════════════════════ -->
{#if queue.length === 0}
  <div class="flex flex-col items-center justify-center" style="min-height: calc(100vh - 180px);">
    <!-- Smart Router info card (auto-routing, no choice exposed) -->
    <div class="cl-panel w-full max-w-4xl mb-5">
      <div class="cl-bd">
      <div class="flex items-start gap-3 flex-wrap">
        <span class="material-symbols-outlined" style="color: var(--primary); font-size: 22px;">auto_awesome</span>
        <div class="flex-1 min-w-[200px]">
          <div class="font-serif text-base" style="color: var(--on-surface); font-weight: 500;">Atlas Router <span style="color: var(--on-surface-muted); font-weight: 400;">· auto</span></div>
          <div class="text-sm mt-1" style="color: var(--on-surface-muted); line-height: 1.5;">
            Auto-classifies each page → PRINTED runs Atlas Swift, INKED runs Atlas Vision, EXTRA attachments skipped. Best for any doc — printed, inked, or mixed.
          </div>
        </div>
        <div class="flex gap-4 text-xs" style="color: var(--on-surface-muted);">
          <div class="flex items-center gap-1"><span class="material-symbols-outlined text-sm" style="color: var(--primary);">payments</span>$0.08–0.40</div>
          <div class="flex items-center gap-1"><span class="material-symbols-outlined text-sm" style="color: var(--primary);">schedule</span>60–150s</div>
        </div>
      </div>
      <!-- Engine selector -->
      <div class="mt-3 pt-3" style="border-top: 1px solid var(--line-2);">
        <div class="text-[11px] uppercase tracking-wider mb-1.5" style="color: var(--on-surface-muted);">Engine</div>
        <div class="flex gap-2 flex-wrap">
          {#each visibleEngines as opt}
            <button
              type="button"
              class="px-3 py-1.5 cursor-pointer transition-all text-left"
              style="border: 1.5px solid {selectedEngine === opt[0] ? 'var(--primary)' : 'var(--line)'}; border-radius: var(--radius-md); background: {selectedEngine === opt[0] ? 'var(--primary-tint)' : 'var(--surface-container-lowest)'}; {selectedEngine === opt[0] ? 'box-shadow: 0 0 0 3px var(--primary-tint);' : ''}"
              onclick={() => (selectedEngine = opt[0] as 'auto' | 'presto' | 'classic' | 'atlas')}>
              <div class="flex items-center gap-1.5">
                <div class="text-xs font-bold" style="color: var(--on-surface);">{opt[1]}</div>
                {#if selectedEngine === opt[0]}<span class="pill clay" style="padding: 1px 7px; font-size: 9px;">SELECTED</span>{/if}
              </div>
              <div class="text-[10px]" style="color: var(--on-surface-muted);">{opt[2]}</div>
            </button>
          {/each}
        </div>
      </div>
      </div>
    </div>
    <!-- Big drop zone -->
    <button
      class="cl-drop w-full max-w-4xl group"
      style="padding: 64px;"
      onclick={() => fileInput.click()}
    >
      <div class="text-center">
        <span class="material-symbols-outlined" style="font-size: 4rem; color: var(--primary);">cloud_upload</span>
        <div class="mt-4 font-serif text-2xl" style="color: var(--on-surface); font-weight: 500; letter-spacing: -0.01em;">
          Drop customs PDFs here
        </div>
        <div class="mt-1.5 text-sm" style="color: var(--on-surface-muted);">
          or click to browse
        </div>
        <div class="mt-5 flex items-center justify-center gap-3 text-xs" style="color: var(--on-surface-subtle);">
          <span>Single or multiple files</span>
          <span>·</span>
          <span>.pdf up to 50 MB each</span>
          <span>·</span>
          <span>Batch processing supported</span>
        </div>
      </div>
    </button>


    <!-- Discovery UI removed -->


    <!-- Recent jobs below -->
    <div class="w-full max-w-4xl mt-6">
      <RecentJobs onselect={(jobId) => { loadJobResult(jobId); selectedIndex = -2; }} />
    </div>
  </div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- STATE 2: FILES LOADED — Split layout                      -->
<!-- ═══════════════════════════════════════════════════════════ -->
{:else}
  <ChapterHeading
    icon="description"
    title="DOCUMENT_INTELLIGENCE"
    subtitle="Upload customs PDFs and extract structured data"
    question="Drop one or multiple PDFs to start extraction"
  />

  {@const _hideQueueForReview = !!selectedJob && selectedPipeline === 'v11' && !running}
  <div class={_hideQueueForReview ? 'grid grid-cols-1 gap-4' : 'grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-4'} style="min-height: calc(100vh - 280px); overflow-x: hidden;">

    <!-- ═══════════ LEFT PANEL ═══════════ -->
    {#if !_hideQueueForReview}
    <div class="flex flex-col">
      <!-- Smart Router info banner (compact for left panel) -->
      <div class="mb-3 px-3 py-2 flex items-center gap-2"
           style="background: var(--primary-tint); border: 1px solid var(--line); border-radius: var(--radius-md);">
        <span class="material-symbols-outlined text-sm" style="color: var(--primary);">auto_awesome</span>
        <span class="text-xs font-medium" style="color: var(--on-surface);">Atlas Router</span>
        <span class="text-[11px] flex-1" style="color: var(--on-surface-muted);">auto · PRINTED→Atlas Swift · INKED→Atlas Vision</span>
        <span class="text-[11px]" style="color: var(--on-surface-muted);">$0.08–0.40</span>
      </div>
      <!-- V12 engine selector: typed-page extraction engine -->
      <div class="mb-3">
        <div class="text-[10px] uppercase tracking-wider mb-1" style="color: var(--on-surface-muted);">Typed-page engine</div>
        <div class="flex gap-1">
          {#each visibleEngines as opt}
            <button
              class="flex-1 px-2 py-1.5 cursor-pointer transition-all text-center"
              style="border: 1.5px solid {selectedEngine === opt[0] ? 'var(--primary)' : 'var(--line)'}; border-radius: var(--radius-md); background: {selectedEngine === opt[0] ? 'var(--primary-tint)' : 'var(--surface-container-lowest)'}; {selectedEngine === opt[0] ? 'box-shadow: 0 0 0 3px var(--primary-tint);' : ''}"
              onclick={() => (selectedEngine = opt[0] as 'auto' | 'presto' | 'classic' | 'atlas')}>
              <div class="text-[11px] font-bold" style="color: var(--on-surface);">{opt[1]}</div>
              <div class="text-[9px]" style="color: var(--on-surface-muted);">{opt[2]}</div>
            </button>
          {/each}
        </div>
      </div>
      <!-- Add more / New job buttons -->
      <div class="flex gap-2 mb-3">
        <button
          class="cl-drop flex-1"
          style="padding: 10px 12px;"
          onclick={() => fileInput.click()}
        >
          <div class="flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base" style="color: var(--primary);">add_circle</span>
            <span class="text-xs font-medium" style="color: var(--on-surface);">Add more PDFs</span>
          </div>
        </button>
        <button
          class="px-3 py-2.5 cursor-pointer press-effect transition-colors"
          style="background: var(--primary); color: #fff; border-radius: var(--radius-md); box-shadow: var(--shadow-xs);"
          onclick={clearAll}
        >
          <div class="flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base">restart_alt</span>
            <span class="text-xs font-medium">New job</span>
          </div>
        </button>
      </div>

      <!-- Queue -->
      <div class="cl-panel flex-1 flex flex-col">
        <div class="cl-hd">
          <span class="dot">◉</span>Queue
          <span class="ct">{doneCount}/{totalCount}</span>
        </div>

        <!-- Queue list -->
        <div class="flex-1 overflow-y-auto custom-scrollbar" style="max-height: 400px; background: var(--surface-container-lowest);">
          {#each queue as entry, i}
            <QueueItem
              filename={entry.filename}
              size={entry.size}
              items={entry.itemsCount}
              accuracy={entry.accuracy}
              status={entry.status}
              progress={entry.progress}
              stepLabel={entry.stepLabel}
              selected={selectedIndex === i}
              onclick={() => selectFile(i)}
            />
          {/each}
        </div>

        <!-- Pipeline Mode -->
        <div class="px-3 pt-2 flex items-center gap-2" style="border-top: 1px solid var(--line-2);">
          <span class="pill ok" style="padding: 2px 8px; font-size: 9px;">RO-ED AI</span>
          <span class="text-[7px] font-mono" style="color: var(--on-surface-subtle);">SMART EXTRACTION · HD VISION</span>
        </div>

        <!-- Actions -->
        <div class="p-2 flex gap-2">
          {#if running}
            <Button variant="danger" size="sm" onclick={stopPipeline}>
              <span class="flex items-center gap-1">
                <span class="material-symbols-outlined text-xs">stop_circle</span> STOP
              </span>
            </Button>
          {:else}
            {#if queue.some(f => f.status === 'queued' || f.status === 'duplicate')}
              <Button variant="primary" size="sm" onclick={startPipeline}>
                <span class="flex items-center gap-1">
                  <span class="material-symbols-outlined text-xs">play_arrow</span> EXECUTE ({queue.filter(f => f.status === 'queued' || f.status === 'duplicate').length})
                </span>
              </Button>
            {/if}
            <Button variant="dark" size="sm" onclick={clearAll}>CLEAR</Button>
          {/if}
        </div>
      </div>

      <!-- Duplicate actions for selected file -->
      {#if selectedFile?.status === 'duplicate'}
        {@const ej = selectedFile.existingJob}
        <div class="cl-panel mt-2">
          <div class="cl-hd">
            <span class="dot" style="color: var(--warning);">◉</span>This Document Was Already Processed
          </div>
          <div class="cl-bd space-y-3">
            <!-- Previous result info -->
            <div class="p-2" style="border: 1px solid var(--line-2); border-radius: var(--radius-sm); background: var(--surface-container-low);">
              <div class="text-[9px] font-bold uppercase" style="color: var(--on-surface-subtle);">PREVIOUS EXTRACTION</div>
              <div class="mt-1 grid grid-cols-3 gap-2 text-[10px]" style="color: var(--on-surface);">
                <div>Processed: <span class="font-bold">{ej?.created_at?.split(' ')[0] ?? '—'}</span></div>
                <div>By: <span class="font-bold">{ej?.username ?? '—'}</span></div>
                <div>Accuracy: <span class="font-bold" style="color: var(--success);">{ej?.accuracy_percent?.toFixed(1) ?? '—'}%</span></div>
                <div>Items: <span class="font-bold">{ej?.items?.length ?? '—'}</span></div>
                <div>Pages: <span class="font-bold">{ej?.total_pages ?? '—'}</span></div>
                <div>Cost: <span class="font-bold" style="color: var(--warning);">${ej?.cost_usd?.toFixed(3) ?? '—'}</span></div>
              </div>
            </div>

            <!-- Action buttons -->
            <div class="text-[10px] font-bold uppercase" style="color: var(--on-surface);">What would you like to do?</div>
            <div class="flex gap-2">
              <button class="cl-btn sm flex items-center gap-1"
                onclick={() => viewDuplicateResult(selectedIndex)}>
                <span class="material-symbols-outlined text-xs">visibility</span> VIEW RESULTS (free)
              </button>
              <button class="cl-btn sm flex items-center gap-1"
                style="border-color: var(--warning); color: var(--warning);"
                onclick={() => showReprocessConfirm = true}>
                <span class="material-symbols-outlined text-xs">refresh</span> RE-PROCESS (~$0.04)
              </button>
              <button class="cl-btn sm flex items-center gap-1"
                style="color: var(--on-surface-muted);"
                onclick={() => { queue = queue.filter((_, i) => i !== selectedIndex); selectedIndex = -1; }}>
                <span class="material-symbols-outlined text-xs">close</span> CANCEL
              </button>
            </div>
          </div>
        </div>

        <!-- Re-process confirmation dialog -->
        {#if showReprocessConfirm}
          <div class="mt-2 p-3" style="border: 1px solid var(--error); border-radius: var(--radius-md); background: var(--error-soft);">
            <div class="text-xs font-bold uppercase" style="color: var(--error);">Are you sure you want to re-process?</div>
            <div class="mt-2 text-[10px] space-y-1" style="color: var(--on-surface);">
              <div>• Run the full pipeline again (~60s)</div>
              <div>• Cost approximately $0.04-0.15</div>
              <div>• Creates a new job (old results kept)</div>
            </div>
            <div class="flex gap-2 mt-3">
              <button class="cl-btn sm flex items-center gap-1"
                style="background: var(--error); color: white; border-color: var(--error);"
                onclick={() => { showReprocessConfirm = false; selectedFile.status = 'queued'; queue = [...queue]; startPipeline(); }}>
                <span class="material-symbols-outlined text-xs">check</span> YES, RE-RUN
              </button>
              <button class="cl-btn sm"
                style="color: var(--on-surface-muted);"
                onclick={() => showReprocessConfirm = false}>
                CANCEL
              </button>
            </div>
          </div>
        {/if}
      {/if}

      <!-- Batch Summary -->
      {#if batchSummary}
        <div class="cl-panel mt-3">
          <div class="cl-hd"><span class="dot">◉</span>Batch Summary</div>
          <div class="cl-bd space-y-1 text-[10px] font-mono">
            <div>COMPLETED: {batchSummary.completed}/{batchSummary.total}</div>
            <div>FAILED: {batchSummary.failed}</div>
            {#if batchSummary.stopped > 0}<div style="color: var(--warning);">STOPPED: {batchSummary.stopped}</div>{/if}
            <div>AVG ACCURACY: {batchSummary.avg_accuracy}%</div>
            <div>TOTAL ITEMS: {batchSummary.total_items}</div>
            <div>TOTAL COST: ${batchSummary.total_cost}</div>
          </div>
        </div>
      {/if}

    </div>
    {/if}

    <!-- ═══════════ RIGHT PANEL ═══════════ -->
    <div style="min-width: 0; overflow-x: hidden;">
      {#if viewMode === 'pipeline'}
        <!-- Pipeline progress for current file -->
        <div class="mb-4 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold uppercase" style="color: var(--on-surface);">Processing: {selectedFile?.filename ?? ''}</span>
            <span class="pill clay">RUNNING</span>
            <span class="pill ok">RO-ED AI</span>
          </div>
          {#if running}
            <Button variant="danger" size="sm" onclick={stopPipeline}>
              <span class="flex items-center gap-1">
                <span class="material-symbols-outlined text-xs">stop_circle</span> STOP_PIPELINE
              </span>
            </Button>
          {/if}
        </div>
        <!-- Pipeline Flow Visualizer -->
        {#if vizSteps.length > 0}
          <div class="mb-3">
            <PipelineVisualizer
              bind:steps={vizSteps}
              filename={selectedFile?.filename ?? ''}
              complete={terminalComplete}
              summary={vizSummary}
            />
          </div>
        {/if}

        <!-- Detailed CLI Terminal -->
        <AgentTerminal
          filename={selectedFile?.filename ?? ''}
          lines={agentLines}
          running={running}
          summary={terminalSummary}
          jobId={selectedPipeline === 'v11' ? streamingJobId : null}
        />

      {:else if viewMode === 'results' || selectedJob || loadingResult || loadError}
        <!-- Results for selected file -->
        {#if loadError && !selectedJob}
          <div class="flex flex-col items-center gap-4 p-12 justify-center">
            <span class="material-symbols-outlined text-3xl" style="color: var(--error);">error</span>
            <span class="text-sm font-bold uppercase" style="color: var(--on-surface);">FAILED TO LOAD RESULTS</span>
            <span class="text-[10px] font-mono" style="color: var(--on-surface-muted);">{loadError}</span>
            <div class="flex gap-3">
              {#if selectedFile?.jobId}
                <button class="cl-btn sm"
                  style="border-color: var(--primary); color: var(--primary);"
                  onclick={() => { loadError = ''; loadJobResult(selectedFile.jobId); }}>
                  RETRY
                </button>
                <a href="/history?job={selectedFile.jobId}" class="cl-btn sm no-underline">
                  VIEW IN HISTORY →
                </a>
              {/if}
            </div>
          </div>
        {:else if loadingResult && !selectedJob}
          <div class="flex flex-col items-center gap-4 p-12 justify-center">
            <div class="agent-spinner" style="border-color: var(--secondary); border-top-color: transparent;"></div>
            <span class="text-sm font-bold uppercase" style="color: var(--on-surface);">LOADING RESULTS...</span>
          </div>
        {:else if selectedJob}
          {#if selectedPipeline === 'v11'}
            <ReviewSplitView
              jobId={selectedJob.job_id}
              job={selectedJob}
              onApprove={() => { reviewToast('Approved'); }}
              onReject={() => { reviewToast('Rejected'); }}
              onClose={() => { reviewToast('Closed'); }}
            />
          {:else}
            <ResultAccordion job={selectedJob} defaultOpen={true}
              pipelineSteps={terminalSteps}
              bind:pipelineCollapsed={terminalCollapsed}
              agentLines={agentLines}
              agentSummary={terminalSummary}
              vizSteps={vizSteps}
              vizSummary={vizSummary}
            />
          {/if}
        {/if}

      {:else if batchSummary}
        <!-- Batch complete: show all results as accordions -->
        <div class="mb-4">
          <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <KpiCard title="TOTAL" value="{batchSummary.total}" icon="folder" accent="var(--info)" subtitle="PDFs processed" />
            <KpiCard title="AVG ACCURACY" value="{batchSummary.avg_accuracy}%" progress={batchSummary.avg_accuracy} accent={getAccuracyColor(batchSummary.avg_accuracy)} />
            <KpiCard title="TOTAL ITEMS" value="{batchSummary.total_items}" icon="inventory_2" accent="var(--success)" />
            <KpiCard title="TOTAL COST" value="${batchSummary.total_cost}" icon="payments" accent="var(--info)" />
          </div>
        </div>

        {#each queue.filter(f => f.status === 'done' && f.jobId) as entry}
          <ResultAccordion job={jobResults[entry.jobId]} defaultOpen={false} />
        {/each}

      {:else if selectedFile?.status === 'error'}
        <!-- Error state -->
        <div class="flex flex-col items-center justify-center h-64">
          <span class="material-symbols-outlined text-4xl" style="color: var(--error);">error</span>
          <div class="mt-2 text-sm font-bold uppercase" style="color: var(--on-surface);">{selectedFile.filename}</div>
          <div class="text-xs mt-1 font-mono" style="color: var(--error);">
            {selectedFile.stepLabel || 'FAILED — pipeline error'}
          </div>
          <div class="mt-3 text-[10px] uppercase" style="color: var(--on-surface-subtle);">
            Clear queue and re-upload to retry
          </div>
        </div>

      {:else if selectedFile}
        <!-- Selected file waiting — show PDF preview -->
        {#if selectedFile.savedPath}
          {@const previewFilename = selectedFile.savedPath.split('/').pop()}
          <div class="cl-panel">
            <div class="cl-hd">
              <span class="dot">◉</span>PDF Preview — {selectedFile.filename}
              <span class="ct">
                {selectedFile.status === 'queued' ? 'Click EXECUTE to process' : selectedFile.status === 'duplicate' ? 'Duplicate — view results or reprocess' : selectedFile.status.toUpperCase()}
              </span>
            </div>
            <iframe
              src="/api/jobs/preview-pdf/{previewFilename}?token={auth.token}"
              title="PDF Preview"
              style="width: 100%; height: calc(100vh - 350px); border: none; min-height: 500px;"
            ></iframe>
          </div>
        {:else}
          <div class="flex flex-col items-center justify-center h-64 opacity-40">
            <span class="material-symbols-outlined text-4xl" style="color: var(--on-surface);">
              {selectedFile.status === 'duplicate' ? 'content_copy' : 'schedule'}
            </span>
            <div class="mt-2 text-sm font-bold uppercase" style="color: var(--on-surface);">{selectedFile.filename}</div>
            <div class="text-xs mt-1" style="color: var(--on-surface-muted);">
              {selectedFile.status === 'queued' ? 'Waiting to process — click EXECUTE' : selectedFile.status === 'duplicate' ? 'Duplicate — view results or reprocess' : selectedFile.status}
            </div>
          </div>
        {/if}

      {:else}
        <!-- No file selected -->
        <div class="flex flex-col items-center justify-center h-64 opacity-20">
          <span class="material-symbols-outlined text-4xl" style="color: var(--on-surface);">arrow_back</span>
          <div class="mt-2 text-sm font-bold uppercase" style="color: var(--on-surface);">SELECT A FILE</div>
          <div class="text-xs mt-1" style="color: var(--on-surface-muted);">Click a file in the queue to view details</div>
        </div>
      {/if}
    </div>
  </div>
{/if}
