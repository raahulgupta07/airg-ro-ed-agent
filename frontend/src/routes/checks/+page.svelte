<script lang="ts">
  /**
   * CHECKS — every field the engine is unsure about, across every document.
   *
   * The review page answers "which documents need attention". This answers
   * "which numbers do I actually have to look at", which is the work. Grouped by
   * document so a reviewer settles one form in a pass rather than bouncing
   * between PDFs, and each field disappears the moment it is settled — including
   * when the reviewer keeps the original value, because "I looked, it's right"
   * is a decision too.
   */
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import EvidenceCard from '$lib/components/EvidenceCard.svelte';

  type Check = {
    field: string; label: string; value: any; alternates: any[];
    source: string; model: string; status: string; reason: string;
    located: 'exact' | 'estimated' | 'unknown'; page?: number;
  };
  type Doc = {
    job_id: string; pdf_name: string; declaration_no?: string;
    importer_name?: string; created_at?: string; review_status?: string;
    checks: Check[];
  };

  let loading = $state(true);
  let error = $state('');
  let docs = $state<Doc[]>([]);
  let totalChecks = $state(0);
  let filterImporter = $state('');
  let open = $state<Set<string>>(new Set());
  let justDone = $state<string[]>([]);

  const importers = $derived(
    [...new Set(docs.map(d => d.importer_name).filter(Boolean))].sort() as string[]
  );
  const shown = $derived(
    filterImporter ? docs.filter(d => d.importer_name === filterImporter) : docs
  );

  async function load() {
    loading = true;
    error = '';
    try {
      const r = await api.evidenceQueue({ limit: 300 });
      docs = r.documents || [];
      totalChecks = r.total_checks || 0;
      // Open the first document — landing on an all-collapsed list hides the work.
      open = new Set(docs.slice(0, 1).map(d => d.job_id));
    } catch (e: any) {
      error = e?.message || 'Could not load the checks.';
    } finally {
      loading = false;
    }
  }

  function toggle(jobId: string) {
    const next = new Set(open);
    next.has(jobId) ? next.delete(jobId) : next.add(jobId);
    open = next;
  }

  function onResolved(jobId: string, field: string) {
    // Drop it locally rather than refetching — the reviewer keeps their place
    // in a long list instead of being bounced to the top after every decision.
    docs = docs
      .map(d => d.job_id === jobId
        ? { ...d, checks: d.checks.filter(c => c.field !== field) }
        : d)
      .filter(d => d.checks.length > 0);
    totalChecks = Math.max(0, totalChecks - 1);
    justDone = [...justDone, `${jobId}:${field}`];
  }

  function when(s?: string) {
    if (!s) return '';
    const d = new Date(s.replace(' ', 'T'));
    return isNaN(+d) ? s : d.toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  onMount(load);
</script>

<svelte:head><title>Checks · City Agent</title></svelte:head>

<div class="cl-ph">
  <h1>Checks</h1>
  <p>
    Values the reader was not sure about. Each one shows the part of the form it came
    from, so you can settle it without opening the document.
  </p>
</div>

<div class="bar">
  <div class="cl-stat">
    <span class="n">{totalChecks}</span>
    <span class="l">{totalChecks === 1 ? 'value to check' : 'values to check'}</span>
  </div>
  <div class="cl-stat">
    <span class="n">{shown.length}</span>
    <span class="l">{shown.length === 1 ? 'document' : 'documents'}</span>
  </div>
  <div class="spacer"></div>
  {#if importers.length > 1}
    <select class="cl-inp sm" bind:value={filterImporter} aria-label="Filter by importer">
      <option value="">All importers</option>
      {#each importers as imp}<option value={imp}>{imp}</option>{/each}
    </select>
  {/if}
  <button class="cl-btn sm" onclick={load} disabled={loading}>Refresh</button>
</div>

{#if loading}
  <p class="msg">Loading…</p>
{:else if error}
  <p class="msg err">{error}</p>
{:else if shown.length === 0}
  <div class="cl-panel done">
    <div class="cl-bd">
      <p class="done-h">Nothing to check.</p>
      <p class="done-p">
        {#if justDone.length}
          You settled {justDone.length} {justDone.length === 1 ? 'value' : 'values'}.
        {:else}
          Every extracted value either matched its second reading or was already confirmed.
        {/if}
      </p>
    </div>
  </div>
{:else}
  {#each shown as doc (doc.job_id)}
    <div class="cl-panel doc">
      <button class="cl-hd doc-hd" onclick={() => toggle(doc.job_id)}
              aria-expanded={open.has(doc.job_id)}>
        <span class="caret">{open.has(doc.job_id) ? '▾' : '▸'}</span>
        <span class="dno">{doc.declaration_no || doc.pdf_name}</span>
        <span class="imp">{doc.importer_name || ''}</span>
        <span class="when">{when(doc.created_at)}</span>
        <span class="pill warn">{doc.checks.length}</span>
      </button>

      {#if open.has(doc.job_id)}
        <div class="cl-bd">
          {#each doc.checks as check (check.field)}
            <EvidenceCard jobId={doc.job_id} {check}
                          onresolved={(f) => onResolved(doc.job_id, f)} />
          {/each}
          <a class="open" href={`/review?job=${doc.job_id}`}>Open the full document →</a>
        </div>
      {/if}
    </div>
  {/each}
{/if}

<style>
  .bar {
    display: flex; align-items: center; gap: 18px;
    margin: 0 0 16px; flex-wrap: wrap;
  }
  .bar .spacer { flex: 1 1 auto; }
  .bar .cl-inp { max-width: 220px; }

  .doc { margin-bottom: 10px; }
  .doc-hd {
    width: 100%; display: flex; align-items: center; gap: 10px;
    background: transparent; border: 0; cursor: pointer; text-align: left;
  }
  .caret { color: var(--on-surface-subtle); width: 10px; }
  .dno { font-family: var(--font-mono, monospace); }
  .imp {
    flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; text-transform: none; letter-spacing: 0;
    color: var(--on-surface-muted); font-size: 12px;
  }
  .when {
    color: var(--on-surface-subtle); font-size: 11px;
    text-transform: none; letter-spacing: 0;
  }

  .open {
    display: inline-block; margin-top: 4px; font-size: 12px;
    color: var(--primary); text-decoration: none;
  }
  .open:hover { text-decoration: underline; }

  .msg { font-size: 13px; color: var(--on-surface-muted); }
  .msg.err { color: var(--error); }
  .done-h { margin: 0 0 4px; font-size: 15px; color: var(--on-surface); }
  .done-p { margin: 0; font-size: 13px; color: var(--on-surface-muted); }
</style>
