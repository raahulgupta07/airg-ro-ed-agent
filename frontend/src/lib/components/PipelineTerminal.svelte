<script lang="ts">
  type LogLine = {
    text: string;
    type: 'detail' | 'success' | 'warning' | 'error' | 'cost' | 'header' | 'data' | 'bar';
    barPercent?: number;
    barColor?: string;
  };

  type StepBox = {
    step: number;
    name: string;
    status: 'running' | 'done' | 'error' | 'skipped' | 'pending';
    duration?: number;
    cost?: number;
    lines: LogLine[];
  };

  let {
    filename = '',
    model = 'gemini-3.1-flash-lite',
    steps = [] as StepBox[],
    complete = false,
    summary = null as { items: number; accuracy: number; decision: string; duration: number; cost: number; pipelineMode?: string; crossValidation?: any } | null,
    collapsed = $bindable(false),
  } = $props();

  let terminal: HTMLDivElement;
  let autoScroll = $state(true);
  let spinnerFrame = $state(0);

  // Braille spinner animation: ⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏
  const spinnerChars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
  const spinner = $derived(spinnerChars[spinnerFrame % spinnerChars.length]);

  $effect(() => {
    if (!complete && steps.some(s => s.status === 'running')) {
      const interval = setInterval(() => { spinnerFrame++; }, 80);
      return () => clearInterval(interval);
    }
  });

  // Auto-scroll to bottom only if user hasn't scrolled up
  $effect(() => {
    if (steps.length && terminal && !collapsed && autoScroll) {
      requestAnimationFrame(() => { terminal.scrollTop = terminal.scrollHeight; });
    }
  });

  function handleScroll() {
    if (!terminal) return;
    const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 40;
    autoScroll = atBottom;
  }

  const doneCount = $derived(steps.filter(s => s.status === 'done' || s.status === 'skipped').length);
  const totalSteps = $derived(Math.max(steps.length, 5));
  const totalCost = $derived(steps.reduce((s, st) => s + (st.cost || 0), 0));
  const totalTime = $derived(steps.filter(s => s.status === 'done').reduce((s, st) => s + (st.duration || 0), 0));

  // Friendly agent-style names
  const agentName = (name: string): string => ({
    'HD_SPLITTER': 'Splitting PDF into HD images (300 DPI)',
    'VISION_EXTRACT': 'AI reading each page',
    'DOC_ANALYSIS': 'Analyzing document structure',
    'AI_ASSEMBLER': 'AI assembling declaration + items',
    'AI_VERIFIER': 'AI verifying results against source',
    'ASSEMBLER_DECLARATION': 'Assembling declaration fields',
    'ASSEMBLER_ITEMS': 'Assembling product items',
    'VALIDATE_SAVE': 'Validating + saving results',
  }[name] || name);
</script>

<div style="border: 2px solid #383832; box-shadow: 4px 4px 0px 0px #383832;">
  {#if collapsed}
    <!-- Collapsed bar -->
    <button class="w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer" style="background: #0a0a0f;">
      <span style="color: #3b3b4f; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 11px;" onclick={() => collapsed = false}>▸ PIPELINE_LOG</span>
      {#if complete}
        <span style="color: #22c55e; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px;">DONE {doneCount}/{totalSteps}</span>
      {/if}
      <span style="flex: 1;"></span>
      <span style="color: #3b3b4f; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px;">{totalTime.toFixed(1)}s │ ${totalCost.toFixed(3)}</span>
    </button>
  {:else}
    <!-- Title bar (fixed) -->
    <div style="background: #111118; border-bottom: 1px solid #1a1a2e; padding: 6px 12px; display: flex; align-items: center; justify-content: space-between;">
      <div style="display: flex; align-items: center; gap: 8px;">
        <!-- Traffic lights -->
        <div style="display: flex; gap: 4px;">
          <span style="width: 8px; height: 8px; border-radius: 50%; background: #ef4444; display: inline-block;"></span>
          <span style="width: 8px; height: 8px; border-radius: 50%; background: #eab308; display: inline-block;"></span>
          <span style="width: 8px; height: 8px; border-radius: 50%; background: #22c55e; display: inline-block;"></span>
        </div>
        <span style="color: #4b5563; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 10px; font-weight: bold; cursor: pointer;" onclick={() => collapsed = true}>
          ro-ed-agent — {filename || 'idle'}
        </span>
      </div>
      <div style="display: flex; align-items: center; gap: 12px; font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 9px;">
        {#if !complete && steps.some(s => s.status === 'running')}
          <span style="color: #38bdf8;">{spinner} RUNNING</span>
        {:else if complete}
          <span style="color: #22c55e;">● DONE</span>
        {/if}
        <span style="color: #374151;">{totalTime.toFixed(1)}s</span>
        <span style="color: #854d0e;">${totalCost.toFixed(3)}</span>
      </div>
    </div>

    <!-- Terminal content (scrollable, fixed height) -->
    <div
      bind:this={terminal}
      onscroll={handleScroll}
      style="
        background: #0a0a0f;
        height: 500px;
        overflow-y: auto;
        overflow-x: hidden;
        font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
        font-size: 11px;
        line-height: 1.7;
        padding: 12px 14px;
        color: #6b7280;
      "
    >
      <!-- Prompt line -->
      <div>
        <span style="color: #22c55e;">❯</span>
        <span style="color: #9ca3af;"> ro-ed extract</span>
        <span style="color: #eab308;"> "{filename}"</span>
        <span style="color: #4b5563;"> --model</span>
        <span style="color: #a78bfa;"> {model}</span>
      </div>
      <div style="color: #1e1e2e; margin-bottom: 4px;">─────────────────────────────────────────────────────────────</div>

      {#each steps as step, idx}
        <!-- Step header -->
        <div style="margin-top: {idx > 0 ? '4px' : '0'};">
          {#if step.status === 'running'}
            <span style="color: #38bdf8;">{spinner}</span>
          {:else if step.status === 'done'}
            <span style="color: #22c55e;">✓</span>
          {:else if step.status === 'error'}
            <span style="color: #ef4444;">✗</span>
          {:else}
            <span style="color: #1e1e2e;">·</span>
          {/if}

          <span style="color: #374151;"> [{step.step}/{totalSteps}]</span>
          <span style="color: {step.status === 'running' ? '#e5e7eb' : step.status === 'done' ? '#6b7280' : '#ef4444'}; font-weight: 600;"> {agentName(step.name)}</span>

          {#if step.status === 'running'}
            <span style="color: #38bdf8;"> {spinnerChars[(spinnerFrame + 2) % spinnerChars.length]}{spinnerChars[(spinnerFrame + 4) % spinnerChars.length]}{spinnerChars[(spinnerFrame + 6) % spinnerChars.length]}</span>
          {/if}

          {#if step.duration && step.duration > 0}
            <span style="color: #1f2937;"> ─</span>
            <span style="color: #374151;"> {step.duration.toFixed(1)}s</span>
          {/if}
          {#if step.cost && step.cost > 0}
            <span style="color: #1f2937;"> ─</span>
            <span style="color: #854d0e;"> ${step.cost.toFixed(4)}</span>
          {/if}
        </div>

        <!-- Step detail lines with left border -->
        {#if step.lines.length > 0}
          {#each step.lines as line}
            <div style="padding-left: 16px; border-left: 1px solid {step.status === 'running' ? '#38bdf8' : step.status === 'done' ? '#1a1a2e' : '#1e3a5f'}; margin-left: 4px; {step.status === 'running' ? 'box-shadow: -2px 0 8px rgba(56,189,248,0.15);' : ''}">
              {#if line.type === 'success'}
                <span style="color: #22c55e; font-size: 10px;">✓ {line.text}</span>
              {:else if line.type === 'warning'}
                <span style="color: #eab308; font-size: 10px;">⚠ {line.text}</span>
              {:else if line.type === 'error'}
                <span style="color: #ef4444; font-size: 10px;">✗ {line.text}</span>
              {:else if line.type === 'data'}
                <span style="color: #4b5563; font-size: 10px;">{line.text}</span>
              {:else if line.type === 'bar' && line.barPercent != null}
                <span style="color: #4b5563; font-size: 10px;">{line.text}</span>
                <span style="display: inline-block; width: 60px; height: 3px; background: #1a1a2e; vertical-align: middle; margin: 0 4px; border-radius: 1px;">
                  <span style="display: block; width: {Math.min(line.barPercent, 100)}%; height: 100%; background: {line.barColor || '#22c55e'}; border-radius: 1px;"></span>
                </span>
                <span style="color: #374151; font-size: 9px;">{line.barPercent.toFixed(0)}%</span>
              {:else}
                <span style="color: #4b5563; font-size: 10px;">{line.text}</span>
              {/if}
            </div>
          {/each}
        {/if}
      {/each}

      <!-- Blinking cursor while running -->
      {#if !complete && steps.length > 0}
        <div style="margin-top: 8px; border-top: 1px solid #111118; padding-top: 6px;">
          <span style="color: #374151; font-size: 10px;">COST</span>
          <span style="color: #854d0e; font-size: 10px;"> ${totalCost.toFixed(3)}</span>
          <span style="color: #1a1a2e;"> │ </span>
          <span style="color: #374151; font-size: 10px;">TIME</span>
          <span style="color: #4b5563; font-size: 10px;"> {totalTime.toFixed(1)}s</span>
          <span class="animate-pulse" style="display: inline-block; width: 6px; height: 12px; background: #38bdf8; margin-left: 6px; vertical-align: middle;"></span>
        </div>
      {/if}

      <!-- Completion box -->
      {#if complete && summary}
        <div style="margin-top: 8px; border: 1px solid #14532d; background: #052e16; padding: 8px 12px;">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="color: #22c55e; font-weight: bold; font-size: 11px;">✓ EXTRACTION COMPLETE</span>
          </div>
          <div style="margin-top: 4px; color: #4b5563; font-size: 10px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px 12px;">
            <div>Items <span style="color: #d1d5db; font-weight: bold;">{summary.items}</span></div>
            <div>Accuracy <span style="color: {summary.accuracy >= 90 ? '#22c55e' : '#eab308'}; font-weight: bold;">{summary.accuracy.toFixed(1)}%</span></div>
            <div>Status <span style="color: {summary.accuracy >= 90 ? '#22c55e' : '#eab308'};">{summary.decision}</span></div>
            <div>Time <span style="color: #9ca3af;">{summary.duration.toFixed(1)}s</span></div>
            <div>Cost <span style="color: #eab308;">${summary.cost.toFixed(3)}</span></div>
            <div>Model <span style="color: #a78bfa;">{model}</span></div>
          </div>
          {#if summary.crossValidation}
            <div style="margin-top: 6px; border-top: 1px solid #14532d; padding-top: 6px;">
              <div style="color: #6b7280; font-size: 9px; font-weight: bold; margin-bottom: 3px;">INVOICE CROSS-CHECK</div>
              <div style="color: #4b5563; font-size: 10px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 1px 12px;">
                <div>Invoice Currency <span style="color: #d1d5db;">{summary.crossValidation.invoice_currency || '—'}</span></div>
                <div>Declaration Currency <span style="color: #d1d5db;">{summary.crossValidation.declaration_currency || '—'}</span></div>
                <div>Invoice Total <span style="color: #d1d5db;">{summary.crossValidation.invoice_total ? summary.crossValidation.invoice_total.toLocaleString() : '—'}</span></div>
                <div>Declaration Price <span style="color: #d1d5db;">{summary.crossValidation.declaration_invoice_price || '—'}</span></div>
              </div>
              {#if summary.crossValidation.conflicts?.length > 0}
                {#each summary.crossValidation.conflicts as conflict}
                  <div style="margin-top: 3px; color: #eab308; font-size: 9px;">
                    ⚠ {conflict.field}: Invoice={conflict.invoice_says} vs Declaration={conflict.declaration_says}
                  </div>
                {/each}
              {:else}
                <div style="margin-top: 3px; color: #22c55e; font-size: 9px;">✓ No conflicts detected</div>
              {/if}
            </div>
          {/if}
        </div>
      {/if}

      <!-- Scroll anchor -->
      <div style="height: 1px;"></div>
    </div>
  {/if}
</div>
