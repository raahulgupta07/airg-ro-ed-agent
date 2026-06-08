<script lang="ts">
  let {
    icon = '',
    title = '',
    subtitle = '',
    question = '',
  } = $props();
</script>

<div class="mb-3" style="background: var(--surface-container); color: var(--on-surface);">
  <div class="px-4 py-3 flex items-center gap-3">
    {#if icon}
      <span class="material-symbols-outlined text-2xl" style="color: var(--warning);">{icon}</span>
    {/if}
    <div>
      <div class="font-medium uppercase text-sm">{title}</div>
      {#if subtitle}
        <div class="text-[10px] opacity-75">{subtitle}</div>
      {/if}
    </div>
  </div>
  {#if question}
    <div class="px-4 pb-3 font-mono text-xs" style="padding-left: 52px; color: var(--primary-container);">
      ? {question}
    </div>
  {/if}
</div>
