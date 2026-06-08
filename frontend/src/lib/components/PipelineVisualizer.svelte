<script lang="ts">
  type StepStatus = 'pending' | 'running' | 'done' | 'error';

  type PipelineStep = {
    id: string;
    label: string;
    icon?: string;
    status: StepStatus;
    detail?: string;
    time?: string;
  };

  let {
    mode = 'v7',
    steps = $bindable([] as PipelineStep[]),
    filename = '',
    complete = false,
    summary = null as { items: number; accuracy: number; cost: number; duration: number; corrections: number; aiTables: number; declFilled: number } | null,
  }: {
    mode?: 'v7' | 'v10' | 'v11' | string;
    steps?: PipelineStep[];
    filename?: string;
    complete?: boolean;
    summary?: any;
  } = $props();

  let spinnerFrame = $state(0);
  const spinnerChars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

  $effect(() => {
    if (!complete && steps.some(s => s.status === 'running')) {
      const interval = setInterval(() => { spinnerFrame++; }, 80);
      return () => clearInterval(interval);
    }
  });

  const spinner = $derived(spinnerChars[spinnerFrame % spinnerChars.length]);

  // Per-pipeline step blueprints
  const PIPELINE_STEPS: Record<string, { key: string; label: string }[]> = {
    v7: [
      { key: 'HD_SPLITTER', label: 'Splitting PDF (300 DPI)' },
      { key: 'VISION_EXTRACT', label: 'Vision agents (per page)' },
      { key: 'DOC_ANALYSIS', label: 'Document classifier' },
      { key: 'DECL_AGENT', label: 'Declaration master + 16 column agents' },
      { key: 'ITEMS_AGENT', label: 'Items master + 10 column agents' },
      { key: 'CROSS_VAL', label: 'Cross-validation' },
      { key: 'VERIFIER', label: 'Sonnet verifier vs page images' },
      { key: 'FEE_VERIFY', label: 'Fee verification' },
    ],
    v10: [
      { key: 'HW_DETECT', label: 'HW detector (haiku)' },
      { key: 'FILTER', label: 'Page filter' },
      { key: 'READER', label: 'Holistic reader (gemini-pro)' },
      { key: 'VERIFIER', label: 'Sonnet verifier' },
      { key: 'HW_VOTE', label: 'Multi-DPI 3-model vote (5 critical fields)' },
      { key: 'DECL_ZOOM', label: 'Decl-no zoom (opus 400 DPI)' },
      { key: 'POSTFIX', label: 'Deterministic post-fix' },
    ],
    v11: [
      { key: 'CLASSIFIER', label: 'Page classifier (haiku)' },
      { key: 'SPLITTER', label: 'Split PDF by labels' },
      { key: 'V7_BRANCH', label: 'Veritas branch (typed pages)' },
      { key: 'V10_BRANCH', label: 'Scrivener branch (HW pages)' },
      { key: 'MERGER', label: 'Merge Veritas + Scrivener results' },
    ],
  };

  const currentSteps = $derived(PIPELINE_STEPS[mode as keyof typeof PIPELINE_STEPS] || PIPELINE_STEPS.v7);

  // Merge blueprint with live status from `steps` prop. Match by id/key (case-insensitive).
  type MergedStep = { key: string; label: string; status: StepStatus; detail?: string; time?: string; icon?: string };

  const mergedSteps = $derived.by<MergedStep[]>(() => {
    return currentSteps.map(blueprint => {
      const live = steps.find(s => {
        const sid = (s.id || '').toString().toUpperCase();
        const slabel = (s.label || '').toString().toUpperCase();
        const key = blueprint.key.toUpperCase();
        return sid === key || slabel.includes(key) || key.includes(sid);
      });
      return {
        key: blueprint.key,
        label: blueprint.label,
        status: (live?.status as StepStatus) || 'pending',
        detail: live?.detail,
        time: live?.time,
        icon: live?.icon,
      };
    });
  });

  function statusIcon(s: StepStatus): string {
    if (s === 'done') return '✓';
    if (s === 'running') return spinner;
    if (s === 'error') return '✗';
    return '·';
  }

  function statusColor(s: StepStatus): string {
    if (s === 'done') return 'var(--success)';
    if (s === 'running') return '#38bdf8';
    if (s === 'error') return '#ef4444';
    return '#374151';
  }

  // V11 layout helpers — split the merged steps into pre-fork, branches, post-join
  const v11Layout = $derived.by(() => {
    if (mode !== 'v11') return null;
    const pre = mergedSteps.filter(s => s.key === 'CLASSIFIER' || s.key === 'SPLITTER');
    const v7Branch = mergedSteps.find(s => s.key === 'V7_BRANCH');
    const v10Branch = mergedSteps.find(s => s.key === 'V10_BRANCH');
    const post = mergedSteps.filter(s => s.key === 'MERGER');
    return { pre, v7Branch, v10Branch, post };
  });

  function pipelineLabel(m: string): string {
    if (m === 'v10' || m === 'v10_pro') return 'SCRIVENER — HOLISTIC HANDWRITING';
    if (m === 'v11') return 'MAESTRO — HYBRID (VERITAS + SCRIVENER)';
    return 'VERITAS — TYPED DOCUMENTS';
  }
</script>

<div style="border: 1px solid var(--on-surface); background: #0a0a0f; font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace; font-size: 11px; color: #6b7280;">

  <!-- Header -->
  <div style="padding: 8px 14px; border-bottom: 1px solid #1a1a2e; display: flex; justify-content: space-between; align-items: center;">
    <div style="display: flex; align-items: center; gap: 8px;">
      <span style="color: var(--success); font-size: 8px;">●</span>
      <span style="color: #4b5563; font-size: 10px; font-weight: bold;">RO-ED AI PIPELINE</span>
      <span style="color: #a78bfa; font-size: 9px; border: 1px solid #4c1d95; padding: 1px 6px; border-radius: 2px;">{pipelineLabel(mode)}</span>
      <span style="color: #eab308; font-size: 10px;">{filename}</span>
    </div>
    {#if !complete && mergedSteps.some(s => s.status === 'running')}
      <span style="color: #38bdf8; font-size: 9px;">{spinner} PROCESSING</span>
    {:else if complete}
      <span style="color: var(--success); font-size: 9px;">● COMPLETE</span>
    {/if}
  </div>

  <!-- Flow Diagram -->
  <div style="padding: 12px 14px;">

    {#if mode === 'v11' && v11Layout}
      <!-- Pre-fork: Classifier → Splitter -->
      <div style="display: flex; flex-wrap: wrap; gap: 4px 2px; align-items: center;">
        {#each v11Layout.pre as step, i}
          <div style="display: flex; align-items: center; gap: 2px;">
            <div style="
              border: 1px solid {statusColor(step.status)};
              padding: 4px 8px;
              min-width: 70px;
              {step.status === 'running' ? `box-shadow: 0 0 8px ${statusColor(step.status)}40;` : ''}
              {step.status === 'done' ? 'opacity: 0.85;' : ''}
            ">
              <div style="display: flex; align-items: center; gap: 4px;">
                <span style="color: {statusColor(step.status)}; font-size: 10px;">{statusIcon(step.status)}</span>
                <span style="color: {step.status === 'running' ? '#e5e7eb' : statusColor(step.status)}; font-size: 9px; font-weight: bold;">{step.label}</span>
              </div>
              {#if step.detail}<div style="color: #4b5563; font-size: 8px; margin-top: 2px;">{step.detail}</div>{/if}
            </div>
            <span style="color: {step.status === 'done' ? 'var(--success)' : '#1a1a2e'}; font-size: 10px;">→</span>
          </div>
        {/each}
        <!-- Fork indicator -->
        <span style="color: #a78bfa; font-size: 11px; font-weight: bold; margin: 0 4px;">⫘ FORK</span>
      </div>

      <!-- Parallel branches -->
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; position: relative;">
        <!-- V7 branch -->
        {#if v11Layout.v7Branch}
          {@const s = v11Layout.v7Branch}
          <div style="
            border: 1px dashed {statusColor(s.status)};
            padding: 8px 10px;
            background: #050510;
            {s.status === 'running' ? `box-shadow: 0 0 10px ${statusColor(s.status)}40;` : ''}
          ">
            <div style="color: #60a5fa; font-size: 9px; font-weight: bold; margin-bottom: 4px;">▼ VERITAS BRANCH (typed pages)</div>
            <div style="display: flex; align-items: center; gap: 4px;">
              <span style="color: {statusColor(s.status)}; font-size: 10px;">{statusIcon(s.status)}</span>
              <span style="color: {s.status === 'running' ? '#e5e7eb' : statusColor(s.status)}; font-size: 9px;">{s.label}</span>
            </div>
            {#if s.detail}<div style="color: #4b5563; font-size: 8px; margin-top: 2px;">{s.detail}</div>{/if}
          </div>
        {/if}
        <!-- V10 branch -->
        {#if v11Layout.v10Branch}
          {@const s = v11Layout.v10Branch}
          <div style="
            border: 1px dashed {statusColor(s.status)};
            padding: 8px 10px;
            background: #050510;
            {s.status === 'running' ? `box-shadow: 0 0 10px ${statusColor(s.status)}40;` : ''}
          ">
            <div style="color: var(--warning); font-size: 9px; font-weight: bold; margin-bottom: 4px;">▼ SCRIVENER BRANCH (HW pages)</div>
            <div style="display: flex; align-items: center; gap: 4px;">
              <span style="color: {statusColor(s.status)}; font-size: 10px;">{statusIcon(s.status)}</span>
              <span style="color: {s.status === 'running' ? '#e5e7eb' : statusColor(s.status)}; font-size: 9px;">{s.label}</span>
            </div>
            {#if s.detail}<div style="color: #4b5563; font-size: 8px; margin-top: 2px;">{s.detail}</div>{/if}
          </div>
        {/if}
      </div>

      <!-- Join + post -->
      <div style="display: flex; flex-wrap: wrap; gap: 4px 2px; align-items: center; margin-top: 10px;">
        <span style="color: #a78bfa; font-size: 11px; font-weight: bold; margin-right: 4px;">⫛ JOIN →</span>
        {#each v11Layout.post as step}
          <div style="
            border: 1px solid {statusColor(step.status)};
            padding: 4px 8px;
            min-width: 70px;
            {step.status === 'running' ? `box-shadow: 0 0 8px ${statusColor(step.status)}40;` : ''}
            {step.status === 'done' ? 'opacity: 0.85;' : ''}
          ">
            <div style="display: flex; align-items: center; gap: 4px;">
              <span style="color: {statusColor(step.status)}; font-size: 10px;">{statusIcon(step.status)}</span>
              <span style="color: {step.status === 'running' ? '#e5e7eb' : statusColor(step.status)}; font-size: 9px; font-weight: bold;">{step.label}</span>
            </div>
            {#if step.detail}<div style="color: #4b5563; font-size: 8px; margin-top: 2px;">{step.detail}</div>{/if}
          </div>
        {/each}
      </div>
    {:else}
      <!-- Linear flow for V7 / V10 -->
      <div style="display: flex; flex-wrap: wrap; gap: 4px 2px; align-items: center;">
        {#each mergedSteps as step, i}
          <div style="display: flex; align-items: center; gap: 2px;">
            <div style="
              border: 1px solid {statusColor(step.status)};
              padding: 4px 8px;
              min-width: 70px;
              {step.status === 'running' ? `box-shadow: 0 0 8px ${statusColor(step.status)}40;` : ''}
              {step.status === 'done' ? 'opacity: 0.85;' : ''}
            ">
              <div style="display: flex; align-items: center; gap: 4px;">
                <span style="color: {statusColor(step.status)}; font-size: 10px;">{statusIcon(step.status)}</span>
                <span style="color: {step.status === 'running' ? '#e5e7eb' : statusColor(step.status)}; font-size: 9px; font-weight: bold;">{step.icon ?? ''} {step.label}</span>
              </div>
              {#if step.detail}<div style="color: #4b5563; font-size: 8px; margin-top: 2px;">{step.detail}</div>{/if}
              {#if step.time}<div style="color: #374151; font-size: 8px;">{step.time}</div>{/if}
            </div>
            {#if i < mergedSteps.length - 1}
              <span style="color: {step.status === 'done' ? 'var(--success)' : '#1a1a2e'}; font-size: 10px;">→</span>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    <!-- Progress bar -->
    {#if mergedSteps.length > 0}
      {@const doneCount = mergedSteps.filter(s => s.status === 'done').length}
      {@const totalSteps = mergedSteps.length}
      <div style="margin-top: 8px; display: flex; align-items: center; gap: 6px;">
        <div style="flex: 1; height: 3px; background: #1a1a2e; border-radius: 2px;">
          <div style="width: {(doneCount / totalSteps) * 100}%; height: 100%; background: var(--success); border-radius: 2px; transition: width 0.3s;"></div>
        </div>
        <span style="color: #374151; font-size: 9px;">{doneCount}/{totalSteps}</span>
      </div>
    {/if}

    <!-- Completion summary -->
    {#if complete && summary}
      <div style="margin-top: 10px; border: 1px solid #14532d; background: #052e16; padding: 8px 12px;">
        <div style="color: var(--success); font-weight: bold; font-size: 11px;">✅ EXTRACTION COMPLETE</div>
        <div style="margin-top: 6px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px 12px; font-size: 10px;">
          <div style="color: #4b5563;">Declaration <span style="color: #d1d5db; font-weight: bold;">{summary.declFilled}/16</span></div>
          <div style="color: #4b5563;">Items <span style="color: #d1d5db; font-weight: bold;">{summary.items}</span></div>
          <div style="color: #4b5563;">AI Tables <span style="color: #d1d5db; font-weight: bold;">{summary.aiTables}</span></div>
          <div style="color: #4b5563;">Accuracy <span style="color: var(--success); font-weight: bold;">{summary.accuracy.toFixed(1)}%</span></div>
          <div style="color: #4b5563;">Cost <span style="color: #eab308;">${summary.cost.toFixed(3)}</span></div>
          <div style="color: #4b5563;">Time <span style="color: #9ca3af;">{summary.duration.toFixed(1)}s</span></div>
        </div>
        {#if summary.corrections > 0}
          <div style="margin-top: 4px; color: #eab308; font-size: 9px;">✦ Verifier fixed {summary.corrections} values</div>
        {/if}
      </div>
    {/if}
  </div>
</div>
