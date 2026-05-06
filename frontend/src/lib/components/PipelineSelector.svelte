<script lang="ts">
  import { PIPELINES, PIPELINE_ORDER, type PipelineKey } from '$lib/pipelineConfig';

  let { selected = $bindable('v11' as PipelineKey), disabled = false }: {
    selected?: PipelineKey;
    disabled?: boolean;
  } = $props();
</script>

<div class="space-y-2">
  <div class="text-xs uppercase tracking-wide text-zinc-400">Extraction Pipeline</div>
  <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
    {#each PIPELINE_ORDER as key (key)}
      {@const pipe = PIPELINES[key]}
      <button
        type="button"
        {disabled}
        onclick={() => (selected = key)}
        class="text-left p-3 rounded border-2 transition-all
               {selected === key
                  ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-950/40'
                  : 'border-zinc-200 dark:border-zinc-700 hover:border-zinc-400'}
               {disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}"
      >
        <div class="flex items-center justify-between mb-1">
          <span class="font-bold text-sm">{pipe.name}</span>
          {#if pipe.badge}
            <span class="text-[10px] px-2 py-0.5 rounded-full
                         {pipe.badge === 'Recommended'
                            ? 'bg-emerald-500 text-white'
                            : 'bg-amber-500 text-white'}">
              {pipe.badge}
            </span>
          {/if}
        </div>
        <div class="text-xs text-zinc-600 dark:text-zinc-400 mb-2 leading-snug">
          {pipe.description}
        </div>
        <div class="flex gap-3 text-[11px] font-mono text-zinc-500">
          <span>💰 {pipe.estCost}</span>
          <span>⏱ {pipe.estTime}</span>
        </div>
      </button>
    {/each}
  </div>
</div>
