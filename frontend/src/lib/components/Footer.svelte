<script lang="ts">
  let {
    pipelineStatus = 'IDLE',
    model = 'GEMINI-3.1-FLASH-LITE',
    totalJobs = 0,
    totalCost = 0,
  } = $props();

  const statusLabel = $derived(pipelineStatus.toString().toLowerCase());
  const modelLabel  = $derived(model.toString().toLowerCase());
</script>

<footer class="fixed bottom-0 left-[236px] right-0 h-10 z-40 flex items-center px-6 gap-5 text-xs"
        style="background: rgba(245,244,238,0.85); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-top: 1px solid var(--line); color: var(--on-surface-muted);">

  <!-- System ok -->
  <div class="flex items-center gap-1.5">
    <span class="inline-block w-1.5 h-1.5 rounded-full" style="background: var(--success);"></span>
    <span class="font-medium" style="color: var(--on-surface);">System active</span>
  </div>

  <span style="color: var(--outline-variant);">·</span>

  <!-- Pipeline -->
  <div>Pipeline · <span style="color: var(--on-surface);">{statusLabel}</span></div>

  <span style="color: var(--outline-variant);">·</span>

  <!-- Model -->
  <div>Model · <span class="font-mono" style="color: var(--on-surface);">{modelLabel}</span></div>

  <span style="color: var(--outline-variant);">·</span>

  <!-- DB -->
  <div>DB · <span style="color: var(--on-surface);">wal_active</span></div>

  <span style="color: var(--outline-variant);">·</span>

  <!-- Jobs -->
  <div>Jobs · <span style="color: var(--on-surface);">{totalJobs}</span></div>

  <span style="color: var(--outline-variant);">·</span>

  <!-- Cost -->
  <div>Cost · <span class="font-mono" style="color: var(--primary);">${totalCost.toFixed(3)}</span></div>

  <!-- Spacer -->
  <div class="flex-1"></div>

  <!-- Live feed -->
  <div class="flex items-center gap-1.5">
    <span class="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style="background: var(--tertiary);"></span>
    <span style="color: var(--on-surface-muted);">Live feed</span>
  </div>
</footer>
