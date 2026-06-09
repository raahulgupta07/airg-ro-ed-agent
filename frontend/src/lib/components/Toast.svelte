<script lang="ts">
  let {
    message = '',
    type = 'success' as 'success' | 'error',
    visible = false,
  } = $props();

  const bg = $derived(type === 'success' ? 'var(--primary)' : 'var(--error)');
</script>

{#if visible}
  <div class="fixed top-20 right-6 z-50 animate-slide-in border-2 stamp-shadow px-4 py-3 text-sm font-bold uppercase tracking-wider text-white"
       style="background: {bg}; border-color: var(--line);">
    {message}
  </div>
{/if}
