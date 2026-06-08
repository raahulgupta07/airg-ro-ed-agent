<script lang="ts">
  let {
    title = '',
    value = '',
    unit = '',
    subtitle = '',
    icon = '',
    progress = -1,
    accent = '',
  } = $props();

  const accentColor = $derived(accent || 'var(--primary)');
</script>

<div class="ink-border stamp-shadow p-5 transition-shadow hover:shadow-md"
     style="background: var(--surface-container-lowest);">
  <div class="flex justify-between items-center mb-3">
    <span class="text-xs font-medium uppercase tracking-wide" style="color: var(--on-surface-muted); letter-spacing: 0.06em;">{title}</span>
    {#if icon}
      <span class="material-symbols-outlined text-base" style="color: {accentColor};">{icon}</span>
    {/if}
  </div>

  <div class="font-serif text-4xl" style="color: var(--on-surface); letter-spacing: -0.02em; font-weight: 500;">
    {value}{#if unit}<span class="text-xl ml-1" style="color: var(--on-surface-muted);">{unit}</span>{/if}
  </div>

  {#if progress >= 0}
    <div class="mt-4 h-1.5 overflow-hidden" style="background: var(--surface-container); border-radius: 999px;">
      <div class="h-full transition-all duration-500"
           style="width: {Math.min(progress, 100)}%; background: {accentColor}; border-radius: 999px;"></div>
    </div>
  {/if}

  {#if subtitle}
    <div class="mt-2 text-xs" style="color: var(--on-surface-muted);">
      {subtitle}
    </div>
  {/if}
</div>
