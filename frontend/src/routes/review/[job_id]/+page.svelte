<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { api } from '$lib/api';
  import ReviewSplitView from '$lib/components/ReviewSplitView.svelte';
  import Button from '$lib/components/Button.svelte';

  let loading = $state(true);
  let errMsg = $state('');
  let job = $state<any>(null);

  const jobId = $derived($page.params.job_id);

  async function load() {
    loading = true;
    errMsg = '';
    try {
      const res = await api.reviewDetail(jobId);
      // Adapt {job, declarations, items, edits, flags} to ReviewSplitView shape
      job = {
        ...(res.job || {}),
        job_id: jobId,
        declaration: (res.declarations && res.declarations[0]) || {},
        items: res.items || [],
        edits: res.edits || [],
        item_review_flags: (res.flags && res.flags.cross_validation && res.flags.cross_validation.item_review_flags) || [],
      };
    } catch (e: any) {
      errMsg = e?.message || 'Failed to load job';
    }
    loading = false;
  }

  function back() { goto('/review'); }

  onMount(load);
</script>

<div class="flex items-center gap-3 mb-3">
  <Button size="sm" variant="ghost" onclick={back}>← BACK_TO_QUEUE</Button>
  <span class="text-xs font-mono opacity-70">JOB_ID: <strong>{jobId}</strong></span>
</div>

{#if loading}
  <div class="flex items-center justify-center p-12">
    <div class="agent-spinner" style="border-color: var(--secondary); border-top-color: transparent;"></div>
    <span class="ml-3 text-sm font-bold uppercase">LOADING_JOB...</span>
  </div>
{:else if errMsg}
  <div class="p-4 border-2" style="border-color: var(--error); background: #fff;">
    <div class="text-xs font-bold uppercase" style="color: var(--error);">ERROR</div>
    <div class="text-xs mt-1">{errMsg}</div>
  </div>
{:else if job}
  <ReviewSplitView
    jobId={jobId}
    job={job}
    onApprove={back}
    onReject={back}
    onClose={back}
  />
{/if}
