<script lang="ts">
  let {
    filename = '',
    size = 0,
    pages = 0,
    items = 0,
    accuracy = 0,
    status = 'queued' as 'queued' | 'processing' | 'done' | 'error' | 'stopped' | 'duplicate',
    progress = 0,
    stepLabel = '',
    selected = false,
    onclick = undefined as (() => void) | undefined,
  } = $props();

  const sizeMB = $derived((size / 1024 / 1024).toFixed(1));

  const statusConfig: Record<string, { color: string; label: string; icon: string }> = {
    queued: { color: 'var(--on-surface-muted)', label: 'QUEUED', icon: 'schedule' },
    processing: { color: 'var(--info)', label: 'PROCESSING', icon: '' },
    done: { color: 'var(--success)', label: 'DONE', icon: 'check_circle' },
    error: { color: 'var(--error)', label: 'ERROR', icon: 'error' },
    stopped: { color: 'var(--warning)', label: 'STOPPED', icon: 'stop_circle' },
    duplicate: { color: 'var(--warning)', label: 'DUPLICATE', icon: 'content_copy' },
  };

  const cfg = $derived(statusConfig[status] || statusConfig.queued);
</script>

<button
  class="w-full text-left p-3 border-b transition-colors cursor-pointer"
  style="
    border-color: rgba(56,56,50,0.15);
    background: {selected ? 'var(--surface-container)' : 'white'};
    {selected ? 'border-left: 1px solid var(--secondary);' : 'border-left: 1px solid transparent;'}
  "
  onclick={onclick}
>
  <div class="flex items-center gap-2">
    <span class="material-symbols-outlined text-sm" style="color: var(--secondary);">picture_as_pdf</span>
    <div class="flex-1 min-w-0">
      <div class="text-xs font-bold truncate" style="color: var(--on-surface);">{filename}</div>
      <div class="text-[9px] uppercase font-mono" style="color: var(--outline);">
        {sizeMB} MB
        {#if pages > 0} · {pages} pages{/if}
        {#if status === 'done' && items > 0} · {items} items · {accuracy.toFixed(1)}%{/if}
      </div>
    </div>
    <div class="flex items-center gap-1">
      {#if status === 'processing'}
        <div class="agent-spinner" style="border-color: {cfg.color}; border-top-color: transparent; width: 12px; height: 12px;"></div>
      {:else if cfg.icon}
        <span class="material-symbols-outlined text-sm" style="color: {cfg.color};">{cfg.icon}</span>
      {/if}
      <span class="text-[8px] font-medium uppercase" style="color: {cfg.color};">{cfg.label}</span>
    </div>
  </div>

  {#if status === 'processing' && progress > 0}
    <div class="mt-1.5 h-1 border" style="border-color: var(--on-surface); background: var(--surface-container-highest);">
      <div class="h-full transition-all" style="width: {Math.min(progress, 100)}%; background: {cfg.color};"></div>
    </div>
    {#if stepLabel}
      <div class="mt-0.5 text-[8px] font-mono uppercase" style="color: {cfg.color};">{stepLabel}</div>
    {/if}
  {/if}
</button>
