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
  let detail = $state<any>(null);
  let detailLoading = $state(false);
  let annotUrl = $state('');
  let showForm = $state(false);

  let corrections = $state<Record<string, Record<string, string>>>({});
  let applying = $state('');
  let processing = $state<string[]>([]);
  let fileInput: HTMLInputElement;
  let dragging = $state(false);

  const LBL: Record<string, string> = {
    declaration_no: 'Declaration No (First-approval)', declaration_no_official: 'Declaration No (Official)',
    declaration_date: 'Date', importer_name: 'Importer', invoice_number: 'Invoice Number',
    currency: 'Currency', exchange_rate: 'Exchange Rate', invoice_price_mmk: 'Invoice Price (MMK)',
    total_customs_value: 'Total Customs Value', import_export_customs_duty: 'Import Duty (CD)',
    commercial_tax_ct: 'Commercial Tax (CT)', advance_income_tax_at: 'Income Tax (AT)',
    security_fee_sf: 'Security (SF)', maccs_service_fee_mf: 'MACCS Fee (MF)', exemption_reduction: 'Exemption/Reduction',
  };

  async function load() {
    loading = true; error = '';
    try {
      [stats, queue, documents] = await Promise.all([api.roverStats(), api.roverReview(), api.roverDocuments()]);
    } catch (e: any) { error = e.message || 'Failed to load'; }
    loading = false;
  }
  onMount(load);

  async function openDoc(docId: string) {
    selected = docId; detail = null; annotUrl = ''; showForm = false; detailLoading = true;
    try { detail = await api.roverDocument(docId); } catch (e: any) { error = e.message; }
    detailLoading = false;
  }
  async function loadForm() {
    if (annotUrl || !selected) { showForm = !showForm; return; }
    try { annotUrl = await api.roverAnnotateUrl(selected); showForm = true; }
    catch { error = 'Annotated preview not available for this document.'; }
  }
  async function onUpload(files: FileList | null) {
    dragging = false;
    if (!files || !files.length) return;
    error = '';
    const names = Array.from(files).map((f) => f.name);
    const before = documents.length;
    processing = [...processing, ...names];
    try { for (const f of Array.from(files)) await api.roverUpload(f); }
    catch (e: any) { error = e.message || 'Upload failed'; }
    const target = before + names.length;
    for (let i = 0; i < 80; i++) { await new Promise((r) => setTimeout(r, 4000)); await load(); if (documents.length >= target) break; }
    processing = processing.filter((n) => !names.includes(n));
  }
  function setCorrection(d: string, c: string, v: string) { corrections[d] = { ...(corrections[d] || {}), [c]: v }; }
  async function applyDoc(item: any) {
    const c = corrections[item.doc_id] || {};
    const payload: Record<string, any> = {};
    for (const f of item.fields) payload[f.column] = (c[f.column] ?? '') !== '' ? c[f.column] : f.value;
    applying = item.doc_id;
    try { await api.roverApply(item.doc_id, payload); await load(); if (selected === item.doc_id) openDoc(item.doc_id); }
    catch (e: any) { error = e.message; }
    applying = '';
  }
  async function authedDownload(url: string, name: string) {
    try {
      await auth.ensureValidToken();
      const res = await fetch(url, { headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {} });
      if (!res.ok) throw new Error('download failed');
      download(name, await res.blob());
    } catch (e: any) { error = e.message; }
  }
  const exportCsv = () => authedDownload('/api/rover/export.csv', 'rover_review.csv');
  const exportXlsx = () => authedDownload('/api/rover/export.xlsx', 'rover_report.xlsx');
  const exportDocXlsx = () => selected && authedDownload(`/api/rover/documents/${encodeURIComponent(selected)}/export.xlsx`, `${selected}.xlsx`);
  function download(name: string, data: Blob | string, type = 'application/json') {
    const blob = data instanceof Blob ? data : new Blob([data], { type });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
  }
  function downloadJson() { if (detail) download(`${selected}.json`, JSON.stringify(detail, null, 2)); }
  function downloadItemsCsv() {
    if (!detail?.items?.length) return;
    const cols = ['no', 'hs_code', 'description', 'quantity', 'unit', 'value'];
    const esc = (s: any) => { s = s == null ? '' : String(s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    let csv = cols.join(',') + '\n';
    for (const it of detail.items) csv += cols.map((c) => esc(it[c])).join(',') + '\n';
    download(`${selected}_products.csv`, csv, 'text/csv');
  }
  function fmt(v: any) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(String(v).replace(/,/g, ''));
    return isNaN(n) ? String(v) : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
</script>

<svelte:head><title>City Agent ROVER · Review</title></svelte:head>

<div class="rv">
  <header class="hd">
    <div>
      <div class="kick">City Agent ROVER</div>
      <h1>Release Order Review</h1>
      <p class="sub">Every number is read and math-verified, or flagged here for you to confirm. Nothing uncertain ships silently.</p>
    </div>
    <button class="btn ghost" onclick={load} disabled={loading} aria-label="Refresh">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if error}
    <div class="alert" role="alert">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      {error}
    </div>
  {/if}

  <button class="drop" class:drag={dragging} type="button"
          onclick={() => fileInput.click()}
          ondragover={(e) => { e.preventDefault(); dragging = true; }}
          ondragleave={() => dragging = false}
          ondrop={(e) => { e.preventDefault(); onUpload(e.dataTransfer?.files ?? null); }}>
    <input bind:this={fileInput} type="file" accept="application/pdf" multiple hidden onchange={(e) => onUpload((e.target as HTMLInputElement).files)} />
    <svg class="di" viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
    <div class="dt">Drop Release-Order PDFs — or click to browse</div>
    <div class="ds">ROVER reads every field, math-verifies, and flags anything uncertain.</div>
  </button>

  {#if processing.length}
    <div class="proc" role="status">
      <span class="spin" aria-hidden="true"></span>
      <span>Extracting {processing.length} document{processing.length > 1 ? 's' : ''} — {processing.join(', ')}</span>
      <span class="proc-s">reading + math-verifying (30–160s each); this list updates automatically.</span>
    </div>
  {/if}

  {#if stats}
    <div class="kpis">
      <div class="kpi"><div class="kv">{stats.total ?? 0}</div><div class="kk">Documents</div></div>
      <div class="kpi ok"><div class="kv">{stats.clean ?? 0}</div><div class="kk">Clean · auto</div></div>
      <div class="kpi warn"><div class="kv">{stats.flagged ?? 0}</div><div class="kk">Need review</div></div>
      <div class="kpi"><div class="kv">{stats.flagged_fields ?? 0}</div><div class="kk">Flagged fields</div></div>
      <div class="export">
        <button class="btn" onclick={exportXlsx} title="Download all documents as an Excel workbook">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>
          Export Excel
        </button>
        <button class="btn ghost" onclick={exportCsv} title="Review-queue CSV">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          CSV
        </button>
      </div>
    </div>
  {/if}

  <div class="grid">
    <section>
      <h2 class="sec">Documents <span class="badge">{documents.length}</span></h2>
      <div class="tw">
        <table>
          <thead><tr><th>Document</th><th class="n">Rate</th><th class="n">Customs value</th><th>Status</th></tr></thead>
          <tbody>
            {#each documents as d (d.doc_id)}
              <tr class:sel={selected === d.doc_id} tabindex="0" onclick={() => openDoc(d.doc_id)} onkeydown={(e) => e.key === 'Enter' && openDoc(d.doc_id)}>
                <td class="docid">{d.document_name || d.doc_id}</td>
                <td class="n">{fmt(d.exchange_rate)}</td>
                <td class="n">{fmt(d.total_customs_value)}</td>
                <td>{#if d.needs_review}<span class="chip warn">review</span>{:else}<span class="chip ok">clean</span>{/if}</td>
              </tr>
            {/each}
            {#if !documents.length && !loading}<tr><td colspan="4" class="empty">No documents yet — drop a PDF above to begin.</td></tr>{/if}
          </tbody>
        </table>
      </div>

      {#if queue.length}
        <h2 class="sec" style="margin-top:24px">Needs confirmation <span class="badge warn">{queue.length}</span></h2>
        {#each queue as item (item.doc_id)}
          <div class="card">
            <div class="ch"><b>{item.pdf || item.doc_id}</b><span class="chip warn">{item.fields.length} to confirm</span></div>
            {#each item.fields as f (f.column)}
              <div class="fld">
                <div class="fc">{LBL[f.column] || f.column}</div>
                <div class="fv">current <b>{fmt(f.value)}</b>{#if f.alternates?.length} · alt {f.alternates.map(fmt).join(', ')}{/if}</div>
                <div class="fe">{f.evidence || f.reason}</div>
                <input class="fi" placeholder="confirm or correct" value={corrections[item.doc_id]?.[f.column] ?? ''}
                       oninput={(e) => setCorrection(item.doc_id, f.column, (e.target as HTMLInputElement).value)} />
              </div>
            {/each}
            <button class="btn" onclick={() => applyDoc(item)} disabled={applying === item.doc_id}>
              {applying === item.doc_id ? 'Saving…' : 'Confirm all'}
            </button>
          </div>
        {/each}
      {/if}
    </section>

    <section>
      <h2 class="sec">Extracted data {#if selected}<span class="badge">{selected}</span>{/if}</h2>
      {#if !selected}
        <div class="card empty">Select a document to see every extracted field, its value, and where it was read from.</div>
      {:else if detailLoading}
        <div class="card empty">Loading…</div>
      {:else if detail}
        <div class="card">
          <div class="acts">
            <button class="btn sm" onclick={exportDocXlsx} title="Download this document as Excel">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>
              Excel
            </button>
            <button class="btn ghost sm" onclick={loadForm}>
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/></svg>
              {showForm ? 'Hide form' : 'Show boxed form'}
            </button>
            <button class="btn ghost sm" onclick={downloadJson}>
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Raw JSON
            </button>
            {#if detail.items?.length}
              <button class="btn ghost sm" onclick={downloadItemsCsv}>
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Products CSV
              </button>
            {/if}
          </div>
          {#if showForm && annotUrl}<img class="annot" src={annotUrl} alt="Extracted form with each field boxed" />{/if}
          <div class="tw">
            <table>
              <thead><tr><th>Field</th><th>Value</th><th>Read by</th><th class="n">Conf.</th><th>Evidence</th></tr></thead>
              <tbody>
                {#each Object.entries(detail.record || {}) as [col, cell] (col)}
                  {#if (cell as any).value !== null && (cell as any).value !== undefined && (cell as any).value !== ''}
                    <tr>
                      <td>{LBL[col] || col}</td>
                      <td class="vmono">{fmt((cell as any).value)}</td>
                      <td><span class="tag">{String((cell as any).model || '').split('/').pop()}</span></td>
                      <td class="n conf">{(cell as any).confidence ?? ''}</td>
                      <td class="src">{(cell as any).source || ''}</td>
                    </tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          </div>
          {#if detail.items?.length}
            <h3>Products <span class="badge">{detail.items.length}</span></h3>
            <div class="tw">
              <table>
                <thead><tr><th>#</th><th>HS code</th><th>Description</th><th class="n">Qty</th><th>Unit</th><th class="n">Value</th></tr></thead>
                <tbody>
                  {#each detail.items as it (it.no ?? it.hs_code)}
                    <tr><td>{it.no ?? ''}</td><td class="vmono">{it.hs_code ?? ''}</td><td>{it.description ?? ''}</td>
                      <td class="n">{fmt(it.quantity)}</td><td>{it.unit ?? ''}</td><td class="n">{fmt(it.value)}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>
      {/if}
    </section>
  </div>
</div>

<style>
  .rv{max-width:1280px;margin:0 auto;padding:26px 24px 72px;color:var(--on-surface);font-family:'Inter',-apple-system,sans-serif}
  .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
  .kick{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--primary);font-weight:600}
  h1{font-family:'Source Serif 4',Georgia,serif;font-size:30px;margin:4px 0 6px;letter-spacing:-.01em}
  .sub{color:var(--on-surface-muted);margin:0;max-width:64ch;font-size:14px;line-height:1.6}
  h2.sec{font-family:'Source Serif 4',Georgia,serif;font-size:18px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
  h3{font-family:'Source Serif 4',Georgia,serif;font-size:15px;margin:18px 0 8px;display:flex;gap:8px;align-items:center}
  .badge{background:var(--surface-container-high);color:var(--on-surface-muted);border-radius:20px;padding:1px 9px;font-size:12px;font-family:'JetBrains Mono',monospace}
  .badge.warn{background:var(--warning-soft);color:var(--warning)}
  /* buttons */
  .btn{display:inline-flex;align-items:center;gap:7px;background:var(--primary);color:var(--on-primary);border:none;
    border-radius:var(--radius-md);padding:9px 15px;font-weight:600;cursor:pointer;font-size:13.5px;
    transition:background .18s ease,box-shadow .18s ease}
  .btn:hover{background:var(--primary-hover)} .btn:disabled{opacity:.55;cursor:default}
  .btn:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
  .btn.ghost{background:transparent;color:var(--primary);border:1px solid var(--outline)}
  .btn.ghost:hover{background:var(--primary-container)}
  .btn.sm{padding:6px 11px;font-size:12.5px}
  .export{display:flex;gap:8px;margin-left:auto}
  /* alert */
  .alert{display:flex;align-items:center;gap:9px;background:var(--error-soft);color:var(--error);
    border:1px solid var(--error-container);border-radius:var(--radius-md);padding:10px 14px;margin:14px 0;font-size:14px}
  /* drop zone */
  .drop{width:100%;text-align:center;margin:18px 0 6px;border:2px dashed var(--outline);border-radius:var(--radius-xl);
    padding:28px;cursor:pointer;background:var(--surface-container-lowest);color:var(--on-surface);
    transition:border-color .18s ease,background .18s ease;display:flex;flex-direction:column;align-items:center;gap:4px}
  .drop:hover,.drop:focus-visible,.drop.drag{border-color:var(--primary);background:var(--primary-container);outline:none}
  .di{color:var(--primary);margin-bottom:4px} .dt{font-size:15px;font-weight:600} .ds{font-size:13px;color:var(--on-surface-muted)}
  /* processing */
  .proc{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:8px 0 4px;background:var(--primary-container);
    border:1px solid var(--primary);border-radius:var(--radius-lg);padding:12px 16px;font-size:14px}
  .proc-s{color:var(--on-surface-muted);font-size:12px;width:100%}
  .spin{width:14px;height:14px;border:2px solid var(--outline-variant);border-top-color:var(--primary);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  /* kpis — data-dense */
  .kpis{display:flex;gap:12px;flex-wrap:wrap;align-items:stretch;margin:20px 0 24px}
  .kpi{background:var(--surface-container-lowest);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    padding:13px 18px;min-width:112px;box-shadow:var(--shadow-xs)}
  .kv{font-family:'JetBrains Mono',monospace;font-size:25px;font-weight:700;line-height:1.1}
  .kk{font-size:11.5px;color:var(--on-surface-muted);margin-top:3px;text-transform:uppercase;letter-spacing:.03em;font-family:'JetBrains Mono',monospace}
  .kpi.ok .kv{color:var(--success)} .kpi.warn .kv{color:var(--warning)}
  /* grid */
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:22px;align-items:start}
  @media (max-width:980px){.grid{grid-template-columns:1fr}}
  .card{background:var(--surface-container-lowest);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow-xs)}
  .ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .acts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
  .fld{display:flex;flex-direction:column;gap:3px;padding:9px 0;border-top:1px solid var(--outline-variant)}
  .fc{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--primary)}
  .fv{font-size:13px}.fv b{font-family:'JetBrains Mono',monospace}
  .fe{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--on-surface-subtle)}
  .fi{margin-top:4px;background:var(--surface);border:1px solid var(--outline);color:var(--on-surface);
    border-radius:var(--radius-sm);padding:7px 10px;font-size:13px;font-family:'JetBrains Mono',monospace;transition:border-color .18s ease}
  .fi:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-container)}
  .chip{font-family:'JetBrains Mono',monospace;font-size:11px;border-radius:20px;padding:2px 9px;font-weight:500}
  .chip.warn{color:var(--warning);background:var(--warning-soft)} .chip.ok{color:var(--success);background:var(--success-soft)}
  .empty{color:var(--on-surface-muted);text-align:center;font-size:14px;padding:24px}
  .annot{width:100%;border:1px solid var(--outline-variant);border-radius:var(--radius-md);background:#fff;margin:2px 0 10px}
  /* tables */
  .tw{overflow-x:auto;border:1px solid var(--outline-variant);border-radius:var(--radius-md);background:var(--surface-container-lowest)}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--outline-variant);vertical-align:top}
  th{font-family:'JetBrains Mono',monospace;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--on-surface-muted);
    background:var(--surface-container-low);position:sticky;top:0}
  th.n,td.n{text-align:right;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  tbody tr{cursor:pointer;transition:background .12s ease}
  tbody tr:hover{background:var(--surface-container-low)}
  tbody tr:focus-visible{outline:2px solid var(--primary);outline-offset:-2px}
  tr.sel{background:var(--primary-container)}
  td.docid{font-family:'JetBrains Mono',monospace;color:var(--primary);font-weight:500}
  td.vmono{font-family:'JetBrains Mono',monospace;font-weight:600;white-space:nowrap}
  td.conf{color:var(--success)} td.src{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--on-surface-subtle)}
  .tag{font-family:'JetBrains Mono',monospace;font-size:10px;border:1px solid var(--outline-variant);border-radius:8px;padding:1px 6px;color:var(--on-surface-muted)}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
