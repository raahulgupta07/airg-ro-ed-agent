<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';

  let stats = $state<any>(null);
  let queue = $state<any[]>([]);
  let documents = $state<any[]>([]);
  let loading = $state(true);
  let error = $state('');
  let selected = $state<string | null>(null);
  let annotUrl = $state<string>('');
  let annotLoading = $state(false);
  let corrections = $state<Record<string, Record<string, string>>>({});
  let applying = $state<string>('');

  async function load() {
    loading = true; error = '';
    try {
      [stats, queue, documents] = await Promise.all([
        api.roverStats(), api.roverReview(), api.roverDocuments(),
      ]);
    } catch (e: any) { error = e.message || 'Failed to load'; }
    loading = false;
  }
  onMount(load);

  async function preview(docId: string) {
    selected = docId; annotUrl = ''; annotLoading = true;
    try { annotUrl = await api.roverAnnotateUrl(docId); }
    catch { annotUrl = ''; }
    annotLoading = false;
  }

  function setCorrection(docId: string, col: string, val: string) {
    corrections[docId] = { ...(corrections[docId] || {}), [col]: val };
  }

  async function applyDoc(item: any) {
    const c = corrections[item.doc_id] || {};
    const payload: Record<string, any> = {};
    for (const f of item.fields) {
      const v = c[f.column];
      payload[f.column] = (v !== undefined && v !== '') ? v : f.value; // confirm current if untouched
    }
    applying = item.doc_id;
    try {
      await api.roverApply(item.doc_id, payload);
      await load();
      if (selected === item.doc_id) selected = null;
    } catch (e: any) { error = e.message; }
    applying = '';
  }

  async function exportCsv() {
    try {
      await auth.ensureValidToken();
      const res = await fetch('/api/rover/export.csv', {
        headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
      });
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = 'rover_review.csv';
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e: any) { error = e.message; }
  }

  function fmt(v: any) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(String(v).replace(/,/g, ''));
    return isNaN(n) ? String(v) : n.toLocaleString();
  }
</script>

<svelte:head><title>City Agent ROVER · Review</title></svelte:head>

<div class="rover">
  <header class="rhead">
    <div>
      <div class="kick">City Agent ROVER</div>
      <h1>Release Order Review</h1>
      <p class="sub">Every number is read + math-verified or flagged here for you to confirm. Nothing uncertain ships silently.</p>
    </div>
    <button class="btn" onclick={load} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
  </header>

  {#if error}<div class="err">{error}</div>{/if}

  {#if stats}
    <div class="stats">
      <div class="stat"><div class="v">{stats.total ?? 0}</div><div class="k">Documents</div></div>
      <div class="stat ok"><div class="v">{stats.clean ?? 0}</div><div class="k">Clean · auto</div></div>
      <div class="stat warn"><div class="v">{stats.flagged ?? 0}</div><div class="k">Need review</div></div>
      <div class="stat"><div class="v">{stats.flagged_fields ?? 0}</div><div class="k">Flagged fields</div></div>
      <button class="btn ghost" onclick={exportCsv}>⬇ Export review CSV</button>
    </div>
  {/if}

  <div class="grid">
    <!-- Review queue -->
    <section>
      <h2>Review queue {#if queue.length}<span class="badge">{queue.length}</span>{/if}</h2>
      {#if !queue.length && !loading}
        <div class="empty">✓ Nothing to review — all documents are clean.</div>
      {/if}
      {#each queue as item (item.doc_id)}
        <div class="card" class:active={selected === item.doc_id}>
          <div class="card-h">
            <button class="link" onclick={() => preview(item.doc_id)}>{item.pdf || item.doc_id}</button>
            <span class="chip warn">{item.fields.length} to confirm</span>
          </div>
          {#each item.fields as f (f.column)}
            <div class="field">
              <div class="fcol">{f.column}</div>
              <div class="fval">
                current <b>{fmt(f.value)}</b>
                {#if f.alternates?.length}· alt {f.alternates.map(fmt).join(', ')}{/if}
              </div>
              <div class="fev">{f.evidence || f.reason}</div>
              <input class="fin" placeholder="confirm / correct"
                     value={corrections[item.doc_id]?.[f.column] ?? ''}
                     oninput={(e) => setCorrection(item.doc_id, f.column, (e.target as HTMLInputElement).value)} />
            </div>
          {/each}
          <div class="card-f">
            <button class="btn sm" onclick={() => applyDoc(item)} disabled={applying === item.doc_id}>
              {applying === item.doc_id ? 'Saving…' : 'Confirm all'}
            </button>
          </div>
        </div>
      {/each}
    </section>

    <!-- Preview + documents -->
    <section>
      <h2>Evidence preview</h2>
      {#if annotLoading}<div class="empty">Rendering…</div>
      {:else if annotUrl}<img class="annot" src={annotUrl} alt="annotated form" />
      {:else}<div class="empty">Select a document to see its fields boxed on the form.</div>{/if}

      <h2 style="margin-top:22px">All documents <span class="badge">{documents.length}</span></h2>
      <div class="tblwrap">
        <table>
          <thead><tr><th>Doc</th><th>Rate</th><th>Customs value</th><th>Status</th></tr></thead>
          <tbody>
            {#each documents as d (d.doc_id)}
              <tr class:rev={d.needs_review}>
                <td><button class="link" onclick={() => preview(d.doc_id)}>{d.doc_id}</button></td>
                <td class="num">{fmt(d.exchange_rate)}</td>
                <td class="num">{fmt(d.total_customs_value)}</td>
                <td>{d.needs_review ? '⚠ review' : '✓ clean'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  </div>
</div>

<style>
  .rover { max-width: 1200px; margin: 0 auto; padding: 24px 20px 70px; color: #e5e7eb;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .rhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
  .kick { font-family: ui-monospace, Menlo, monospace; font-size: 11px; letter-spacing: .15em;
    text-transform: uppercase; color: #2dd4bf; font-weight: 600; }
  h1 { font-size: 26px; margin: 4px 0 6px; }
  .sub { color: #9ca3af; margin: 0; max-width: 60ch; font-size: 14px; }
  h2 { font-size: 16px; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }
  .badge { background: #1f2937; color: #9ca3af; border-radius: 20px; padding: 1px 9px; font-size: 12px;
    font-family: ui-monospace, monospace; }
  .err { background: #3b1215; color: #fca5a5; border: 1px solid #7f1d1d; border-radius: 8px;
    padding: 10px 14px; margin: 14px 0; font-size: 14px; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin: 20px 0 24px; }
  .stat { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 14px 18px; min-width: 120px; }
  .stat .v { font-family: ui-monospace, monospace; font-size: 26px; font-weight: 700; }
  .stat .k { font-size: 12px; color: #9ca3af; margin-top: 2px; }
  .stat.ok .v { color: #34d399; } .stat.warn .v { color: #fbbf24; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: start; }
  @media (max-width: 880px) { .grid { grid-template-columns: 1fr; } }
  .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; }
  .card.active { border-color: #2dd4bf; }
  .card-h { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .card-f { margin-top: 10px; }
  .field { display: grid; grid-template-columns: 1fr; gap: 3px; padding: 8px 0; border-top: 1px solid #1f2937; }
  .fcol { font-family: ui-monospace, monospace; font-size: 12px; color: #93c5fd; }
  .fval { font-size: 13px; } .fval b { font-family: ui-monospace, monospace; }
  .fev { font-family: ui-monospace, monospace; font-size: 11px; color: #6b7280; }
  .fin { margin-top: 4px; background: #0b1220; border: 1px solid #374151; color: #e5e7eb;
    border-radius: 7px; padding: 6px 9px; font-size: 13px; font-family: ui-monospace, monospace; }
  .fin:focus { outline: none; border-color: #2dd4bf; }
  .btn { background: #2dd4bf; color: #06231f; border: none; border-radius: 9px; padding: 9px 16px;
    font-weight: 600; cursor: pointer; font-size: 14px; }
  .btn.sm { padding: 7px 14px; font-size: 13px; }
  .btn.ghost { background: transparent; color: #2dd4bf; border: 1px solid #234; margin-left: auto; }
  .btn:disabled { opacity: .5; cursor: default; }
  .link { background: none; border: none; color: #93c5fd; cursor: pointer; font: inherit; padding: 0;
    font-family: ui-monospace, monospace; font-size: 13px; }
  .chip { font-family: ui-monospace, monospace; font-size: 11px; border-radius: 20px; padding: 2px 9px; }
  .chip.warn { color: #fbbf24; background: #422006; }
  .empty { color: #6b7280; background: #0f1521; border: 1px dashed #1f2937; border-radius: 10px;
    padding: 24px; text-align: center; font-size: 14px; }
  .annot { width: 100%; border: 1px solid #1f2937; border-radius: 10px; background: #fff; }
  .tblwrap { overflow-x: auto; border: 1px solid #1f2937; border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #1f2937; }
  th { font-size: 11px; text-transform: uppercase; color: #9ca3af; font-family: ui-monospace, monospace; }
  td.num { font-family: ui-monospace, monospace; }
  tr.rev td { background: #1c1508; }
</style>
