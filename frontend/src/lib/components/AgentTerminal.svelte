<script lang="ts">
  // ─────────────────────────────────────────────────────────────────────────
  // AgentTerminal
  //
  // Two modes:
  //   1) LEGACY (V7 WebSocket flow): pass `lines/running/summary` props.
  //      Renders the original cli-style log + completion box.
  //   2) V11 SSE STREAM: pass `jobId`. Component opens an EventSource on
  //      `/api/extract-v11/stream/{jobId}` and renders live router decisions,
  //      a page strip, and a running cost split.
  //
  // Both modes coexist — when `jobId` is set the SSE pane takes over the
  // terminal body, otherwise the legacy log is shown.
  // ─────────────────────────────────────────────────────────────────────────

  import { auth } from '$lib/stores/auth.svelte';

  type LegacyLine = { text: string; type: string };

  type SseLine = {
    text: string;
    color: string;
    raw: any;
    eventType: string;
  };

  type PageBox = {
    page: number;
    /** route bucket: V7 | V10P | DROP | PENDING */
    route: 'V7' | 'V10P' | 'DROP' | 'PENDING';
    verdict?: string;
    confidence?: number;
    penalty?: number;
    typed?: number;
    features?: string;
    evidence?: string;
  };

  // ─── Atlas V14 naming ─────────────────────────────────────────────────────
  // Orchestrator = ATLAS V14; typed branch = ATLAS SWIFT (V14-1);
  // handwriting branch = ATLAS VISION (V14-2). Legacy engine keys map here too.
  const PIPELINE_LABEL: Record<string, string> = {
    V7: 'ATLAS SWIFT', V10_PRO: 'ATLAS VISION', V11: 'ATLAS V14',
    Veritas: 'ATLAS SWIFT', Scrivener: 'ATLAS VISION', Maestro: 'ATLAS V14',
    VERITAS: 'ATLAS SWIFT', SCRIVENER: 'ATLAS VISION', MAESTRO: 'ATLAS V14',
    presto: 'ATLAS SWIFT', PRESTO: 'ATLAS SWIFT', Swift: 'ATLAS SWIFT',
    scribe: 'ATLAS VISION', SCRIBE: 'ATLAS VISION', Vision: 'ATLAS VISION',
    atlas: 'ATLAS V14', ATLAS: 'ATLAS V14',
  };
  const VERDICT_LABEL: Record<string, string> = {
    typed: 'PRINTED', handwritten: 'INKED', attachment: 'EXTRA',
    PRINTED: 'PRINTED', INKED: 'INKED', EXTRA: 'EXTRA',
  };
  function pipeLabel(s?: string): string { return PIPELINE_LABEL[s || ''] || s || '?'; }
  function verdictLabel(s?: string): string { return VERDICT_LABEL[s || ''] || s || '?'; }
  function confBand(c: number): string {
    if (!Number.isFinite(c)) return '?';
    if (c >= 0.9) return 'high';
    if (c >= 0.6) return 'med';
    return 'low';
  }
  function verdictColor(v: string): string {
    switch (v) {
      case 'PRINTED': return '#93c5fd';
      case 'INKED':   return '#fdba74';
      case 'EXTRA':   return '#a1a1aa';
      default:        return '#dddddd';
    }
  }
  function pipelineColor(p: string): string {
    switch (p) {
      case 'ATLAS SWIFT':  return '#93c5fd';
      case 'ATLAS VISION': return '#fdba74';
      case 'ATLAS V14':    return '#c4b5fd';
      default:             return '#9ca3af';
    }
  }

  let {
    filename = '',
    lines = [] as LegacyLine[],
    running = false,
    summary = null as any,
    jobId = null as string | null,
  } = $props();

  let terminal: HTMLDivElement | null = $state(null);

  // ─── Legacy spinner ───────────────────────────────────────────────────────
  let frame = $state(0);
  const spinChars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
  const spin = $derived(spinChars[frame % spinChars.length]);

  $effect(() => {
    if (running || sseActive) {
      const iv = setInterval(() => { frame++; }, 80);
      return () => clearInterval(iv);
    }
  });

  // ─── SSE state ────────────────────────────────────────────────────────────
  let sseLines = $state<SseLine[]>([]);
  let sseActive = $state(false);
  let sseDone = $state(false);
  let sseFailed = $state(false);
  let paused = $state(false);
  let pageBoxes = $state<PageBox[]>([]);
  let costV7 = $state(0);
  let costV10P = $state(0);
  let totalCost = $state(0);
  let totalDuration = $state(0);
  let pendingBuffer = $state<SseLine[]>([]);

  // ─── Mid-stage heartbeat ──────────────────────────────────────────────────
  // Veritas/Scrivener (Swift/Vision) run as one blocking call that emits nothing
  // between STAGE_START and STAGE_DONE (60–150s). Show a live "extracting…" line
  // so the terminal doesn't look frozen. Cleared on the stage's terminal event.
  let activeStage = $state<string | null>(null);
  let stageStartTs = $state(0);
  let stageTick = $state(0);  // bumped every second to refresh the elapsed counter
  const stageElapsed = $derived.by(() => {
    void stageTick;  // re-evaluate every second
    if (!activeStage || !stageStartTs) return 0;
    return Math.max(0, Math.round((Date.now() - stageStartTs) / 1000));
  });
  $effect(() => {
    if (!activeStage) return;
    const iv = setInterval(() => { stageTick++; }, 1000);
    return () => clearInterval(iv);
  });

  let es: EventSource | null = null;

  function colorForEvent(ev: string, payload?: any): string {
    switch (ev) {
      case 'JOB_START':   return '#67e8f9';
      case 'CLASSIFY': {
        const v = verdictLabel(payload?.verdict);
        return verdictColor(v);
      }
      case 'ROUTE':       return '#fde047';
      case 'STAGE_START':
      case 'STAGE_DONE':
      case 'STAGE_DETAIL': {
        const lbl = pipeLabel(payload?.label || payload?.pipeline || payload?.stage || payload?.name);
        return ev === 'STAGE_DETAIL' ? '#6b7280' : pipelineColor(lbl);
      }
      case 'MERGE':       return '#fdba74';
      case 'DB_SAVE':     return '#86efac';
      case 'DONE':        return '#4ade80';
      case 'FAIL':        return '#f87171';
      default:            return '#9ca3af';
    }
  }

  function fmt(n: any, digits = 3): string {
    const v = Number(n);
    return Number.isFinite(v) ? v.toFixed(digits) : String(n ?? '');
  }

  function formatLine(ev: string, d: any): string {
    try {
      const fmtTok = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
      switch (ev) {
        case 'JOB_START': {
          const file = d.file ?? d.filename ?? '?';
          const pages = d.pages ?? d.page_count ?? '?';
          const sizeMb = d.size_mb ?? d.size ?? null;
          const sizeStr = sizeMb !== null && sizeMb !== undefined ? ` size=${fmt(sizeMb, 2)}MB` : '';
          return `[ATLAS V14] start file=${file} pages=${pages}${sizeStr}`;
        }
        case 'CLASSIFY': {
          const p = d.page ?? d.p ?? '?';
          const rawV = d.verdict ?? '';
          const v = verdictLabel(rawV);
          const showOld = !VERDICT_LABEL[rawV] && rawV ? ` (${rawV})` : '';
          const c = Number(d.confidence ?? d.conf ?? 0);
          const band = confBand(c);
          const pct = Number.isFinite(c) ? Math.round(c * 100) : 0;
          const ev2 = d.evidence ?? d.reason ?? '';
          const evStr = ev2 ? `  evidence: ${ev2}` : '';
          return `[SCAN] p${p}  ${v}${showOld}  conf=${band} (${pct}%)${evStr}`;
        }
        case 'ROUTE': {
          const v7 = d.v7_pages ?? d.v7 ?? d.V7 ?? d.veritas ?? [];
          const vp = d.v10_pro_pages ?? d.v10_pro ?? d.V10_PRO ?? d.v10p ?? d.scrivener ?? [];
          const dropped = d.dropped ?? d.drop ?? d.skip ?? [];
          const a = (arr: any[]) => `[${(arr as any[]).join(',')}] (${(arr as any[]).length})`;
          return `[ROUTE]  Swift pages=${a(v7)}  Vision pages=${a(vp)}  SKIP=${a(dropped)}`;
        }
        case 'STAGE_START': {
          const lbl = pipeLabel(d.label ?? d.pipeline ?? d.stage ?? d.name);
          const pages = d.pages ?? [];
          return `[${lbl}] start pages=[${(pages as any[]).join(',')}]`;
        }
        case 'STAGE_DONE': {
          const lbl = pipeLabel(d.label ?? d.pipeline ?? d.stage ?? d.name);
          const dur = d.duration ?? d.duration_s ?? 0;
          const cost = d.cost ?? d.cost_usd ?? 0;
          const inT = d.tokens_in ?? d.in ?? 0;
          const outT = d.tokens_out ?? d.out ?? 0;
          return `[${lbl}] DONE  ${fmt(dur, 1)}s  $${fmt(cost, 3)}  in=${fmtTok(inT)}  out=${fmtTok(outT)}`;
        }
        case 'STAGE_DETAIL': {
          const lbl = pipeLabel(d.label ?? d.pipeline ?? d.stage ?? d.name);
          const step = d.step ?? d.phase ?? '?';
          const msg = d.msg ?? d.message ?? d.detail ?? '';
          return `  └─ [${lbl}] ${step}: ${msg}`;
        }
        case 'MERGE':
          return `[MERGE] conflicts=${d.conflicts ?? 0} resolved=${d.resolved ?? d.resolver ?? '?'}`;
        case 'DB_SAVE':
          return `[DB_SAVE] job=${d.job ?? d.job_id ?? '?'} decls=${d.decls ?? d.declarations ?? 0} items=${d.items ?? 0}`;
        case 'DONE': {
          const dur = d.duration ?? d.total_s ?? 0;
          const cost = d.cost ?? d.total_cost ?? 0;
          const inT = d.tokens_in ?? d.in ?? 0;
          const outT = d.tokens_out ?? d.out ?? 0;
          const tokStr = (inT || outT) ? `  tokens ${fmtTok(inT)}/${fmtTok(outT)}` : '';
          return `[ATLAS V14] DONE  total ${fmt(dur, 1)}s  $${fmt(cost, 3)}${tokStr}`;
        }
        case 'FAIL':
          return `[FAIL] stage=${pipeLabel(d.label ?? d.pipeline ?? d.stage)} error=${d.error ?? d.message ?? '?'}`;
        default:
          return `[${ev}] ${JSON.stringify(d)}`;
      }
    } catch {
      return `[${ev}] (parse error)`;
    }
  }

  function pushLine(ev: string, payload: any) {
    const text = formatLine(ev, payload);
    const line: SseLine = {
      text,
      color: colorForEvent(ev, payload),
      raw: payload,
      eventType: ev,
    };
    if (paused) {
      pendingBuffer.push(line);
    } else {
      sseLines = [...sseLines, line];
    }
  }

  function applyClassify(d: any) {
    const page = Number(d.page ?? d.p);
    if (!Number.isFinite(page)) return;
    const verdict = verdictLabel(d.verdict ?? '');
    const confidence = Number(d.confidence ?? d.conf ?? 0);
    const penalty = Number(d.penalty ?? d.pen ?? 0);
    const typed = Number(d.typed ?? 0);
    const features = d.features ? JSON.stringify(d.features) : '';
    const evidence = d.evidence ?? d.reason ?? '';

    const idx = pageBoxes.findIndex(b => b.page === page);
    const next: PageBox = {
      page,
      route: 'PENDING',
      verdict,
      confidence,
      penalty,
      typed,
      features,
      evidence,
    };
    if (idx >= 0) {
      pageBoxes[idx] = { ...pageBoxes[idx], ...next, route: pageBoxes[idx].route };
      pageBoxes = [...pageBoxes];
    } else {
      pageBoxes = [...pageBoxes, next].sort((a, b) => a.page - b.page);
    }
  }

  function applyRoute(d: any) {
    const v7: number[] = (d.v7_pages ?? d.v7 ?? d.V7 ?? d.veritas ?? []).map((n: any) => Number(n));
    const vp: number[] = (d.v10_pro_pages ?? d.v10_pro ?? d.V10_PRO ?? d.v10p ?? d.scrivener ?? []).map((n: any) => Number(n));
    const dropped: number[] = (d.dropped ?? d.drop ?? d.skip ?? []).map((n: any) => Number(n));

    const updated = [...pageBoxes];
    const ensure = (page: number, route: PageBox['route']) => {
      const idx = updated.findIndex(b => b.page === page);
      if (idx >= 0) updated[idx] = { ...updated[idx], route };
      else updated.push({ page, route });
    };
    v7.forEach(p => ensure(p, 'V7'));
    vp.forEach(p => ensure(p, 'V10P'));
    dropped.forEach(p => ensure(p, 'DROP'));
    pageBoxes = updated.sort((a, b) => a.page - b.page);
  }

  function applyStageDone(d: any) {
    const lbl = pipeLabel(d.label ?? d.pipeline ?? d.stage ?? d.name).toUpperCase();
    const stageStr = String(d.stage ?? d.name ?? '').toUpperCase();
    const cost = Number(d.cost ?? d.cost_usd ?? 0);
    const dur = Number(d.duration ?? d.duration_s ?? 0);
    if (lbl.includes('VISION') || stageStr.includes('V10')) costV10P += cost;
    else if (lbl.includes('SWIFT') || stageStr.includes('V7')) costV7 += cost;
    totalCost += cost;
    totalDuration += dur;
  }

  function applyDone(d: any) {
    const c = Number(d.cost ?? d.total_cost);
    const t = Number(d.duration ?? d.total_s);
    if (Number.isFinite(c)) totalCost = c;
    if (Number.isFinite(t)) totalDuration = t;
  }

  function attachListeners(stream: EventSource) {
    const events = [
      'JOB_START', 'CLASSIFY', 'ROUTE',
      'STAGE_START', 'STAGE_DONE', 'STAGE_DETAIL',
      'MERGE', 'DB_SAVE', 'DONE', 'FAIL',
    ];
    for (const ev of events) {
      stream.addEventListener(ev, (e: MessageEvent) => {
        let payload: any = {};
        try { payload = JSON.parse(e.data); } catch { payload = { raw: e.data }; }
        pushLine(ev, payload);
        if (ev === 'CLASSIFY') applyClassify(payload);
        else if (ev === 'ROUTE') applyRoute(payload);
        else if (ev === 'STAGE_START') {
          // Open a heartbeat for this stage (cleared by its terminal event).
          activeStage = pipeLabel(payload?.label ?? payload?.pipeline ?? payload?.stage ?? payload?.name);
          stageStartTs = Date.now();
        }
        else if (ev === 'STAGE_DONE') { applyStageDone(payload); activeStage = null; }
        else if (ev === 'MERGE') { activeStage = null; }
        else if (ev === 'DONE') {
          applyDone(payload);
          activeStage = null;
          sseDone = true;
          closeStream();
        } else if (ev === 'FAIL') {
          activeStage = null;
          sseFailed = true;
          closeStream();
        }
      });
    }
    stream.onerror = () => {
      // On unexpected close before DONE/FAIL, mark inactive but keep lines.
      if (!sseDone && !sseFailed) {
        pushLine('FAIL', { stage: 'sse', error: 'connection lost' });
        sseFailed = true;
      }
      closeStream();
    };
  }

  function closeStream() {
    if (es) { try { es.close(); } catch {} }
    es = null;
    sseActive = false;
  }

  // Open / close stream when jobId changes
  $effect(() => {
    if (!jobId) {
      closeStream();
      return;
    }
    // Reset
    sseLines = [];
    pageBoxes = [];
    costV7 = 0;
    costV10P = 0;
    totalCost = 0;
    totalDuration = 0;
    sseDone = false;
    sseFailed = false;
    paused = false;
    pendingBuffer = [];
    activeStage = null;
    stageStartTs = 0;

    // EventSource cannot set an Authorization header — the stream route takes
    // the JWT as a query param instead (same as the PDF / page-image routes).
    const stream = new EventSource(
      `/api/extract-v11/stream/${jobId}?token=${encodeURIComponent(auth.token ?? '')}`
    );
    es = stream;
    sseActive = true;
    attachListeners(stream);

    return () => closeStream();
  });

  // Auto-scroll
  $effect(() => {
    if ((sseLines.length || lines.length) && terminal) {
      requestAnimationFrame(() => { if (terminal) terminal.scrollTop = terminal.scrollHeight; });
    }
  });

  function togglePause() {
    if (paused) {
      // flush buffered
      if (pendingBuffer.length) {
        sseLines = [...sseLines, ...pendingBuffer];
        pendingBuffer = [];
      }
      paused = false;
    } else {
      paused = true;
    }
  }

  function clearLines() {
    sseLines = [];
    pendingBuffer = [];
  }

  function pageBoxStyle(b: PageBox): { bg: string; fg: string; border: string; label: string } {
    switch (b.route) {
      case 'V7':   return { bg: '#bfdbfe', fg: '#1e3a8a', border: '#3b82f6', label: `${b.page}:VER` };
      case 'V10P': return { bg: '#fed7aa', fg: '#7c2d12', border: '#fb923c', label: `${b.page}:SCR` };
      case 'DROP': return { bg: '#a1a1aa', fg: '#27272a', border: '#52525b', label: `${b.page}:SKIP` };
      default:     return { bg: '#0a0a0f', fg: '#71717a', border: '#3f3f46', label: `${b.page}:?` };
    }
  }

  function pageTooltip(b: PageBox): string {
    const routeLabel = b.route === 'V7' ? 'Swift' : b.route === 'V10P' ? 'Vision' : b.route === 'DROP' ? 'SKIP' : '?';
    const parts: string[] = [`Page ${b.page}`];
    if (b.verdict) parts.push(`verdict=${verdictLabel(b.verdict)}`);
    if (b.confidence !== undefined) {
      const c = b.confidence ?? 0;
      parts.push(`conf=${confBand(c)} (${Math.round(c * 100)}%)`);
    }
    if (b.evidence) parts.push(`evidence: ${b.evidence}`);
    if (b.penalty !== undefined) parts.push(`pen=${(b.penalty ?? 0).toFixed(2)}`);
    if (b.typed !== undefined) parts.push(`typed=${(b.typed ?? 0).toFixed(2)}`);
    if (b.features) parts.push(`features=${b.features}`);
    parts.push(`route=${routeLabel}`);
    return parts.join('  ');
  }
</script>

<!-- Page strip + cost split badge (only in SSE mode) -->
{#if jobId}
  <div class="mb-2 flex items-center justify-between gap-3 flex-wrap">
    <!-- Page strip -->
    <div class="flex items-center gap-1 flex-wrap">
      <span class="text-[10px] font-mono uppercase font-black" style="color: var(--on-surface);">PAGES</span>
      {#each pageBoxes as b}
        {@const sty = pageBoxStyle(b)}
        <div
          title={pageTooltip(b)}
          class="border-2"
          style="
            min-width: 24px; height: 24px;
            padding: 0 4px;
            display: inline-flex; align-items: center; justify-content: center;
            font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px; font-weight: 900;
            background: {sty.bg}; color: {sty.fg};
            border-color: {sty.border};
            box-shadow: 2px 2px 0px 0px var(--on-surface);
          "
        >{sty.label}</div>
      {/each}
      {#if pageBoxes.length === 0}
        <span class="text-[10px] font-mono" style="color: var(--outline);">awaiting CLASSIFY…</span>
      {/if}
    </div>

    <!-- Cost split badge -->
    <div
      class="border-2 px-2 py-1 font-mono uppercase font-black text-[10px]"
      style="
        border-color: var(--line);
        background: var(--surface);
        color: var(--on-surface);
        box-shadow: 2px 2px 0px 0px var(--on-surface);
        white-space: nowrap;
      "
    >
      SWIFT ${costV7.toFixed(2)} + VISION ${costV10P.toFixed(2)} = ${totalCost.toFixed(2)}
    </div>
  </div>
{/if}

<div style="border: 2px solid #383832; box-shadow: 4px 4px 0px 0px #383832;">
  <!-- Title bar -->
  <div style="background: #111118; border-bottom: 1px solid #1a1a2e; padding: 6px 12px; display: flex; align-items: center; justify-content: space-between;">
    <div style="display: flex; align-items: center; gap: 8px;">
      <div style="display: flex; gap: 4px;">
        <span style="width: 8px; height: 8px; border-radius: 50%; background: #ef4444; display: inline-block;"></span>
        <span style="width: 8px; height: 8px; border-radius: 50%; background: #eab308; display: inline-block;"></span>
        <span style="width: 8px; height: 8px; border-radius: 50%; background: #22c55e; display: inline-block;"></span>
      </div>
      <span style="color: #4b5563; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; font-weight: bold;">
        cityagent — {filename || (jobId ? `job:${jobId}` : 'idle')}
      </span>
    </div>
    <div style="display: flex; align-items: center; gap: 8px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px;">
      {#if jobId}
        {#if sseActive}
          <span style="color: #38bdf8;">{spin} STREAMING</span>
        {:else if sseDone}
          <span style="color: #22c55e;">● DONE</span>
        {:else if sseFailed}
          <span style="color: #ef4444;">● FAIL</span>
        {/if}
        <button
          type="button"
          onclick={togglePause}
          style="background: #1f2937; color: #d1d5db; border: 1px solid #374151; padding: 2px 8px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px; cursor: pointer; text-transform: uppercase; font-weight: bold;"
        >{paused ? 'RESUME' : 'PAUSE'}</button>
        <button
          type="button"
          onclick={clearLines}
          style="background: #1f2937; color: #d1d5db; border: 1px solid #374151; padding: 2px 8px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px; cursor: pointer; text-transform: uppercase; font-weight: bold;"
        >CLEAR</button>
      {:else if running}
        <span style="color: #38bdf8;">{spin} RUNNING</span>
      {:else if summary}
        <span style="color: #22c55e;">● DONE</span>
      {/if}
    </div>
  </div>

  <!-- Terminal body -->
  <div
    bind:this={terminal}
    style="
      background: #0a0a0f;
      height: 520px;
      overflow-y: auto;
      font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
      font-size: 11px;
      line-height: 1.6;
      padding: 12px 14px;
      color: #9ca3af;
    "
  >
    {#if jobId}
      <!-- SSE mode -->
      {#each sseLines as line}
        <div style="color: {line.color};">{line.text}</div>
      {/each}

      {#if activeStage && !paused && !sseDone && !sseFailed}
        <div style="color: {pipelineColor(activeStage)};">
          {spin} [{activeStage}] extracting… {stageElapsed}s elapsed
        </div>
      {/if}

      {#if sseActive && !paused && sseLines.length === 0}
        <div style="color: #6b7280;">// awaiting JOB_START …</div>
      {/if}

      {#if paused}
        <div style="color: #fbbf24; margin-top: 4px;">// PAUSED — {pendingBuffer.length} buffered</div>
      {/if}

      {#if sseActive && !paused}
        <div style="margin-top: 4px; display: flex; align-items: center; gap: 8px;">
          <span style="color: #38bdf8; font-size: 13px;">{spin}</span>
          <span class="animate-pulse" style="display: inline-block; width: 6px; height: 12px; background: #38bdf8;"></span>
        </div>
      {/if}
    {:else}
      <!-- Legacy mode (V7 WebSocket flow) -->
      {#each lines as line}
        {#if line.type === 'header'}
          <div style="color: #1e1e2e; margin-top: 6px;">────────────────────────────────────────────</div>
          <div style="color: #e5e7eb; font-weight: bold; margin-bottom: 2px;">{line.text}</div>
        {:else if line.type === 'success' && line.text.includes('═')}
          <div style="color: #22c55e; font-weight: bold; margin-top: 8px;">{line.text}</div>
        {:else if line.type === 'success'}
          <div style="color: #22c55e;">{line.text}</div>
        {:else if line.type === 'warning'}
          <div style="color: #eab308;">{line.text}</div>
        {:else if line.type === 'error'}
          <div style="color: #ef4444;">{line.text}</div>
        {:else if line.type === 'data'}
          <div style="color: #6b7280; padding-left: 4px;">{line.text}</div>
        {:else}
          <div style="color: #9ca3af;">{line.text}</div>
        {/if}
      {/each}

      {#if running}
        <div style="margin-top: 4px; display: flex; align-items: center; gap: 8px;">
          <span style="color: #38bdf8; font-size: 13px;">{spin}</span>
          <span style="color: #38bdf8; font-size: 10px;">Processing...</span>
          <span class="animate-pulse" style="display: inline-block; width: 6px; height: 12px; background: #38bdf8;"></span>
        </div>
      {/if}

      {#if summary && !running}
        <div style="margin-top: 8px; border: 1px solid #14532d; background: #052e16; padding: 8px 12px;">
          <div style="color: #22c55e; font-weight: bold; font-size: 11px;">✓ EXTRACTION COMPLETE</div>
          <div style="margin-top: 4px; color: #4b5563; font-size: 10px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px 12px;">
            <div>Items <span style="color: #d1d5db; font-weight: bold;">{summary.items}</span></div>
            <div>Accuracy <span style="color: {summary.accuracy >= 90 ? '#22c55e' : '#eab308'}; font-weight: bold;">{summary.accuracy?.toFixed(1)}%</span></div>
            <div>Status <span style="color: #22c55e;">ACCEPTED</span></div>
            <div>Time <span style="color: #9ca3af;">{summary.duration?.toFixed(1)}s</span></div>
            <div>Cost <span style="color: #eab308;">${summary.cost?.toFixed(3)}</span></div>
            <div>Model <span style="color: #a78bfa;">gemini-flash-lite</span></div>
          </div>
        </div>
      {/if}
    {/if}

    <div style="height: 1px;"></div>
  </div>
</div>
