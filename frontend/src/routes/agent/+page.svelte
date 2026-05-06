<script lang="ts">
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { api, extractPDF, normalizeExtractResult } from '$lib/api';
  import PipelineSelector from '$lib/components/PipelineSelector.svelte';
  import type { PipelineKey } from '$lib/pipelineConfig';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import Button from '$lib/components/Button.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import QueueItem from '$lib/components/QueueItem.svelte';
  import RecentJobs from '$lib/components/RecentJobs.svelte';
  import ResultAccordion from '$lib/components/ResultAccordion.svelte';
  import ReviewSplitView from '$lib/components/ReviewSplitView.svelte';
  import PipelineTerminal from '$lib/components/PipelineTerminal.svelte';
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
  let ws = $state<WebSocket | null>(null);
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
  // V11 review mode toggle: VIEW (read-only ResultAccordion) | REVIEW (editable split view)
  let reviewMode = $state<'view' | 'review'>('view');
  function reviewToast(msg: string) {
    // ReviewSplitView shows its own toast; this is just a no-op safety hook
    void msg;
  }
  const isProcessing = $derived(running);

  // Route extraction through chosen pipeline. V7 keeps WebSocket flow (handled in startPipeline);
  // V10/V11 use HTTP since they don't have WebSocket support yet.
  async function runExtract(file: File, jobId?: string) {
    if (selectedPipeline === 'v7') {
      // TODO: V7 normally uses the WebSocket flow in startPipeline().
      // This HTTP fallback is provided for parity if called directly.
      const raw = await extractPDF(file, 'v7', auth.token);
      return normalizeExtractResult(raw);
    }
    const raw = await extractPDF(file, selectedPipeline, auth.token, jobId);
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

    // V10/V11 don't have WebSocket support yet — use HTTP path. V7 uses WebSocket below.
    if (selectedPipeline !== 'v7') {
      runHttpPipeline(filesToProcess);
      return;
    }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${location.host}/api/ws/batch`);
    ws = socket;

    socket.onopen = () => {
      const filesPayload = queue
        .filter(f => f.status === 'queued')
        .map(f => ({ path: f.savedPath, filename: f.filename }));

      socket.send(JSON.stringify({ token: auth.token, files: filesPayload, mode: pipelineMode }));
    };

    socket.onmessage = async (event) => {
      const msg = JSON.parse(event.data);

      if (msg.error) {
        running = false;
        return;
      }

      // Batch complete
      if (msg.batch_complete) {
        batchSummary = msg.summary;
        running = false;
        ws = null;
        // Only switch to batch view if we don't already have results showing
        if (viewMode === 'pipeline') viewMode = 'results';
        return;
      }

      // Find file by index (index into queued-only list)
      const queuedFiles = queue.filter(f => f.status !== 'done' && f.status !== 'error' || f.status === 'processing' || f.status === 'queued' || f.status === 'stopped');
      const fileIdx = msg.file_index;

      // Handle log messages FIRST (they don't have file_index)
      if (msg.log && msg.file_index === undefined) {
        agentLines = [...agentLines, { text: msg.log, type: msg.log_type || 'detail' }];
        // Also add to last terminal step
        const lastStep = terminalSteps[terminalSteps.length - 1];
        if (lastStep) {
          lastStep.lines.push({ text: msg.log, type: msg.log_type || 'detail' });
          terminalSteps = [...terminalSteps];
        }
        return;
      }

      // Map file_index to queue index
      let queueIdx = -1;
      let counter = 0;
      for (let i = 0; i < queue.length; i++) {
        if (queue[i].status === 'queued' || queue[i].status === 'processing' || queue[i].status === 'stopped') {
          if (counter === fileIdx) { queueIdx = i; break; }
          counter++;
        }
      }
      // Fallback: direct index
      if (queueIdx < 0 && fileIdx < queue.length) queueIdx = fileIdx;
      if (queueIdx < 0) return;

      const entry = queue[queueIdx];

      // Job created — save jobId early so it survives navigation
      if (msg.job_created) {
        queue[queueIdx] = { ...entry, jobId: msg.job_id, status: 'processing' };
        queue = [...queue];
        return;
      }

      // File complete
      if (msg.file_complete) {
        terminalComplete = true;
        vizSummary = {
          items: msg.items_count || 0, accuracy: msg.accuracy || 0,
          cost: msg.cost || 0, duration: msg.duration || 0,
          corrections: (msg.corrections || []).length || 0,
          aiTables: 0,
          declFilled: 16,
        };
        terminalSummary = {
          items: msg.items_count || 0,
          accuracy: msg.accuracy || 0,
          decision: (msg.gate_log?.[0] || '').split('—')[0]?.trim() || 'UNKNOWN',
          duration: msg.duration || 0,
          cost: msg.cost || 0,
          pipelineMode: msg.pipeline_mode || pipelineMode,
          crossValidation: msg.cross_validation || null,
        };

        // Replace entry with NEW object (forces $derived to detect change)
        queue[queueIdx] = {
          ...entry,
          status: msg.status === 'done' ? 'done' : msg.status === 'stopped' ? 'stopped' : 'error',
          jobId: msg.job_id || '',
          accuracy: msg.accuracy || 0,
          itemsCount: msg.items_count || 0,
          cost: msg.cost || 0,
          duration: msg.duration || 0,
          gateLog: msg.gate_log || [],
          progress: 100,
        };
        queue = [...queue];

        // Auto-select completed file and load results
        if (msg.job_id) {
          selectedIndex = queueIdx;
          // Use inline job data from WebSocket (avoids separate HTTP fetch)
          if (msg.job_data) {
            jobResults[msg.job_id] = msg.job_data;
            jobResults = { ...jobResults };
            loadingResult = false;
            viewMode = 'results';
          } else {
            loadJobResult(msg.job_id);
            viewMode = 'results';
          }
        }
        return;
      }

      // Step progress → build terminal steps + visualizer
      if (msg.step) {
        queue[queueIdx] = {
          ...entry,
          status: 'processing',
          stepLabel: `${msg.name} ${msg.status === 'done' ? '✓' : '...'}`,
          progress: (msg.step / 10) * 100,
        };
        queue = [...queue];
        selectedIndex = queueIdx;

        // Update visualizer flow diagram
        const vizIcons: Record<string, string> = {
          'HD_SPLITTER': '📄', 'VISION_EXTRACT': '👁', 'AI_ASSEMBLER': '🤖',
          'AI_VERIFIER': '✦', 'VALIDATE_SAVE': '💾',
        };
        const vizIdx = vizSteps.findIndex(s => s.id === msg.name);
        if (vizIdx < 0) {
          vizSteps = [...vizSteps, {
            id: msg.name, label: msg.name.replace(/_/g, ' '),
            icon: vizIcons[msg.name] || '⚙', status: msg.status,
            detail: msg.detail || '', time: '',
          }];
        } else {
          vizSteps[vizIdx].status = msg.status;
          if (msg.detail) vizSteps[vizIdx].detail = msg.detail;
          vizSteps = [...vizSteps];
        }

        // Update terminal steps (deduplicate by step + name)
        const existingIdx = terminalSteps.findIndex(s => s.step === msg.step && s.name === msg.name);
        if (msg.status === 'running') {
          if (existingIdx < 0) {
            terminalSteps = [...terminalSteps, { step: msg.step, name: msg.name, status: 'running', lines: [], duration: 0, cost: 0 }];
          } else {
            terminalSteps[existingIdx].status = 'running';
            terminalSteps = [...terminalSteps];
          }
        } else if (msg.status === 'done') {
          if (existingIdx >= 0) {
            terminalSteps[existingIdx].status = 'done';
            terminalSteps[existingIdx].duration = msg.duration || 0;
            terminalSteps[existingIdx].cost = msg.cost || 0;
            if (msg.detail) {
              terminalSteps[existingIdx].lines.push({ text: msg.detail, type: 'success' });
            }
            // Add detailed log lines from backend
            if (msg.log_lines && Array.isArray(msg.log_lines)) {
              for (const line of msg.log_lines) {
                terminalSteps[existingIdx].lines.push({ text: line, type: 'data' });
              }
            }
            terminalSteps = [...terminalSteps];
          }
        } else if (msg.status === 'error') {
          if (existingIdx >= 0) {
            terminalSteps[existingIdx].status = 'error';
            terminalSteps[existingIdx].duration = msg.duration || 0;
            if (msg.detail) {
              terminalSteps[existingIdx].lines.push({ text: msg.detail, type: 'error' });
            }
            terminalSteps = [...terminalSteps];
          }
        }
      }

      // Log messages → add to BOTH old terminal AND new agent terminal
      if (msg.log) {
        const lastStep = terminalSteps[terminalSteps.length - 1];
        if (lastStep) {
          lastStep.lines.push({ text: msg.log, type: msg.log_type || 'detail' });
          terminalSteps = [...terminalSteps];
        }
        // New agent terminal — just append every line
        agentLines = [...agentLines, { text: msg.log, type: msg.log_type || 'detail' }];
      }
    };

    socket.onerror = () => { running = false; ws = null; startCompletionPoll(); };
    socket.onclose = () => { running = false; ws = null; startCompletionPoll(); };

    // Also start a safety poll — if WS goes silent, check DB for completion
    const safetyPoll = setInterval(async () => {
      const processingEntries = queue.filter(q => q.status === 'processing' && q.jobId);
      if (processingEntries.length === 0 && !running) {
        clearInterval(safetyPoll);
        return;
      }
      for (const entry of processingEntries) {
        try {
          const job = await api.getJob(entry.jobId);
          if (job.status === 'COMPLETED') {
            entry.status = 'done';
            entry.accuracy = job.accuracy_percent || 0;
            entry.itemsCount = job.items?.length || 0;
            entry.cost = job.cost_usd || 0;
            entry.duration = job.processing_time_seconds || 0;
            entry.progress = 100;
            entry.stepLabel = '';
            jobResults[entry.jobId] = job;
            jobResults = { ...jobResults };
            terminalComplete = true;
            terminalSummary = {
              items: job.items?.length || 0,
              accuracy: job.accuracy_percent || 0,
              decision: (job.accuracy_percent || 0) >= 90 ? 'ACCEPTED' : 'FIXED',
              duration: job.processing_time_seconds || 0,
              cost: job.cost_usd || 0,
            };
            queue = [...queue];
            running = false;
            clearInterval(safetyPoll);
          }
        } catch {}
      }
    }, 5000);
  }

  // ── WS closed — start polling for completion ──
  function startCompletionPoll() {
    if (pollTimer) return; // already polling
    const hasProcessing = queue.some(q => q.status === 'processing' && q.jobId);
    if (hasProcessing) {
      pollProcessingJobs();
      pollTimer = setInterval(pollProcessingJobs, 3000);
    }
  }

  // ── HTTP pipeline path (V10/V11 — no WebSocket yet) ──
  async function runHttpPipeline(filesToProcess: FileEntry[]) {
    for (const entry of filesToProcess) {
      const queueIdx = queue.indexOf(entry);
      if (queueIdx < 0) continue;
      // For V11, allocate a job id up-front and start the SSE stream so the
      // AgentTerminal renders router decisions live while the HTTP call runs.
      const preAllocId = selectedPipeline === 'v11' ? makeJobId() : '';
      queue[queueIdx] = {
        ...entry,
        status: 'processing',
        stepLabel: `${selectedPipeline.toUpperCase()} extracting...`,
        progress: 30,
        jobId: preAllocId || entry.jobId,
      };
      queue = [...queue];
      selectedIndex = queueIdx;
      if (preAllocId) streamingJobId = preAllocId;
      try {
        const job = await runExtract(entry.file, preAllocId || undefined);
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
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ stop: true }));
    }
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

  // Poll for completion of processing jobs
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  let pollAttempts = 0;
  const MAX_POLL_ATTEMPTS = 20; // ~60s at 3s intervals

  async function pollProcessingJobs() {
    const processingEntries = queue.filter(q => q.status === 'processing' && q.jobId);
    if (processingEntries.length === 0) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      pollAttempts = 0;
      return;
    }

    pollAttempts++;

    for (const entry of processingEntries) {
      try {
        const job = await api.getJob(entry.jobId);
        if (job.status === 'COMPLETED') {
          entry.status = 'done';
          entry.accuracy = job.accuracy_percent || 0;
          entry.itemsCount = job.items?.length || 0;
          entry.cost = job.cost_usd || 0;
          entry.duration = job.processing_time_seconds || 0;
          entry.progress = 100;
          entry.stepLabel = '';
          jobResults[entry.jobId] = job;
          jobResults = { ...jobResults };
          queue = [...queue];
        } else if (job.status === 'FAILED') {
          entry.status = 'error';
          entry.progress = 100;
          entry.stepLabel = job.error_message || 'FAILED';
          queue = [...queue];
        } else if (pollAttempts >= MAX_POLL_ATTEMPTS) {
          // Job stuck in PROCESSING too long — mark as error
          entry.status = 'error';
          entry.progress = 100;
          entry.stepLabel = 'TIMED OUT — pipeline may have crashed';
          queue = [...queue];
        }
      } catch {
        // API error (404 etc) — mark as error
        entry.status = 'error';
        entry.progress = 100;
        entry.stepLabel = 'FAILED — job not found';
        queue = [...queue];
      }
    }

    // Stop polling if no more processing
    if (!queue.some(q => q.status === 'processing')) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      pollAttempts = 0;
    }
  }

  // On mount: restore queue, then poll for updates
  onMount(async () => {
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

      // Check if any restored items were processing WITH jobId — immediately check DB status
      if (queue.some(q => q.status === 'processing' && q.jobId)) {
        pollAttempts = 0;
        await pollProcessingJobs();
        // If still processing after first check, continue polling
        if (queue.some(q => q.status === 'processing')) {
          pollTimer = setInterval(pollProcessingJobs, 3000);
        }
      }
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
          pollTimer = setInterval(pollProcessingJobs, 3000);
        }
      }
    } catch {}

    // Enable queue persistence now that restore is done
    mounted = true;

    return () => {
      if (pollTimer) clearInterval(pollTimer);
    };
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
    <div class="w-full max-w-4xl mb-4 p-4 rounded border-2 border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30">
      <div class="flex items-start gap-3 flex-wrap">
        <span class="text-2xl">🎼</span>
        <div class="flex-1 min-w-[200px]">
          <div class="font-bold text-base">Maestro Router (auto)</div>
          <div class="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
            Auto-classifies each page → PRINTED pages run Veritas, INKED pages run Scrivener, EXTRA attachments skipped. Best for any doc — printed, inked, or mixed bundles.
          </div>
        </div>
        <div class="flex gap-4 text-xs font-mono">
          <div>💰 $0.08–0.40</div>
          <div>⏱ 60–150s</div>
        </div>
      </div>
    </div>
    <!-- Big drop zone -->
    <button
      class="w-full max-w-4xl border-2 border-dashed cursor-pointer transition-colors p-16"
      style="border-color: var(--on-surface); background: transparent;"
      onclick={() => fileInput.click()}
    >
      <div class="text-center">
        <span class="material-symbols-outlined" style="font-size: 4rem; color: var(--on-surface); opacity: 0.4;">cloud_upload</span>
        <div class="mt-4 text-xl font-black uppercase tracking-tight" style="color: var(--on-surface);">
          DROP CUSTOMS PDFs HERE
        </div>
        <div class="mt-2 text-sm font-bold uppercase" style="color: var(--outline);">
          or click to browse
        </div>
        <div class="mt-4 flex items-center justify-center gap-6 text-[10px] font-mono uppercase" style="color: var(--outline);">
          <span>Single or multiple files</span>
          <span>·</span>
          <span>.pdf up to 50MB each</span>
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

  <div class="grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-4" style="min-height: calc(100vh - 280px); overflow-x: hidden;">

    <!-- ═══════════ LEFT PANEL ═══════════ -->
    <div class="flex flex-col">
      <!-- Smart Router info banner (compact for left panel) -->
      <div class="mb-3 p-2 rounded border border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30 flex items-center gap-2">
        <span>🎼</span>
        <span class="text-xs font-semibold">Maestro Router</span>
        <span class="text-[11px] text-zinc-600 dark:text-zinc-400 flex-1">auto: PRINTED→Veritas, INKED→Scrivener</span>
        <span class="text-[11px] font-mono">$0.08–0.40</span>
      </div>
      <!-- Add more / New job buttons -->
      <div class="flex gap-2 mb-3">
        <button
          class="flex-1 p-3 border-2 border-dashed cursor-pointer transition-colors"
          style="border-color: var(--on-surface); background: transparent;"
          onclick={() => fileInput.click()}
        >
          <div class="flex items-center justify-center gap-2">
            <span class="material-symbols-outlined text-base" style="color: var(--on-surface);">add_circle</span>
            <span class="text-[10px] font-black uppercase" style="color: var(--on-surface);">ADD MORE PDFs</span>
          </div>
        </button>
        <button
          class="p-3 border-2 cursor-pointer press-effect"
          style="border-color: var(--on-surface); background: var(--primary-container); box-shadow: 2px 2px 0px 0px var(--on-surface);"
          onclick={clearAll}
        >
          <div class="flex items-center justify-center gap-2">
            <span class="material-symbols-outlined text-base" style="color: var(--on-surface);">restart_alt</span>
            <span class="text-[10px] font-black uppercase" style="color: var(--on-surface);">NEW JOB</span>
          </div>
        </button>
      </div>

      <!-- Queue -->
      <div class="border-2 flex-1 flex flex-col" style="border-color: var(--on-surface);">
        <div class="dark-bar flex justify-between items-center text-xs">
          <span>QUEUE</span>
          <span class="text-[10px] py-0.5 px-2" style="background: var(--surface); color: var(--on-surface);">
            {doneCount}/{totalCount}
          </span>
        </div>

        <!-- Queue list -->
        <div class="flex-1 overflow-y-auto custom-scrollbar bg-white" style="max-height: 400px;">
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
        <div class="px-2 pt-2 flex items-center gap-2" style="border-top: 1px solid rgba(56,56,50,0.15);">
          <span class="px-2 py-1 text-[9px] font-black uppercase" style="background: #007518; color: white;">RO-ED AI</span>
          <span class="text-[7px] font-mono" style="color: var(--outline);">SMART EXTRACTION · HD VISION</span>
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
        <div class="mt-2 border-2" style="border-color: var(--on-surface);">
          <div class="px-3 py-2 text-xs font-bold uppercase text-white" style="background: #b45309;">
            THIS DOCUMENT WAS ALREADY PROCESSED
          </div>
          <div class="p-3 bg-white space-y-3">
            <!-- Previous result info -->
            <div class="border p-2" style="border-color: rgba(56,56,50,0.15); background: var(--surface-container);">
              <div class="text-[9px] font-bold uppercase" style="color: var(--outline);">PREVIOUS EXTRACTION</div>
              <div class="mt-1 grid grid-cols-3 gap-2 text-[10px]" style="color: var(--on-surface);">
                <div>Processed: <span class="font-bold">{ej?.created_at?.split(' ')[0] ?? '—'}</span></div>
                <div>By: <span class="font-bold">{ej?.username ?? '—'}</span></div>
                <div>Accuracy: <span class="font-bold" style="color: #22c55e;">{ej?.accuracy_percent?.toFixed(1) ?? '—'}%</span></div>
                <div>Items: <span class="font-bold">{ej?.items?.length ?? '—'}</span></div>
                <div>Pages: <span class="font-bold">{ej?.total_pages ?? '—'}</span></div>
                <div>Cost: <span class="font-bold" style="color: #eab308;">${ej?.cost_usd?.toFixed(3) ?? '—'}</span></div>
              </div>
            </div>

            <!-- Action buttons -->
            <div class="text-[10px] font-bold uppercase" style="color: var(--on-surface);">What would you like to do?</div>
            <div class="flex gap-2">
              <button class="flex items-center gap-1 px-3 py-2 text-[10px] font-bold uppercase cursor-pointer border-2"
                style="border-color: var(--on-surface); background: var(--on-surface); color: var(--surface);"
                onclick={() => viewDuplicateResult(selectedIndex)}>
                <span class="material-symbols-outlined text-xs">visibility</span> VIEW RESULTS (free)
              </button>
              <button class="flex items-center gap-1 px-3 py-2 text-[10px] font-bold uppercase cursor-pointer border-2"
                style="border-color: #b45309; color: #b45309; background: white;"
                onclick={() => showReprocessConfirm = true}>
                <span class="material-symbols-outlined text-xs">refresh</span> RE-PROCESS (~$0.04)
              </button>
              <button class="flex items-center gap-1 px-3 py-2 text-[10px] font-bold uppercase cursor-pointer border"
                style="border-color: var(--outline); color: var(--outline);"
                onclick={() => { queue = queue.filter((_, i) => i !== selectedIndex); selectedIndex = -1; }}>
                <span class="material-symbols-outlined text-xs">close</span> CANCEL
              </button>
            </div>
          </div>
        </div>

        <!-- Re-process confirmation dialog -->
        {#if showReprocessConfirm}
          <div class="mt-2 border-2 p-3" style="border-color: #ef4444; background: #fef2f2;">
            <div class="text-xs font-bold uppercase" style="color: #ef4444;">Are you sure you want to re-process?</div>
            <div class="mt-2 text-[10px] space-y-1" style="color: var(--on-surface);">
              <div>• Run the full pipeline again (~60s)</div>
              <div>• Cost approximately $0.04-0.15</div>
              <div>• Creates a new job (old results kept)</div>
            </div>
            <div class="flex gap-2 mt-3">
              <button class="flex items-center gap-1 px-3 py-2 text-[10px] font-bold uppercase cursor-pointer"
                style="background: #ef4444; color: white; border: none;"
                onclick={() => { showReprocessConfirm = false; selectedFile.status = 'queued'; queue = [...queue]; startPipeline(); }}>
                <span class="material-symbols-outlined text-xs">check</span> YES, RE-RUN
              </button>
              <button class="px-3 py-2 text-[10px] font-bold uppercase cursor-pointer border"
                style="border-color: var(--outline); color: var(--outline);"
                onclick={() => showReprocessConfirm = false}>
                CANCEL
              </button>
            </div>
          </div>
        {/if}
      {/if}

      <!-- Batch Summary -->
      {#if batchSummary}
        <div class="mt-3 border-2 p-3" style="border-color: var(--on-surface); background: white;">
          <div class="tag-label mb-2">BATCH_SUMMARY</div>
          <div class="space-y-1 text-[10px] font-mono">
            <div>COMPLETED: {batchSummary.completed}/{batchSummary.total}</div>
            <div>FAILED: {batchSummary.failed}</div>
            {#if batchSummary.stopped > 0}<div style="color: #ff9d00;">STOPPED: {batchSummary.stopped}</div>{/if}
            <div>AVG ACCURACY: {batchSummary.avg_accuracy}%</div>
            <div>TOTAL ITEMS: {batchSummary.total_items}</div>
            <div>TOTAL COST: ${batchSummary.total_cost}</div>
          </div>
        </div>
      {/if}

    </div>

    <!-- ═══════════ RIGHT PANEL ═══════════ -->
    <div style="min-width: 0; overflow-x: hidden;">
      {#if viewMode === 'pipeline'}
        <!-- Pipeline progress for current file -->
        <div class="mb-4 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold uppercase" style="color: var(--on-surface);">Processing: {selectedFile?.filename ?? ''}</span>
            <Badge text="RUNNING" variant="secondary" />
            <Badge text="RO-ED AI" variant="success" />
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
            <span class="material-symbols-outlined text-3xl" style="color: var(--tertiary);">error</span>
            <span class="text-sm font-bold uppercase" style="color: var(--on-surface);">FAILED TO LOAD RESULTS</span>
            <span class="text-[10px] font-mono" style="color: var(--outline);">{loadError}</span>
            <div class="flex gap-3">
              {#if selectedFile?.jobId}
                <button class="text-[10px] font-bold uppercase px-3 py-2 border-2 cursor-pointer"
                  style="border-color: var(--primary); color: var(--primary); background: transparent;"
                  onclick={() => { loadError = ''; loadJobResult(selectedFile.jobId); }}>
                  RETRY
                </button>
                <a href="/history?job={selectedFile.jobId}" class="text-[10px] font-bold uppercase no-underline px-3 py-2 border-2"
                  style="border-color: var(--on-surface); color: var(--on-surface);">
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
            <div class="flex gap-2 mb-2">
              <button
                class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
                style="border-color: var(--on-surface); background: {reviewMode === 'view' ? 'var(--on-surface)' : 'var(--surface)'}; color: {reviewMode === 'view' ? 'var(--surface)' : 'var(--on-surface)'};"
                onclick={() => reviewMode = 'view'}>VIEW</button>
              <button
                class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
                style="border-color: var(--on-surface); background: {reviewMode === 'review' ? 'var(--on-surface)' : 'var(--surface)'}; color: {reviewMode === 'review' ? 'var(--surface)' : 'var(--on-surface)'};"
                onclick={() => reviewMode = 'review'}>REVIEW + EDIT</button>
            </div>
            {#if reviewMode === 'review'}
              <ReviewSplitView
                jobId={selectedJob.job_id}
                job={selectedJob}
                onApprove={() => { reviewMode = 'view'; reviewToast('Approved'); }}
                onReject={() => { reviewMode = 'view'; reviewToast('Rejected'); }}
                onClose={() => { reviewMode = 'view'; }}
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
            <KpiCard title="TOTAL" value="{batchSummary.total}" icon="folder" accent="#006f7c" subtitle="PDFs processed" />
            <KpiCard title="AVG ACCURACY" value="{batchSummary.avg_accuracy}%" progress={batchSummary.avg_accuracy} accent={getAccuracyColor(batchSummary.avg_accuracy)} />
            <KpiCard title="TOTAL ITEMS" value="{batchSummary.total_items}" icon="inventory_2" accent="#007518" />
            <KpiCard title="TOTAL COST" value="${batchSummary.total_cost}" icon="payments" accent="#006f7c" />
          </div>
        </div>

        {#each queue.filter(f => f.status === 'done' && f.jobId) as entry}
          <ResultAccordion job={jobResults[entry.jobId]} defaultOpen={false} />
        {/each}

      {:else if selectedFile?.status === 'error'}
        <!-- Error state -->
        <div class="flex flex-col items-center justify-center h-64">
          <span class="material-symbols-outlined text-4xl" style="color: var(--tertiary);">error</span>
          <div class="mt-2 text-sm font-bold uppercase" style="color: var(--on-surface);">{selectedFile.filename}</div>
          <div class="text-xs mt-1 font-mono" style="color: var(--tertiary);">
            {selectedFile.stepLabel || 'FAILED — pipeline error'}
          </div>
          <div class="mt-3 text-[10px] uppercase" style="color: var(--outline);">
            Clear queue and re-upload to retry
          </div>
        </div>

      {:else if selectedFile}
        <!-- Selected file waiting — show PDF preview -->
        {#if selectedFile.savedPath}
          {@const previewFilename = selectedFile.savedPath.split('/').pop()}
          <div class="border-2" style="border-color: var(--on-surface);">
            <div class="dark-bar flex items-center justify-between text-xs">
              <span>PDF_PREVIEW — {selectedFile.filename}</span>
              <span class="text-[10px]" style="color: var(--primary-container);">
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
            <div class="text-xs mt-1" style="color: var(--outline);">
              {selectedFile.status === 'queued' ? 'Waiting to process — click EXECUTE' : selectedFile.status === 'duplicate' ? 'Duplicate — view results or reprocess' : selectedFile.status}
            </div>
          </div>
        {/if}

      {:else}
        <!-- No file selected -->
        <div class="flex flex-col items-center justify-center h-64 opacity-20">
          <span class="material-symbols-outlined text-4xl" style="color: var(--on-surface);">arrow_back</span>
          <div class="mt-2 text-sm font-bold uppercase" style="color: var(--on-surface);">SELECT A FILE</div>
          <div class="text-xs mt-1" style="color: var(--outline);">Click a file in the queue to view details</div>
        </div>
      {/if}
    </div>
  </div>
{/if}
