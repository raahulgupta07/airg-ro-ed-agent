<script lang="ts">
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api, extractPDF, normalizeExtractResult } from '$lib/api';
  import type { PipelineKey } from '$lib/pipelineConfig';
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
    // Id the SERVER publishes SSE events + status under. Persisted alongside
    // jobId so a page refresh can reopen the stream; without it a refresh left
    // the job running on the server while the UI stopped following it.
    streamId: string;
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
  // Engine availability — super-admin controls this in Settings.
  // ROSETTA and ROVER PRO were retired as extraction engines on 2 Aug 2026: both
  // returned before every ATLAS stage, so a scanned declaration was read off
  // whichever attached page happened to carry a text layer. ROSETTA was also the
  // pre-selected default here, which is what an ordinary upload used.
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
        stepLabel: q.stepLabel, jobId: q.jobId, streamId: q.streamId, accuracy: q.accuracy,
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
      // `streamId` was added later — entries written by an older build don't
      // carry it, so default it rather than leaving the field undefined.
      queue = parsed.map(q => ({ ...q, streamId: q.streamId ?? '', file: new File([], q.filename) }));
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
  // Review takes the full width once a finished job is open and nothing is running.
  const hideQueueForReview = $derived(!!selectedJob && selectedPipeline === 'v11' && !running);

  // Nothing queued, nothing running, nothing open: the page has one job, which is
  // to accept a PDF. The engine picker went when ATLAS became the only engine, the
  // QUEUE panel spent its life reading "0 files", and the console sat idle telling
  // the reader to drop a PDF next to it — three panels explaining an empty state
  // that the drop zone already explains by existing.
  const idle = $derived(!running && queue.length === 0 && !hideQueueForReview
                        && !selectedJob && !streamingJobId);

  // CLI panel is always alive on the right — idle shows a ready banner.
  const cliLines = $derived(
    agentLines.length ? agentLines : [
      { text: `cityagent cli ready — engine ${ENGINE_OPTIONS.find(o => o[0] === selectedEngine)?.[1] ?? selectedEngine}`, type: 'muted' },
      { text: 'tip: drop a PDF left, EXECUTE, watch it stream here', type: 'muted' },
    ]
  );

  // ── Finished-state actions ─────────────────────────────────────────────
  // When a run ends the queue row said "DONE 100%" and stopped there — the
  // things a reviewer actually wants next (open it, take the marked PDF, take
  // the spreadsheet) were each somewhere else, and the marked PDF was reachable
  // only by typing its URL. This puts them where the run finished.
  //
  // The mark count is fetched rather than assumed: a scanned declaration
  // produces no coordinates, so offering a "marked PDF" there would hand the
  // user a 404. `available:false` hides the action and keeps the reason.
  let markInfo = $state<Record<string, { available: boolean; marks: number; reason: string | null }>>({});
  async function loadMarkInfo(jobId: string) {
    if (!jobId || markInfo[jobId]) return;
    try {
      markInfo[jobId] = await api.markedPdfStatus(jobId);
    } catch {
      // Never block the finished state on this — worst case the button is absent.
      markInfo[jobId] = { available: false, marks: 0, reason: null };
    }
  }
  $effect(() => {
    for (const e of queue) if (e.status === 'done' && e.jobId) loadMarkInfo(e.jobId);
  });

  async function takeExcel(entry: any) {
    try {
      await api.downloadJobExcel(entry.jobId, entry.filename);
    } catch (e: any) {
      agentLines = [...agentLines, { text: `excel download failed — ${e?.message ?? e}`, type: 'error' }];
    }
  }

  // Recent jobs shown at the bottom of the merged terminal (click → history detail).
  let recentJobs = $state<any[]>([]);
  onMount(async () => {
    try { recentJobs = (await api.listJobs(6)) ?? []; } catch {}
  });

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
        streamId: '',
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

  // Follow one V11 run to a terminal state by polling the queue status route.
  // Shared by the submit path and by the refresh-resume path so a job picked
  // back up after a reload resolves exactly like a freshly-submitted one.
  // `initialDelayMs` is shortened on resume: the run may already have finished
  // while the page was away, and the row must not sit spinning for a full tick.
  async function pollV11Job(queueIdx: number, streamId: string, dbJobId: string, initialDelayMs = 3000) {
    let pollAttempts = 0;
    const MAX_POLL = 600;  // 600 × 3s = 30 min ceiling
    let terminal = false;
    // A stream id can outlive its RQ record (or be a stale one restored from
    // localStorage). Without this the row polls a dead id for the full 30 min
    // and the user just watches a spinner.
    let consecutiveFailures = 0;
    const MAX_FAILURES = 20;  // ~60s of unbroken failures

    while (pollAttempts < MAX_POLL) {
      await new Promise(r => setTimeout(r, pollAttempts === 0 ? initialDelayMs : 3000));
      pollAttempts++;
      // STOP & CLEAR (or CLEAR) can drop the row mid-poll; writing to a gone
      // index would resurrect a phantom queue entry.
      if (!queue[queueIdx]) return;
      try {
        const headers: Record<string, string> = {};
        if (auth.token) headers['Authorization'] = `Bearer ${auth.token}`;
        const r = await fetch(`/api/extract-v11/status/${streamId}`, { headers });
        if (!r.ok) throw new Error(`status ${r.status}`);
        const txt = await r.text();
        const statusRes = JSON.parse(txt);
        consecutiveFailures = 0;

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
        // network blip — keep polling, but give up on a status id that never
        // answers so the row can't stay 'processing' forever
        consecutiveFailures++;
        if (consecutiveFailures >= MAX_FAILURES) {
          queue[queueIdx] = {
            ...queue[queueIdx],
            status: 'error',
            progress: 100,
            stepLabel: 'LOST TRACK OF JOB — check /history',
          };
          queue = [...queue];
          terminal = true;
          break;
        }
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
        // Persisted from the very first tick so a refresh during the upload —
        // before the 202 comes back — still has an id to reconnect with.
        streamId: preAllocId || entry.streamId,
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

          // Listen on the id the SERVER published under. `streamingJobId` was
          // set to our client-generated preAllocId before submitting, but the
          // server may allocate its own stream id — and the worker emits to
          // that one. When they differ the terminal subscribed to a channel
          // nothing was ever published on, so a running job showed the idle
          // banner and no log at all.
          if (streamId && streamId !== streamingJobId) streamingJobId = streamId;

          // Persist the server's stream id with the queue row. A refresh mid-run
          // used to lose it entirely: the job kept running but the UI had no id
          // to reopen the stream or the status poll with.
          queue[queueIdx] = { ...queue[queueIdx], streamId, jobId: dbJobId || queue[queueIdx].jobId };
          queue = [...queue];

          await pollV11Job(queueIdx, streamId, dbJobId);
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

  // Stop whatever is (or looks) running — including a stale job restored from
  // localStorage after a reload — and wipe the queue back to the idle state.
  function stopAndClear() {
    running = false;
    streamingJobId = null;
    clearAll();
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

  // Reconnect to a run that was still going when the page was refreshed.
  // The job never stopped server-side — only the UI stopped following it, so
  // progress froze and the queue row stayed 'processing' forever. Reopening the
  // stream replays the whole log from Redis, and the status poll settles the row
  // even when the run already finished while the page was away.
  async function resumeInterruptedJob() {
    const idx = queue.findIndex(q => q.status === 'processing' && (q.streamId || q.jobId));
    if (idx < 0) return;
    const entry = queue[idx];
    const streamId = entry.streamId || entry.jobId;

    selectedIndex = idx;
    viewMode = 'pipeline';
    queue[idx] = { ...queue[idx], stepLabel: 'RECONNECTING — following job...' };
    queue = [...queue];

    if (selectedPipeline === 'v11') streamingJobId = streamId;
    running = true;
    try {
      // Short first delay (not 0): if the run already finished, the status check
      // resolves the row almost immediately, but the terminal still gets a
      // moment to replay the historical events before the stream is closed.
      await pollV11Job(idx, streamId, entry.jobId || streamId, 1500);
    } finally {
      running = false;
      streamingJobId = null;
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
          if (!entry.jobId && !entry.streamId) {
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

      // Load results for completed items
      for (const entry of queue) {
        if (entry.status === 'done' && entry.jobId && !jobResults[entry.jobId]) {
          loadJobResult(entry.jobId);
        }
      }

      // Persistence must be live before the resume awaits anything, otherwise a
      // second refresh during the resume would restore the pre-resume snapshot.
      mounted = true;
      resumeInterruptedJob();
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
              // The DB row knows nothing about the SSE channel the worker
              // published on, so leave it blank rather than opening a stream
              // on an id that was never a stream id.
              streamId: '',
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
<!-- MERGED SINGLE PAGE — light controls left · CLI-only right  -->
<!-- ═══════════════════════════════════════════════════════════ -->

<div class={(hideQueueForReview || idle) ? 'grid grid-cols-1 gap-4' : 'grid grid-cols-1 xl:grid-cols-[minmax(660px,64%)_1fr] gap-4'} style="overflow-x: hidden;">

  <!-- ═══════════ LEFT: merged control terminal (light) ═══════════ -->
  {#if !hideQueueForReview}
  <div class="rv-term">
    <div class="rv-tbar">
      <span class="rv-dots"><span style="background:var(--error)"></span><span style="background:var(--warning)"></span><span style="background:var(--success)"></span></span>
      cityagent — agent
      <span class="flex-1"></span>
      <span>{doneCount}/{totalCount}</span>
    </div>
    <div class="rv-body">
     <div class="rv-work {idle ? 'stack' : ''}">
      <!-- rail: add work, run it -->
      <div class="rv-rail">
      <!-- One engine, no choice to make. The picker reappears by itself if a
           second engine is ever enabled again — the list is server-driven. -->
      {#if visibleEngines.length > 1}
        <div><span class="rv-p">❯</span> engine <span class="rv-cmt"># applies to whole queue</span></div>
        <div class="rv-engines">
          {#each visibleEngines as opt, ei}
            <button type="button" class="rv-eng {selectedEngine === opt[0] ? 'sel' : ''}"
                    onclick={() => (selectedEngine = opt[0] as 'auto' | 'presto' | 'classic' | 'atlas')}>
              <span class="rv-k">[{ei + 1}]</span><b>{opt[1]}</b>
              {#if selectedEngine === opt[0]}<span class="rv-mark">●</span>{/if}
              <div class="rv-d">{opt[2]}</div>
            </button>
          {/each}
        </div>
        <div class="rv-rule">── DROP ──────────────────────────────────</div>
      {/if}

      <button type="button" class="rv-drop {idle ? 'big' : ''}" onclick={() => fileInput.click()}>
        <span class="rv-arr">⇪</span>
        <span class="text-left">
          <b>drop customs PDFs here</b> <span class="rv-cmt">or click to browse</span><br/>
          <span class="rv-sub">.pdf ≤ 50 MB · multiple ok{idle ? '' : ' · lands in QUEUE below'}</span>
        </span>
      </button>

      <div class="rv-exec">
        {#if running}
          <button type="button" class="rv-btn danger" onclick={stopPipeline}>■ STOP</button>
          <button type="button" class="rv-btn danger" onclick={stopAndClear}>■ STOP &amp; CLEAR</button>
        {:else}
          {#if queue.some(f => f.status === 'processing')}
            <button type="button" class="rv-btn danger" onclick={stopAndClear}>■ STOP &amp; CLEAR</button>
          {/if}
          {#if queue.some(f => f.status === 'queued' || f.status === 'duplicate')}
            <button type="button" class="rv-btn pri" onclick={startPipeline}>▶ EXECUTE ({queue.filter(f => f.status === 'queued' || f.status === 'duplicate').length})</button>
          {/if}
          {#if queue.length > 0}
            <button type="button" class="rv-btn" onclick={clearAll}>CLEAR</button>
          {/if}
        {/if}
      </div>
      </div><!-- /rv-rail -->

      <!-- runs: what is queued and what has already been read -->
      <div class="rv-runs">
      <!-- The queue panel only ever had something to say once a file was in it.
           Empty, it was a heading and a line of text restating the drop zone. -->
      {#if queue.length > 0}
        <div class="rv-rule">── QUEUE ── {totalCount} file{totalCount === 1 ? '' : 's'} ────────────────────</div>
        <table class="rv-q">
          <tbody>
          {#each queue as entry, i}
            <tr class="rv-row {selectedIndex === i ? 'sel' : ''}" onclick={() => selectFile(i)}>
              <td class="rv-fn">{entry.filename}</td>
              <td class="rv-sz">{(entry.size / 1048576).toFixed(1)}M</td>
              <td>
                {#if entry.status === 'processing'}
                  <span class="rv-st p">◐ PROCESSING</span>
                  <span class="rv-bar"><i style="width:{Math.max(entry.progress || 0, 8)}%"></i></span>
                {:else if entry.status === 'duplicate'}
                  <span class="rv-st d">⧉ DUPLICATE</span>
                {:else if entry.status === 'done'}
                  <span class="rv-st ok">✓ DONE{entry.accuracy ? ' ' + entry.accuracy.toFixed(1) + '%' : ''}</span>
                {:else if entry.status === 'error'}
                  <span class="rv-st er">✗ ERROR</span>
                {:else if entry.status === 'stopped'}
                  <span class="rv-st er">■ STOPPED</span>
                {:else}
                  <span class="rv-st q">· QUEUED</span>
                {/if}
              </td>
              <td class="rv-act">
                {#if entry.status === 'duplicate'}
                  <button type="button" onclick={(e) => { e.stopPropagation(); viewDuplicateResult(i); }}>view</button>
                  <button type="button" class="pri" onclick={(e) => { e.stopPropagation(); selectedIndex = i; showReprocessConfirm = true; }}>re-run</button>
                {/if}
                {#if entry.status !== 'processing'}
                  <button type="button" aria-label="Remove from queue"
                          onclick={(e) => { e.stopPropagation(); queue = queue.filter((_, x) => x !== i); if (selectedIndex === i) selectedIndex = -1; }}>✕</button>
                {/if}
              </td>
            </tr>
            {#if entry.status === 'done' && entry.jobId}
              {@const mi = markInfo[entry.jobId]}
              <tr class="rv-expand"><td colspan="4">
                <div class="rv-done">
                  <button type="button" class="rv-btn pri"
                          onclick={(e) => { e.stopPropagation(); selectFile(i); }}>OPEN REVIEW</button>
                  {#if mi?.available}
                    <a class="rv-btn" href={api.markedPdfUrl(entry.jobId)} target="_blank"
                       rel="noopener" onclick={(e) => e.stopPropagation()}>⬇ MARKED PDF ({mi.marks})</a>
                  {/if}
                  <button type="button" class="rv-btn"
                          onclick={(e) => { e.stopPropagation(); takeExcel(entry); }}>⬇ EXCEL</button>
                </div>
                <div class="rv-batch">
                  {entry.duration ? entry.duration.toFixed(1) + 's · ' : ''}{entry.cost ? '$' + entry.cost.toFixed(4) + ' · ' : ''}{entry.itemsCount ?? 0} items{mi?.available ? ' · ' + mi.marks + ' values marked on the PDF' : ''}
                  {#if mi && !mi.available && mi.reason}
                    <br/><span class="rv-cmt">{mi.reason}</span>
                  {/if}
                </div>
              </td></tr>
            {/if}
            {#if entry.status === 'duplicate' && selectedIndex === i}
              {@const ej = entry.existingJob}
              <tr class="rv-expand"><td colspan="4">
                └ <b>already processed</b> {ej?.created_at?.split(' ')[0] ?? '—'} · acc {ej?.accuracy_percent?.toFixed(1) ?? '—'}% · {ej?.total_pages ?? '—'} pg · ${ej?.cost_usd?.toFixed(3) ?? '—'}
                <span class="rv-cmt"> — view = free · re-run ≈ $0.04, old result kept</span>
                {#if showReprocessConfirm}
                  <div class="rv-confirm">
                    re-run full pipeline (~60s, ~$0.04)?
                    <button type="button" class="danger"
                            onclick={() => { showReprocessConfirm = false; entry.status = 'queued'; queue = [...queue]; startPipeline(); }}>yes, re-run</button>
                    <button type="button" onclick={() => (showReprocessConfirm = false)}>cancel</button>
                  </div>
                {/if}
              </td></tr>
            {/if}
          {/each}
          </tbody>
        </table>
      {/if}


      {#if batchSummary}
        <div class="rv-rule">── BATCH ─────────────────────────────────</div>
        <div class="rv-batch">
          done {batchSummary.completed}/{batchSummary.total} · fail {batchSummary.failed}{batchSummary.stopped > 0 ? ' · stopped ' + batchSummary.stopped : ''} · avg acc {batchSummary.avg_accuracy}% · items {batchSummary.total_items} · ${batchSummary.total_cost}
        </div>
      {/if}

      {#if recentJobs.length > 0}
        <div class="rv-head">
          <span class="rv-rule" style="margin:0;">── RECENT ── last {recentJobs.length} ─────────────────</span>
          <a href="/history" class="rv-all">view all →</a>
        </div>
        <table class="rv-q">
          <tbody>
          {#each recentJobs as rj}
            <tr class="rv-row" onclick={() => { window.location.href = '/history?job=' + rj.job_id; }}>
              <td class="rv-fn">{rj.pdf_name}</td>
              <td class="rv-sz">{rj.created_at?.split(' ')[0] ?? ''}</td>
              <td><span class="rv-st" style="color: {getAccuracyColor(rj.accuracy_percent ?? 0)};">{(rj.accuracy_percent ?? 0).toFixed(1)}%</span></td>
              <td><span class="rv-st {rj.review_status === 'approved' ? 'ok' : 'd'}">{rj.review_status === 'approved' ? '✓ APPROVED' : (rj.review_status ?? rj.status ?? '').toUpperCase()}</span></td>
            </tr>
          {/each}
          </tbody>
        </table>
      {/if}

      </div><!-- /rv-runs -->
     </div><!-- /rv-work -->
    </div>
  </div>
  {/if}

  <!-- ═══════════ RIGHT: CLI only (review takes over full width when done) ═══════════ -->
  <!-- Hidden while idle. An empty console whose only content is a tip telling you
       to drop a PDF is half the screen spent restating the drop zone. It comes
       back the moment there is something to stream. -->
  {#if !idle}
  <div style="min-width: 0; overflow-x: hidden;">
    {#if hideQueueForReview && selectedJob}
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
    {:else}
      {#if viewMode === 'pipeline'}
        <div class="mb-2 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold uppercase" style="color: var(--on-surface);">Processing: {selectedFile?.filename ?? ''}</span>
            <span class="pill clay">RUNNING</span>
          </div>
          {#if running}
            <Button variant="danger" size="sm" onclick={stopPipeline}>
              <span class="flex items-center gap-1">
                <span class="material-symbols-outlined text-xs">stop_circle</span> STOP
              </span>
            </Button>
          {/if}
        </div>
      {/if}

      {#if loadError && !selectedJob}
        <div class="mb-2 px-3 py-2 flex items-center gap-3" style="border: 1px solid var(--error); border-radius: var(--radius-md); background: var(--error-soft);">
          <span class="text-[11px] font-mono" style="color: var(--error);">failed to load results — {loadError}</span>
          {#if selectedFile?.jobId}
            <button class="cl-btn sm" style="border-color: var(--primary); color: var(--primary);"
              onclick={() => { loadError = ''; loadJobResult(selectedFile.jobId); }}>RETRY</button>
            <a href="/history?job={selectedFile.jobId}" class="cl-btn sm no-underline">HISTORY →</a>
          {/if}
        </div>
      {:else if loadingResult && !selectedJob}
        <div class="mb-2 px-3 py-2 flex items-center gap-2" style="border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--surface-container-lowest);">
          <div class="agent-spinner" style="width: 14px; height: 14px; border-color: var(--secondary); border-top-color: transparent;"></div>
          <span class="text-[11px] font-mono" style="color: var(--on-surface-muted);">loading results…</span>
        </div>
      {/if}

      <AgentTerminal
        filename={selectedFile?.filename ?? ''}
        lines={cliLines}
        running={running}
        summary={terminalSummary}
        jobId={selectedPipeline === 'v11' ? streamingJobId : null}
        defaultHeight={520}
        light
      />
    {/if}
  </div>
  {/if}
</div>

<style>
  /* Merged control terminal — light "paper console", palette from the mockup */
  .rv-term { border: 1px solid var(--line); background: var(--surface-container-lowest); border-radius: 8px; overflow: hidden;
             box-shadow: 0 1px 3px rgba(60,50,30,0.06);
             font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 12px; color: var(--on-surface); }
  .rv-tbar { display: flex; align-items: center; gap: 8px; padding: 7px 12px; background: var(--surface-container-low);
             border-bottom: 1px solid var(--line); font-size: 10.5px; color: var(--on-surface-muted); }
  .rv-dots span { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; opacity: 0.75; }
  .rv-body { padding: 12px 14px; }
  .rv-p { color: var(--primary); font-weight: 700; }
  .rv-cmt { color: var(--on-surface-subtle); }
  .rv-sub { color: var(--on-surface-muted); font-size: 11px; }
  .rv-rule { color: var(--on-surface-subtle); margin: 14px 0 8px; font-size: 11px; letter-spacing: 0.05em; white-space: nowrap; overflow: hidden; }

  .rv-engines { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }

  /* Workbench layout: a narrow rail for "what to run and how", a wide column
     for "what is running and what already ran". Below 1000px they stack, so a
     laptop or tablet still gets the old single-column reading order. */
  .rv-work { display: grid; grid-template-columns: 1fr; gap: 18px; }
  @media (min-width: 1000px) {
    .rv-work { grid-template-columns: 212px minmax(0, 1fr); }
    .rv-rail { border-right: 1px solid var(--line); padding-right: 16px; }
    .rv-rail .rv-engines { flex-direction: column; gap: 6px; }
    .rv-rail .rv-eng { min-width: 0; width: 100%; }
    .rv-rail .rv-drop { width: 100%; }
    .rv-rail .rv-exec { flex-direction: column; align-items: stretch; }
    .rv-rail .rv-exec button { width: 100%; }
    /* The rail's own rules would run the full column width and look broken. */
    .rv-rail .rv-rule { overflow: hidden; }
  }
  /* Idle: one column, drop zone on top, recent below. The rail's divider and
     fixed width only make sense next to a running queue. */
  .rv-work.stack { grid-template-columns: 1fr !important; gap: 22px; }
  .rv-work.stack .rv-rail { border-right: none; padding-right: 0; }

  .rv-runs { min-width: 0; }
  .rv-runs .rv-q { width: 100%; }
  .rv-head { display: flex; align-items: baseline; justify-content: space-between;
             gap: 12px; margin: 14px 0 8px; }
  .rv-all { color: var(--primary); font-size: 11px; text-decoration: none; white-space: nowrap; }
  .rv-all:hover { text-decoration: underline; }
  .rv-eng { flex: 1; min-width: 150px; border: 1px solid var(--line); border-radius: 6px; padding: 7px 11px;
            cursor: pointer; background: var(--surface-container-lowest); text-align: left; color: var(--on-surface);
            font-family: inherit; font-size: 12px; }
  .rv-eng:hover { border-color: var(--on-surface-muted); }
  .rv-eng.sel { border-color: var(--primary); background: var(--primary-container); }
  .rv-eng.sel b { color: var(--primary); }
  .rv-eng .rv-k { color: var(--on-surface-subtle); margin-right: 6px; }
  .rv-eng .rv-d { color: var(--on-surface-muted); font-size: 10.5px; }
  .rv-mark { color: var(--primary); margin-left: 6px; }

  .rv-drop { width: 100%; border: 1.5px dashed var(--primary); border-radius: 6px; padding: 12px 14px;
             display: flex; align-items: center; gap: 12px; cursor: pointer; background: var(--primary-soft);
             color: var(--on-surface); font-family: inherit; font-size: 12px; }
  .rv-drop:hover { background: var(--primary-container); }
  .rv-arr { color: var(--primary); font-size: 18px; }
  /* Idle: the drop zone IS the page, so it gets the room the other panels gave up. */
  .rv-drop.big { flex-direction: column; justify-content: center; text-align: center;
                 gap: 8px; padding: 40px 20px; }
  .rv-drop.big .rv-arr { font-size: 30px; }
  .rv-drop.big :global(b) { font-size: 14px; }

  .rv-q { width: 100%; border-collapse: collapse; font-size: 12px; }
  .rv-q td { padding: 5px 8px; vertical-align: middle; white-space: nowrap; }
  .rv-row { border-bottom: 1px solid var(--line-soft); cursor: pointer; }
  .rv-row:hover { background: var(--surface-container-low); }
  .rv-row.sel { background: var(--primary-container); box-shadow: inset 2px 0 0 var(--primary); }
  .rv-fn { color: var(--on-surface); max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
  .rv-sz { color: var(--on-surface-subtle); font-size: 11px; }
  .rv-st { font-size: 10px; font-weight: 700; letter-spacing: 0.06em; }
  .rv-st.q { color: var(--on-surface-muted); } .rv-st.p { color: var(--info); } .rv-st.d { color: var(--warning); }
  .rv-st.ok { color: var(--success); } .rv-st.er { color: var(--error); }
  .rv-bar { display: inline-block; width: 80px; height: 5px; background: var(--line-soft); border-radius: 3px;
            overflow: hidden; vertical-align: middle; margin-left: 6px; }
  .rv-bar i { display: block; height: 100%; background: var(--info); transition: width 0.4s; }
  .rv-act { text-align: right; }
  .rv-act button { font-size: 10px; color: var(--on-surface-muted); border: 1px solid var(--line); border-radius: 4px;
                   padding: 1px 8px; margin-left: 4px; background: transparent; cursor: pointer;
                   font-family: inherit; }
  .rv-act button:hover { color: var(--on-surface); border-color: var(--on-surface-muted); }
  .rv-act button.pri { color: var(--primary); border-color: var(--primary); }
  .rv-expand td { background: var(--surface-container-low); border-bottom: 1px solid var(--line-soft); white-space: normal;
                  color: var(--on-surface-muted); font-size: 11px; padding: 8px 12px; }
  .rv-expand b { color: var(--warning); }
  .rv-confirm { margin-top: 6px; color: var(--on-surface); }
  .rv-confirm button { font-size: 10px; border: 1px solid var(--line); border-radius: 4px; padding: 2px 8px;
                       margin-left: 6px; background: transparent; color: var(--on-surface); cursor: pointer;
                       font-family: inherit; }
  .rv-confirm button.danger { color: var(--error); border-color: var(--error-container); }
  /* `.rv-empty` went with the "queue empty — drop a PDF above" line it styled. */

  .rv-exec { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  .rv-btn { border: 1px solid var(--line); border-radius: 5px; padding: 6px 14px; cursor: pointer;
            background: var(--surface-container-lowest); color: var(--on-surface); font-family: inherit; font-size: 11px;
            font-weight: 700; letter-spacing: 0.05em; }
  .rv-btn:hover { border-color: var(--on-surface-muted); }
  .rv-btn.pri { background: var(--primary); border-color: var(--primary); color: var(--surface-container-lowest); }
  .rv-btn.pri:hover { background: var(--primary-hover); }
  .rv-btn.danger { color: var(--error); border-color: var(--error-container); }
  .rv-btn.danger:hover { background: var(--error-soft); }

  .rv-batch { color: var(--on-surface-muted); font-size: 11px; }
  /* Finished-state actions: what to do next, where the run ended. */
  .rv-done { display: flex; gap: 7px; flex-wrap: wrap; margin: 2px 0 6px; }
  .rv-done .rv-btn { text-decoration: none; display: inline-block; }
</style>
