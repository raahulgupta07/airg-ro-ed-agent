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
  let detail = $state<any>(null);        // full document (record + items)
  let detailLoading = $state(false);
  let annotUrl = $state('');
  let showForm = $state(false);

  let corrections = $state<Record<string, Record<string, string>>>({});
  let applying = $state('');
  let processing = $state<string[]>([]);
  let fileInput: HTMLInputElement;

  const LBL: Record<string, string> = {
    declaration_no: 'Declaration No (First-approval)',
    declaration_no_official: 'Declaration No (Official)',
    declaration_date: 'Date', importer_name: 'Importer', invoice_number: 'Invoice Number',
    currency: 'Currency', exchange_rate: 'Exchange Rate', invoice_price_mmk: 'Invoice Price (MMK)',
    total_customs_value: 'Total Customs Value', import_export_customs_duty: 'Import Duty (CD)',
    commercial_tax_ct: 'Commercial Tax (CT)', advance_income_tax_at: 'Income Tax (AT)',
    security_fee_sf: 'Security (SF)', maccs_service_fee_mf: 'MACCS Fee (MF)',
    exemption_reduction: 'Exemption/Reduction',
  };

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

  async function openDoc(docId: string) {
    selected = docId; detail = null; annotUrl = ''; showForm = false; detailLoading = true;
    try { detail = await api.roverDocument(docId); }
    catch (e: any) { error = e.message; }
    detailLoading = false;
  }

  async function loadForm() {
    if (annotUrl || !selected) { showForm = !showForm; return; }
    try { annotUrl = await api.roverAnnotateUrl(selected); showForm = true; }
    catch { error = 'Annotated preview not available for this document.'; }
  }

  async function onUpload(files: FileList | null) {
    if (!files || !files.length) return;
    error = '';
    const names = Array.from(files).map((f) => f.name);
    const before = documents.length;
    processing = [...processing, ...names];
    try { for (const f of Array.from(files)) await api.roverUpload(f); }
    catch (e: any) { error = e.message || 'Upload failed'; }
    const target = before + names.length;
    for (let i = 0; i < 80; i++) {
      await new Promise((r) => setTimeout(r, 4000));
      await load();
      if (documents.length >= target) break;
    }
    processing = processing.filter((n) => !names.includes(n));
  }

  function setCorrection(docId: string, col: string, val: string) {
    corrections[docId] = { ...(corrections[docId] || {}), [col]: val };
  }
  async function applyDoc(item: any) {
    const c = corrections[item.doc_id] || {};
    const payload: Record<string, any> = {};
    for (const f of item.fields) payload[f.column] = (c[f.column] ?? '') !== '' ? c[f.column] : f.value;
    applying = item.doc_id;
    try { await api.roverApply(item.doc_id, payload); await load(); if (selected === item.doc_id) openDoc(item.doc_id); }
    catch (e: any) { error = e.message; }
    applying = '';
  }

  async function exportCsv() {
    try {
      await auth.ensureValidToken();
      const res = await fetch('/api/rover/export.csv', { headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {} });
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = 'rover_review.csv';
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e: any) { error = e.message; }
  }

  function fmt(v: any) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(String(v).replace(/,/g, ''));
    return isNaN(n) ? String(v) : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  function download(name: string, text: string, type = 'application/json') {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
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

  <div class="drop" role="button" tabindex="0"
       onclick={() => fileInput.click()}
       onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && fileInput.click()}
       ondragover={(e) => e.preventDefault()}
       ondrop={(e) => { e.preventDefault(); onUpload(e.dataTransfer?.files ?? null); }}>
    <input bind:this={fileInput} type="file" accept="application/pdf" multiple hidden
           onchange={(e) => onUpload((e.target as HTMLInputElement).files)} />
    <div class="drop-t">⬆ Drop Release-Order PDFs here — or click to browse</div>
    <div class="drop-s">ROVER reads every field, math-verifies, and flags anything uncertain.</div>
  </div>
  {#if processing.length}
    <div class="proc"><span class="spin"></span>
      Extracting {processing.length} doc{processing.length > 1 ? 's' : ''} — {processing.join(', ')}
      <span class="proc-s">reading + math-verifying (30–160s each)… this updates automatically.</span></div>
  {/if}

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
    <section>
      <h2>Documents <span class="badge">{documents.length}</span></h2>
      <div class="tblwrap">
        <table>
          <thead><tr><th>Doc</th><th class="num">Rate</th><th class="num">Customs value</th><th>Status</th></tr></thead>
          <tbody>
            {#each documents as d (d.doc_id)}
              <tr class:sel={selected === d.doc_id} onclick={() => openDoc(d.doc_id)}>
                <td class="docid">{d.doc_id}</td>
                <td class="num">{fmt(d.exchange_rate)}</td>
                <td class="num">{fmt(d.total_customs_value)}</td>
                <td>{#if d.needs_review}<span class="pill warn">review</span>{:else}<span class="pill ok">clean</span>{/if}</td>
              </tr>
            {/each}
            {#if !documents.length && !loading}<tr><td colspan="4" class="empty">No documents yet — drop a PDF above.</td></tr>{/if}
          </tbody>
        </table>
      </div>

      {#if queue.length}
        <h2 style="margin-top:22px">Needs your confirmation <span class="badge warn">{queue.length}</span></h2>
        {#each queue as item (item.doc_id)}
          <div class="card">
            <div class="card-h"><b>{item.pdf || item.doc_id}</b><span class="pill warn">{item.fields.length} to confirm</span></div>
            {#each item.fields as f (f.column)}
              <div class="field">
                <div class="fcol">{LBL[f.column] || f.column}</div>
                <div class="fval">current <b>{fmt(f.value)}</b>{#if f.alternates?.length} · alt {f.alternates.map(fmt).join(', ')}{/if}</div>
                <div class="fev">{f.evidence || f.reason}</div>
                <input class="fin" placeholder="confirm / correct"
                       value={corrections[item.doc_id]?.[f.column] ?? ''}
                       oninput={(e) => setCorrection(item.doc_id, f.column, (e.target as HTMLInputElement).value)} />
              </div>
            {/each}
            <button class="btn sm" onclick={() => applyDoc(item)} disabled={applying === item.doc_id}>
              {applying === item.doc_id ? 'Saving…' : 'Confirm all'}</button>
          </div>
        {/each}
      {/if}
    </section>

    <section>
      <h2>Extracted data {#if selected}<span class="badge">{selected}</span>{/if}</h2>
      {#if !selected}
        <div class="empty card">Click a document to see every extracted field, its value, and where it was read from.</div>
      {:else if detailLoading}
        <div class="empty card">Loading…</div>
      {:else if detail}
        <div class="card">
          <div class="detail-actions">
            <button class="btn ghost sm" onclick={loadForm}>{showForm ? 'Hide form' : '🖼 Show boxed form'}</button>
            <button class="btn ghost sm" onclick={downloadJson}>⬇ Raw JSON</button>
            {#if detail.items?.length}<button class="btn ghost sm" onclick={downloadItemsCsv}>⬇ Products CSV</button>{/if}
          </div>
          {#if showForm && annotUrl}<img class="annot" src={annotUrl} alt="annotated form" />{/if}
          <div class="tblwrap" style="margin-top:12px">
            <table>
              <thead><tr><th>Field</th><th>Value</th><th>Read by</th><th class="num">Conf.</th><th>Evidence</th></tr></thead>
              <tbody>
                {#each Object.entries(detail.record || {}) as [col, cell] (col)}
                  {#if (cell as any).value !== null && (cell as any).value !== undefined && (cell as any).value !== ''}
                    <tr>
                      <td>{LBL[col] || col}</td>
                      <td class="v">{fmt((cell as any).value)}</td>
                      <td><span class="tag">{String((cell as any).model || '').split('/').pop()}</span></td>
                      <td class="num conf">{(cell as any).confidence ?? ''}</td>
                      <td class="src">{(cell as any).source || ''}</td>
                    </tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          </div>
          {#if detail.items?.length}
            <h3>Products <span class="badge">{detail.items.length}</span></h3>
            <div class="tblwrap">
              <table>
                <thead><tr><th>#</th><th>HS code</th><th>Description</th><th class="num">Qty</th><th>Unit</th><th class="num">Value</th></tr></thead>
                <tbody>
                  {#each detail.items as it (it.no ?? it.hs_code)}
                    <tr><td>{it.no ?? ''}</td><td class="v">{it.hs_code ?? ''}</td><td>{it.description ?? ''}</td>
                      <td class="num">{fmt(it.quantity)}</td><td>{it.unit ?? ''}</td><td class="num">{fmt(it.value)}</td></tr>
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
  .rover { max-width: 1240px; margin: 0 auto; padding: 26px 24px 70px;
    color: var(--on-surface); font-family: 'Inter', -apple-system, sans-serif; }
  .rhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
  .kick { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: .15em;
    text-transform: uppercase; color: var(--primary); font-weight: 600; }
  h1 { font-family: 'Source Serif 4', Georgia, serif; font-size: 30px; margin: 4px 0 6px; color: var(--on-surface); }
  h2 { font-family: 'Source Serif 4', Georgia, serif; font-size: 19px; margin: 0 0 12px;
    display: flex; align-items: center; gap: 8px; }
  h3 { font-family: 'Source Serif 4', Georgia, serif; font-size: 16px; margin: 18px 0 8px; }
  .sub { color: var(--on-surface-muted); margin: 0; max-width: 62ch; font-size: 14px; }
  .badge { background: var(--surface-container-high); color: var(--on-surface-muted); border-radius: 20px;
    padding: 1px 9px; font-size: 12px; font-family: 'JetBrains Mono', monospace; }
  .badge.warn { background: var(--warning-soft); color: var(--warning); }
  .err { background: var(--error-soft); color: var(--error); border: 1px solid var(--error-container);
    border-radius: var(--radius-md); padding: 10px 14px; margin: 14px 0; font-size: 14px; }
  .drop { margin: 18px 0 6px; border: 2px dashed var(--outline); border-radius: var(--radius-xl); padding: 26px;
    text-align: center; cursor: pointer; background: var(--surface-container-lowest); transition: border-color .15s, background .15s; }
  .drop:hover, .drop:focus { border-color: var(--primary); background: var(--primary-container); outline: none; }
  .drop-t { font-size: 16px; font-weight: 600; color: var(--on-surface); }
  .drop-s { font-size: 13px; color: var(--on-surface-muted); margin-top: 6px; }
  .proc { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 6px;
    background: var(--primary-container); border: 1px solid var(--primary); border-radius: var(--radius-lg);
    padding: 12px 16px; font-size: 14px; color: var(--on-surface); }
  .proc-s { color: var(--on-surface-muted); font-size: 12px; width: 100%; }
  .spin { width: 14px; height: 14px; border: 2px solid var(--outline-variant); border-top-color: var(--primary);
    border-radius: 50%; animation: spin .8s linear infinite; display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin: 20px 0 24px; }
  .stat { background: var(--surface-container-lowest); border: 1px solid var(--outline-variant);
    border-radius: var(--radius-lg); padding: 14px 18px; min-width: 118px; box-shadow: var(--shadow-xs); }
  .stat .v { font-family: 'JetBrains Mono', monospace; font-size: 26px; font-weight: 700; }
  .stat .k { font-size: 12px; color: var(--on-surface-muted); margin-top: 2px; }
  .stat.ok .v { color: var(--success); } .stat.warn .v { color: var(--warning); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: start; }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--surface-container-lowest); border: 1px solid var(--outline-variant);
    border-radius: var(--radius-lg); padding: 14px 16px; margin-bottom: 12px; box-shadow: var(--shadow-xs); }
  .card-h { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .field { display: flex; flex-direction: column; gap: 3px; padding: 8px 0; border-top: 1px solid var(--outline-variant); }
  .fcol { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--primary); }
  .fval { font-size: 13px; } .fval b { font-family: 'JetBrains Mono', monospace; }
  .fev { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--on-surface-subtle); }
  .fin { margin-top: 4px; background: var(--surface); border: 1px solid var(--outline); color: var(--on-surface);
    border-radius: var(--radius-sm); padding: 6px 9px; font-size: 13px; font-family: 'JetBrains Mono', monospace; }
  .fin:focus { outline: none; border-color: var(--primary); }
  .btn { background: var(--primary); color: var(--on-primary); border: none; border-radius: var(--radius-md);
    padding: 9px 16px; font-weight: 600; cursor: pointer; font-size: 14px; }
  .btn:hover { background: var(--primary-hover); }
  .btn.sm { padding: 7px 14px; font-size: 13px; margin-top: 8px; }
  .btn.ghost { background: transparent; color: var(--primary); border: 1px solid var(--outline); }
  .btn.ghost:hover { background: var(--primary-container); }
  .btn.ghost.sm { margin-top: 0; margin-bottom: 4px; }
  .detail-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .pill { font-family: 'JetBrains Mono', monospace; font-size: 11px; border-radius: 20px; padding: 2px 9px; }
  .pill.warn { color: var(--warning); background: var(--warning-soft); }
  .pill.ok { color: var(--success); background: var(--success-soft); }
  .empty { color: var(--on-surface-muted); text-align: center; font-size: 14px; padding: 22px; }
  .annot { width: 100%; border: 1px solid var(--outline-variant); border-radius: var(--radius-md); background: #fff; margin: 8px 0; }
  .tblwrap { overflow-x: auto; border: 1px solid var(--outline-variant); border-radius: var(--radius-md); background: var(--surface-container-lowest); }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--outline-variant); vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; color: var(--on-surface-muted); font-family: 'JetBrains Mono', monospace; }
  th.num, td.num { text-align: right; font-family: 'JetBrains Mono', monospace; }
  tr:last-child td { border-bottom: none; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: var(--surface-container-low); }
  tr.sel { background: var(--primary-container); }
  td.docid { font-family: 'JetBrains Mono', monospace; color: var(--primary); }
  td.v { font-family: 'JetBrains Mono', monospace; font-weight: 600; white-space: nowrap; }
  td.conf { color: var(--success); } td.src { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--on-surface-subtle); }
  .tag { font-family: 'JetBrains Mono', monospace; font-size: 10px; border: 1px solid var(--outline-variant);
    border-radius: 8px; padding: 1px 6px; color: var(--on-surface-muted); }
</style>
