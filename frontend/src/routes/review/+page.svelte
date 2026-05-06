<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import Button from '$lib/components/Button.svelte';
  import { getAccuracyColor } from '$lib/colors';

  let loading = $state(true);
  let jobs = $state<any[]>([]);
  let filter = $state<'all' | 'review' | 'approved' | 'escalated'>('review');

  const filtered = $derived(() => {
    if (filter === 'all') return jobs;
    if (filter === 'review') return jobs.filter(j => j.review_status === 'quick_review');
    if (filter === 'approved') return jobs.filter(j => j.review_status === 'auto_approved');
    if (filter === 'escalated') return jobs.filter(j => j.review_status === 'full_review');
    return jobs;
  });

  const counts = $derived({
    total: jobs.length,
    approved: jobs.filter(j => j.review_status === 'auto_approved').length,
    review: jobs.filter(j => j.review_status === 'quick_review').length,
    escalated: jobs.filter(j => j.review_status === 'full_review').length,
  });

  function getStatusBadge(status: string): { text: string; variant: 'success' | 'warning' | 'critical' } {
    if (status === 'auto_approved') return { text: 'AUTO-APPROVED', variant: 'success' };
    if (status === 'quick_review') return { text: 'NEEDS REVIEW', variant: 'warning' };
    return { text: 'ESCALATED', variant: 'critical' };
  }

  async function approveJob(jobId: string) {
    // Mark as reviewed
    const idx = jobs.findIndex(j => j.job_id === jobId);
    if (idx >= 0) {
      jobs[idx].review_status = 'auto_approved';
      jobs = [...jobs];
    }
  }

  async function loadJobs() {
    loading = true;
    try {
      const allJobs = await api.listJobs(200);
      jobs = allJobs.map((j: any) => {
        const acc = j.accuracy_percent || 0;
        let review_status = 'auto_approved';
        if (acc < 80) review_status = 'full_review';
        else if (acc < 95) review_status = 'quick_review';
        return { ...j, review_status };
      });
    } catch {}
    loading = false;
  }

  onMount(loadJobs);
</script>

<div class="w-full max-w-6xl mx-auto mb-4 p-4 rounded border-2 border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30">
  <div class="flex items-start gap-3 flex-wrap">
    <span class="text-2xl">⚠️</span>
    <div class="flex-1 min-w-[200px]">
      <div class="font-bold text-base">Review Queue</div>
      <div class="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
        Docs flagged for human verification (handwriting, low-confidence reads, currency-band failures). Approve or correct each.
      </div>
    </div>
  </div>
</div>

<ChapterHeading icon="checklist" title="REVIEW_QUEUE" subtitle="Confidence-based job routing — approve, review, or escalate" />

{#if loading}
  <div class="flex items-center justify-center p-12">
    <div class="agent-spinner" style="border-color: var(--secondary); border-top-color: transparent;"></div>
    <span class="ml-3 text-sm font-bold uppercase">LOADING...</span>
  </div>
{:else}
  <!-- KPIs -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
    <KpiCard title="TOTAL JOBS" value="{counts.total}" icon="folder" accent="#006f7c" />
    <KpiCard title="AUTO-APPROVED" value="{counts.approved}" icon="check_circle" accent="#007518" subtitle="{counts.total > 0 ? ((counts.approved/counts.total*100).toFixed(0) + '%') : '0%'}" />
    <KpiCard title="NEEDS REVIEW" value="{counts.review}" icon="rate_review" accent="#ff9d00" />
    <KpiCard title="ESCALATED" value="{counts.escalated}" icon="error" accent="#be2d06" />
  </div>

  <!-- Filter tabs -->
  <div class="flex gap-0 mb-4 border-2" style="border-color: var(--on-surface); width: fit-content;">
    {#each [['all', 'ALL'], ['review', 'NEEDS REVIEW'], ['escalated', 'ESCALATED'], ['approved', 'APPROVED']] as [key, label]}
      <button class="px-4 py-1.5 text-[10px] font-bold uppercase cursor-pointer"
        style="{filter === key ? 'background: var(--on-surface); color: var(--surface);' : 'background: white; color: var(--on-surface);'}"
        onclick={() => filter = key as any}>
        {label} ({key === 'all' ? counts.total : key === 'review' ? counts.review : key === 'escalated' ? counts.escalated : counts.approved})
      </button>
    {/each}
  </div>

  <!-- Job list -->
  <div class="border-2" style="border-color: var(--on-surface);">
    <div class="dark-bar text-xs flex justify-between items-center">
      <span>JOBS — {filter.toUpperCase()}</span>
      <span class="text-[10px]" style="color: var(--primary-container);">{filtered().length} jobs</span>
    </div>
    <div class="overflow-auto bg-white" style="max-height: 600px;">
      {#each filtered() as job}
        <div class="flex items-center gap-3 px-4 py-2.5 border-b" style="border-color: rgba(56,56,50,0.08);">
          <!-- Status indicator -->
          <div class="w-2 h-2" style="background: {job.review_status === 'auto_approved' ? '#22c55e' : job.review_status === 'quick_review' ? '#ff9d00' : '#ef4444'};"></div>

          <!-- PDF info -->
          <div class="flex-1 min-w-0">
            <div class="text-xs font-bold truncate">{job.pdf_name}</div>
            <div class="text-[9px]" style="color: var(--outline);">{job.username || '—'} · {(job.created_at || '').split(' ')[0]}</div>
          </div>

          <!-- Accuracy -->
          <div class="text-right">
            <div class="text-xs font-bold font-mono" style="color: {getAccuracyColor(job.accuracy_percent || 0)};">
              {(job.accuracy_percent || 0).toFixed(1)}%
            </div>
          </div>

          <!-- Items -->
          <div class="text-[10px] font-bold w-12 text-right" style="color: var(--on-surface);">
            {job.items?.length || '—'}
          </div>

          <!-- Badge -->
          <Badge text={getStatusBadge(job.review_status).text} variant={getStatusBadge(job.review_status).variant} />

          <!-- Actions -->
          {#if job.review_status === 'quick_review'}
            <Button variant="primary" size="sm" onclick={() => approveJob(job.job_id)}>
              <span class="flex items-center gap-1 text-[9px]">
                <span class="material-symbols-outlined text-xs">check</span> APPROVE
              </span>
            </Button>
          {/if}

          <a href="/history?job={job.job_id}" class="text-[9px] font-bold uppercase no-underline" style="color: var(--secondary);">VIEW</a>
        </div>
      {/each}
      {#if filtered().length === 0}
        <div class="p-8 text-center text-xs uppercase" style="color: var(--outline);">No jobs in this category</div>
      {/if}
    </div>
  </div>
{/if}
