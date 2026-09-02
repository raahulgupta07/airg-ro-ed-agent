<script lang="ts">
  import { onMount } from 'svelte';

  let {
    pipelineStatus = 'IDLE',
    model = '',
    totalJobs = 0,
    totalCost = 0,
  } = $props();

  const statusLabel = $derived(pipelineStatus.toString().toLowerCase());
  const modelLabel  = $derived(
    (model?.toString() || healthModel || '—').toLowerCase());

  // App version/patch — fetched from the running backend so the UI always
  // reflects what is actually deployed (handy for verifying an AWS update).
  // /api/health reports the model the DEFAULT engine actually uses.
  let healthModel = $state('');
  let appVersion = $state('');
  let appEngine = $state('');
  let changelog = $state<string[]>([]);
  onMount(async () => {
    try {
      const r = await fetch('/api/health');
      const j = JSON.parse(await r.text());
      appVersion = j.version || '';
      appEngine = j.engine || '';
      healthModel = j.model || '';
      changelog = Array.isArray(j.changelog) ? j.changelog : [];
    } catch {}
  });
</script>

<footer class="fixed bottom-0 left-0 right-0 h-10 z-40 flex items-center px-3 md:px-6 gap-3 md:gap-5 text-xs overflow-x-auto whitespace-nowrap"
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

  <!-- Version / patch (what's actually deployed) -->
  {#if appVersion}
    <div class="font-mono" title={changelog.join('\n')} style="color: var(--on-surface);">
      {appEngine} · v{appVersion}
    </div>
    <span style="color: var(--outline-variant);">·</span>
  {/if}

  <!-- Live feed -->
  <div class="flex items-center gap-1.5">
    <span class="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style="background: var(--tertiary);"></span>
    <span style="color: var(--on-surface-muted);">Live feed</span>
  </div>
</footer>
