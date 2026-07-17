<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';

  type Stage = 'upload' | 'processing' | 'review' | 'history';

  let stage = $state<Stage>('upload');
  let error = $state('');
  let toast = $state('');

  let fileInput: HTMLInputElement;
  let dragging = $state(false);

  let uploadedName = $state('');
  let stagedFile = $state<File | null>(null);
  let streaming = $state(false);
  let logs = $state<any[]>([]);
  let logEl: HTMLDivElement;

  let docId = $state('');
  let detail = $state<any>(null);
  let annotUrl = $state('');
  let pdfUrl = $state('');
  let pdfView = $state<'pdf' | 'boxed'>('pdf');
  let edits = $state<Record<string, string>>({});
  let approving = $state(false);
  // Review-stage edit log (this session). Reset whenever the reviewed doc changes.
  let editLog = $state<{ field: string; label: string; from: string; to: string }[]>([]);
  $effect(() => { docId; editLog = []; });

  let history = $state<any[]>([]);
  let historyCount = $state(0);

  const LBL: Record<string, string> = {
    declaration_no: 'Declaration No (First-approval)', declaration_no_official: 'Declaration No (Official)',
    declaration_date: 'Date', importer_name: 'Importer', invoice_number: 'Invoice Number',
    currency: 'Currency', exchange_rate: 'Exchange Rate', invoice_price_mmk: 'Invoice Price (MMK)',
    total_customs_value: 'Total Customs Value', import_export_customs_duty: 'Import Duty (CD)',
    commercial_tax_ct: 'Commercial Tax (CT)', advance_income_tax_at: 'Income Tax (AT)',
    security_fee_sf: 'Security (SF)', maccs_service_fee_mf: 'MACCS Fee (MF)', exemption_reduction: 'Exemption/Reduction',
  };

  const H1: Record<Stage, string> = {
    upload: 'Process a Release Order',
    processing: 'Reading your document',
    review: 'Review & confirm',
    history: 'Approved history',
  };
  const SUB: Record<Stage, string> = {
    upload: 'Drop a Release-Order PDF. ROVER reads every field, math-verifies it, and flags anything uncertain before you approve.',
    processing: 'Every number is read and cross-checked. This usually takes 30–160 seconds.',
    review: 'Check the boxed form against the extracted fields. Nothing is saved until you approve.',
    history: 'Every document you have approved, with who signed off and when.',
  };
  const STEPS = ['Upload', 'Process', 'Review', 'Approve & Save'];
  const stepIndex = $derived(
    stage === 'upload' ? 0 : stage === 'processing' ? 1 : stage === 'review' ? 2 : 3
  );

  function fmt(v: any) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(String(v).replace(/,/g, ''));
    return isNaN(n) ? String(v) : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  function fmtBytes(b: number) {
    if (!b && b !== 0) return '';
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(1)} MB`;
  }
  function showToast(msg: string) {
    toast = msg;
    setTimeout(() => { if (toast === msg) toast = ''; }, 4000);
  }

  async function loadHistory() {
    try { history = await api.roverHistory(); historyCount = history.length; }
    catch (e: any) { error = e.message || 'Failed to load history'; }
  }
  onMount(async () => {
    try { const h = await api.roverHistory(); historyCount = h.length; } catch { /* non-fatal */ }
  });

  // Stage a picked/dropped file — does NOT auto-start extraction.
  function pickFile(files: FileList | null) {
    dragging = false;
    if (!files || !files.length) return;
    error = '';
    stagedFile = files[0];
    uploadedName = files[0].name;
  }
  function clearStaged() {
    stagedFile = null;
    uploadedName = '';
    error = '';
    if (fileInput) fileInput.value = '';
  }

  function addLog(msg: string, level = 'info') {
    logs = [...logs, { t: new Date().toLocaleTimeString(), msg, level }];
    // auto-scroll to bottom on next paint
    requestAnimationFrame(() => { if (logEl) logEl.scrollTop = logEl.scrollHeight; });
  }

  // User clicked Extract: stage server-side, then stream the live CLI log.
  async function runExtract() {
    if (!stagedFile || streaming) return;
    error = '';
    logs = [];
    const file = stagedFile;

    let res: any;
    try {
      res = await api.roverUpload(file);
      uploadedName = res?.pdf || file.name;
    } catch (e: any) { error = e.message || 'Upload failed'; return; }

    stage = 'processing';
    streaming = true;
    addLog(`Staged ${file.name} (${fmtBytes(file.size)})`, 'ok');
    addLog('Opening extraction stream…', 'info');

    try {
      await auth.ensureValidToken();
      const resp = await fetch(
        `/api/rover/extract/stream?pdf=${encodeURIComponent(res.pdf)}`,
        { headers: { Authorization: `Bearer ${auth.token}` } }
      );
      if (!resp.ok || !resp.body) throw new Error(`Stream failed (${resp.status})`);

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop() ?? '';
        for (const p of parts) {
          const ev = p.match(/^event: (.+)$/m)?.[1];
          const dm = p.match(/^data: (.+)$/m)?.[1];
          if (!dm) continue;
          let d: any;
          try { d = JSON.parse(dm); } catch { continue; }
          if (ev === 'log') addLog(d.msg, d.level);
          else if (ev === 'done') { docId = d.doc_id; await openReview(docId); }
          else if (ev === 'error') { error = d.msg; addLog(d.msg || 'Extraction error', 'error'); }
        }
      }
      if (stage === 'processing' && !error) {
        error = 'Stream ended before the document was ready. Check ROVER Review.';
      }
    } catch (e: any) {
      error = e.message || 'Extraction failed';
      addLog(error, 'error');
    } finally {
      streaming = false;
    }
  }

  async function openReview(id: string) {
    docId = id; detail = null; annotUrl = ''; edits = {};
    try { detail = await api.roverDocument(id); }
    catch (e: any) { error = e.message || 'Failed to load document'; stage = 'upload'; return; }
    // pre-fill edits from record cells
    const rec = detail?.record || {};
    for (const [col, cell] of Object.entries(rec)) {
      const v = (cell as any)?.value;
      if (v !== null && v !== undefined && v !== '') edits[col] = String(v);
    }
    try { pdfUrl = await api.roverPdfUrl(id); } catch { pdfUrl = ''; }
    try { annotUrl = await api.roverAnnotateUrl(id); } catch { annotUrl = ''; }
    pdfView = pdfUrl ? 'pdf' : 'boxed';
    stage = 'review';
  }

  function isSuspect(col: string): boolean {
    const s = detail?.suspect;
    if (!s) return false;
    if (Array.isArray(s)) return s.includes(col);
    return !!s[col];
  }

  // Review-stage field edit: update the value AND record it in the session edit log.
  function onFieldEdit(col: string, e: Event) {
    const to = (e.target as HTMLInputElement).value;
    edits[col] = to;
    const orig = detail?.record?.[col]?.value;
    const from = (orig === null || orig === undefined) ? '' : String(orig);
    if (to === from) { editLog = editLog.filter((x) => x.field !== col); return; }
    const entry = { field: col, label: LBL[col] || col, from, to };
    const i = editLog.findIndex((x) => x.field === col);
    editLog = i >= 0 ? editLog.map((x) => (x.field === col ? entry : x)) : [...editLog, entry];
  }

  function reject() {
    docId = ''; detail = null; annotUrl = ''; edits = {}; uploadedName = ''; error = '';
    stagedFile = null; logs = [];
    stage = 'upload';
  }

  async function approve() {
    if (!docId) return;
    approving = true; error = '';
    try {
      await api.roverApprove(docId, edits);
      showToast('Approved and saved to History.');
      await loadHistory();
      docId = ''; detail = null; annotUrl = ''; edits = {}; uploadedName = '';
      stagedFile = null; logs = [];
      stage = 'history';
    } catch (e: any) { error = e.message || 'Approve failed'; }
    approving = false;
  }

  async function authedDownload(url: string, name: string) {
    try {
      await auth.ensureValidToken();
      const res = await fetch(url, { headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {} });
      if (!res.ok) throw new Error('download failed');
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e: any) { error = e.message || 'Download failed'; }
  }
  const exportDocXlsx = (id: string) =>
    authedDownload(`/api/rover/documents/${encodeURIComponent(id)}/export.xlsx`, `${id}.xlsx`);
  const exportAllXlsx = () => authedDownload('/api/rover/export.xlsx', 'rover_history.xlsx');

  async function openHistory() {
    stage = 'history';
    await loadHistory();
  }
  async function viewDoc(id: string) {
    error = '';
    await openReview(id);
  }
</script>

<svelte:head><title>City Agent ROVER · Process</title></svelte:head>

<div class="rv">
  <header class="hd">
    <div>
      <div class="kick">City Agent ROVER</div>
      <h1>{H1[stage]}</h1>
      <p class="sub">{SUB[stage]}</p>
    </div>
    <button class="btn ghost" onclick={openHistory} aria-label="View history">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
      History{#if historyCount} · {historyCount}{/if}
    </button>
  </header>

  {#if stage !== 'history'}
    <ol class="steps" aria-label="Progress">
      {#each STEPS as s, i}
        <li class="step" class:done={i < stepIndex} class:cur={i === stepIndex}>
          <span class="sdot" aria-hidden="true">
            {#if i < stepIndex}
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            {:else}{i + 1}{/if}
          </span>
          <span class="slbl">{s}</span>
          {#if i < STEPS.length - 1}<span class="sbar" aria-hidden="true"></span>{/if}
        </li>
      {/each}
    </ol>
  {/if}

  {#if error}
    <div class="alert" role="alert">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      {error}
    </div>
  {/if}

  {#if stage === 'upload'}
    <input bind:this={fileInput} type="file" accept="application/pdf" hidden
           onchange={(e) => pickFile((e.target as HTMLInputElement).files)} />

    {#if !stagedFile}
      <button class="drop" class:drag={dragging} type="button"
              onclick={() => fileInput.click()}
              ondragover={(e) => { e.preventDefault(); dragging = true; }}
              ondragleave={() => dragging = false}
              ondrop={(e) => { e.preventDefault(); pickFile(e.dataTransfer?.files ?? null); }}>
        <svg class="di" viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <div class="dt">Drop a Release-Order PDF — or click to browse</div>
        <div class="ds">One document at a time. ROVER reads every field, math-verifies, and flags anything uncertain.</div>
      </button>
    {:else}
      <div class="card staged">
        <div class="staged-file">
          <span class="fico" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          </span>
          <div class="fmeta">
            <div class="fname">{stagedFile.name}</div>
            <div class="fsize">{fmtBytes(stagedFile.size)} · ready to extract</div>
          </div>
        </div>
        <div class="staged-btns">
          <button class="btn ghost" onclick={clearStaged}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m-9 0v14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V6"/></svg>
            Choose different file
          </button>
          <button class="btn" onclick={runExtract}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>
            Extract
          </button>
        </div>
      </div>
    {/if}

  {:else if stage === 'processing'}
    <div class="term">
      <div class="term-hd">
        {#if streaming}<span class="spin" aria-hidden="true"></span>{/if}
        <span class="term-title">{streaming ? 'Extracting…' : 'Extraction finished'}</span>
        <span class="term-name">{uploadedName || 'document'}</span>
      </div>
      <div class="term-body" bind:this={logEl} role="log" aria-live="polite" aria-label="Extraction log">
        {#each logs as l, i (i)}
          <div class="lrow lvl-{l.level || 'info'}">
            <span class="lt">{l.t}</span>
            <span class="ldot" aria-hidden="true"></span>
            <span class="lmsg">{l.msg}</span>
          </div>
        {/each}
        {#if streaming}
          <div class="lrow lvl-info"><span class="lt"></span><span class="ldot" aria-hidden="true"></span><span class="lmsg caret">▍</span></div>
        {/if}
      </div>
    </div>

  {:else if stage === 'review'}
    {#if !detail}
      <div class="card empty">Loading…</div>
    {:else}
      {@const rvCells = Object.entries(detail.record || {}).filter(([, c]) => (c as any)?.value !== null && (c as any)?.value !== undefined && (c as any)?.value !== '')}
      {@const rvFields = rvCells.length}
      {@const rvItems = detail.items || []}
      {@const rvSum = rvItems.reduce((s: number, it: any) => s + (Number(String(it?.value ?? '').replace(/,/g, '')) || 0), 0)}

      <!-- STATUS / ACTION BAR -->
      <div class="rvbar">
        <div class="rvbar-l">
          {#if detail.needs_review}
            <span class="schip pending">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              PENDING REVIEW
            </span>
          {:else}
            <span class="schip verified">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
              VERIFIED
            </span>
          {/if}
          <span class="rvmeta">{detail.document_name || docId} · {rvFields} fields · {rvItems.length} products · review={detail.needs_review ? 'true' : 'false'} · ${(Number(detail.cost) || 0).toFixed(4)}</span>
        </div>
        <div class="rvbar-r">
          <button class="btn ghost sm" onclick={reject}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            Discard
          </button>
          <button class="btn ghost sm" onclick={() => exportDocXlsx(docId)}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Excel
          </button>
          <button class="btn ghost sm danger" onclick={reject}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            Reject
          </button>
          <button class="btn ok sm" onclick={approve} disabled={approving}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            {approving ? 'Saving…' : 'Approve & Save'}
          </button>
        </div>
      </div>

      <!-- TWO-PANE REVIEW -->
      <div class="rvsplit">
        <!-- LEFT: PDF VIEWER -->
        <div class="rvcard pdfcard">
          <div class="rvch">
            <span class="rvct">PDF VIEWER</span>
            <div class="pdfseg">
              <button class="pseg" class:on={pdfView === 'pdf'} onclick={() => pdfView = 'pdf'} disabled={!pdfUrl}>Full PDF</button>
              <button class="pseg" class:on={pdfView === 'boxed'} onclick={() => pdfView = 'boxed'} disabled={!annotUrl}>Boxed form</button>
            </div>
          </div>
          <div class="pdfbody">
            {#if pdfView === 'pdf' && pdfUrl}
              <iframe class="pdfframe" src={pdfUrl} title="Source PDF (all pages)"></iframe>
            {:else if pdfView === 'boxed' && annotUrl}
              <img class="annot" src={annotUrl} alt="Extracted form with each field boxed" />
            {:else}
              <div class="noprev">
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/></svg>
                <div>Preview unavailable. Confirm the fields against the source.</div>
              </div>
            {/if}
          </div>
        </div>

        <!-- RIGHT: declaration + items + edit log -->
        <div class="rvstack">
          <!-- a) DECLARATION -->
          <div class="rvcard">
            <div class="rvch">
              <span class="rvct">DECLARATION</span>
              <span class="rvbadge">{rvFields} FIELDS</span>
            </div>
            <div class="dtw">
              <table class="dtable">
                <thead><tr><th class="c-field">FIELD</th><th class="c-val">VALUE</th><th class="c-ck" aria-label="verified">✓</th></tr></thead>
                <tbody>
                  {#each rvCells as [col] (col)}
                    {@const flagged = isSuspect(col)}
                    <tr class:flag={flagged}>
                      <td class="d-field">{LBL[col] || col}</td>
                      <td class="d-val">
                        <input class="d-input" value={edits[col] ?? ''} oninput={(e) => onFieldEdit(col, e)} aria-label={LBL[col] || col} />
                      </td>
                      <td class="d-ck">
                        {#if flagged}
                          <span class="mk warn" title="Needs confirmation">!</span>
                        {:else}
                          <span class="mk ok" title="Looks good">
                            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          </span>
                        {/if}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </div>

          <!-- b) PRODUCT ITEMS -->
          <div class="rvcard">
            <div class="rvch">
              <span class="rvct">PRODUCT ITEMS</span>
              <span class="rvbadge">[{rvItems.length} ROWS]</span>
            </div>
            <div class="dtw">
              <table class="dtable items">
                <thead><tr><th class="n">#</th><th>ITEM NAME</th><th>HS</th><th class="n">QTY</th><th>UNIT</th><th class="n">VALUE</th></tr></thead>
                <tbody>
                  {#each rvItems as it, i (it.no ?? it.hs_code ?? i)}
                    <tr>
                      <td class="n muted">{it.no ?? i + 1}</td>
                      <td class="iname">{it.item_name ?? it.description ?? ''}</td>
                      <td class="vmono">{it.hs_code ?? ''}</td>
                      <td class="n">{fmt(it.qty ?? it.quantity)}</td>
                      <td>{it.unit ?? ''}</td>
                      <td class="n">{fmt(it.value)}</td>
                    </tr>
                  {/each}
                  {#if !rvItems.length}<tr><td colspan="6" class="empty sm">No line items extracted.</td></tr>{/if}
                </tbody>
              </table>
            </div>
            <div class="isum">
              <span class="isum-t">TOTAL PRODUCTS: {rvItems.length}  ·  Σ VALUE: {fmt(rvSum)}  ·</span>
              {#if detail.needs_review}
                <span class="schip pending sm">NEEDS REVIEW</span>
              {:else}
                <span class="schip verified sm">BALANCED</span>
              {/if}
            </div>
          </div>

          <!-- c) EDIT LOG -->
          <div class="rvcard">
            <div class="rvch">
              <span class="rvct">EDIT LOG</span>
              <span class="rvbadge">({editLog.length} edits this session)</span>
            </div>
            {#if editLog.length}
              <div class="elog">
                {#each editLog as ed (ed.field)}
                  <div class="elog-row">
                    <span class="elog-f">{ed.label}:</span>
                    <span class="elog-from">{ed.from || '∅'}</span>
                    <svg class="elog-arr" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
                    <span class="elog-to">{ed.to || '∅'}</span>
                  </div>
                {/each}
              </div>
            {:else}
              <div class="elog-empty">No edits yet — edit any field above.</div>
            {/if}
          </div>
        </div>
      </div>
    {/if}

  {:else if stage === 'history'}
    <div class="hist-top">
      <button class="btn ghost" onclick={reject}>
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19" transform="rotate(180 12 12)"/></svg>
        Process another
      </button>
      <button class="btn" onclick={exportAllXlsx}>
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>
        Export all (Excel)
      </button>
    </div>
    <div class="tw">
      <table>
        <thead><tr><th>Document</th><th>Approved at</th><th>By</th><th class="n">Customs value</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {#each history as h (h.doc_id)}
            <tr>
              <td class="docid">{h.document_name || h.doc_id}</td>
              <td>{h.approved_at || h.reviewed_at || '—'}</td>
              <td>{h.approved_by || h.reviewed_by || '—'}</td>
              <td class="n">{fmt(h.total_customs_value)}</td>
              <td><span class="chip ok">{h.review_status || 'approved'}</span></td>
              <td class="rowacts">
                <button class="btn ghost sm" onclick={() => viewDoc(h.doc_id)}>
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
                  View
                </button>
                <button class="btn ghost sm" onclick={() => exportDocXlsx(h.doc_id)}>
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>
                  Excel
                </button>
              </td>
            </tr>
          {/each}
          {#if !history.length}<tr><td colspan="6" class="empty">No approved documents yet.</td></tr>{/if}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if toast}
  <div class="toast" role="status">
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    {toast}
  </div>
{/if}

<style>
  .rv{max-width:1280px;margin:0 auto;padding:26px 24px 96px;color:var(--on-surface);font-family:'Inter',-apple-system,sans-serif}
  .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
  .kick{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--primary);font-weight:600}
  h1{font-family:'Source Serif 4',Georgia,serif;font-size:30px;margin:4px 0 6px;letter-spacing:-.01em}
  .sub{color:var(--on-surface-muted);margin:0;max-width:64ch;font-size:14px;line-height:1.6}
  .badge{background:var(--surface-container-high);color:var(--on-surface-muted);border-radius:20px;padding:1px 9px;font-size:12px;font-family:'JetBrains Mono',monospace}
  /* buttons */
  .btn{display:inline-flex;align-items:center;gap:7px;background:var(--primary);color:var(--on-primary);border:none;
    border-radius:var(--radius-md);padding:9px 15px;font-weight:600;cursor:pointer;font-size:13.5px;
    transition:background .18s ease,box-shadow .18s ease}
  .btn:hover{background:var(--primary-hover)} .btn:disabled{opacity:.55;cursor:default}
  .btn:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
  .btn.ghost{background:transparent;color:var(--primary);border:1px solid var(--outline)}
  .btn.ghost:hover{background:var(--primary-container)}
  .btn.ok{background:var(--success);color:#fff}
  .btn.ok:hover{filter:brightness(.94)}
  .btn.ok:focus-visible{outline:2px solid var(--success);outline-offset:2px}
  .btn.sm{padding:6px 11px;font-size:12.5px}
  /* progress steps */
  .steps{list-style:none;display:flex;align-items:center;gap:0;margin:22px 0 6px;padding:0;flex-wrap:wrap}
  .step{display:flex;align-items:center;gap:9px;position:relative;color:var(--on-surface-muted);font-size:13px}
  .sdot{width:24px;height:24px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;border-radius:50%;
    font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;border:1.5px solid var(--outline);
    background:var(--surface-container-lowest);color:var(--on-surface-muted);transition:all .18s ease}
  .slbl{white-space:nowrap}
  .sbar{width:36px;height:1.5px;background:var(--outline-variant);margin:0 12px}
  .step.done .sdot{background:var(--success);border-color:var(--success);color:#fff}
  .step.done .slbl{color:var(--on-surface)}
  .step.cur .sdot{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}
  .step.cur .slbl{color:var(--on-surface);font-weight:600}
  @media (max-width:640px){.slbl{display:none}.sbar{width:20px;margin:0 6px}}
  /* alert */
  .alert{display:flex;align-items:center;gap:9px;background:var(--error-soft);color:var(--error);
    border:1px solid var(--error-container,var(--outline));border-radius:var(--radius-md);padding:10px 14px;margin:14px 0;font-size:14px}
  /* drop zone */
  .drop{width:100%;text-align:center;margin:20px 0 6px;border:2px dashed var(--outline);border-radius:var(--radius-xl,16px);
    padding:44px 28px;cursor:pointer;background:var(--surface-container-lowest);color:var(--on-surface);
    transition:border-color .18s ease,background .18s ease;display:flex;flex-direction:column;align-items:center;gap:5px}
  .drop:hover,.drop:focus-visible,.drop.drag{border-color:var(--primary);background:var(--primary-container);outline:none}
  .di{color:var(--primary);margin-bottom:4px} .dt{font-size:15px;font-weight:600} .ds{font-size:13px;color:var(--on-surface-muted);max-width:52ch}
  /* staged file card */
  .staged{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-top:20px;padding:16px 18px}
  .staged-file{display:flex;align-items:center;gap:13px;min-width:0}
  .fico{width:42px;height:42px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;border-radius:var(--radius-md);
    background:var(--primary-container);color:var(--primary)}
  .fmeta{min-width:0}
  .fname{font-weight:600;font-size:14.5px;word-break:break-all}
  .fsize{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--on-surface-muted);margin-top:2px}
  .staged-btns{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
  /* live terminal log */
  .term{margin-top:20px;border:1px solid var(--outline-variant);border-radius:var(--radius-lg);overflow:hidden;box-shadow:var(--shadow-xs)}
  .term-hd{display:flex;align-items:center;gap:10px;padding:11px 15px;background:#0b111b;border-bottom:1px solid rgba(255,255,255,.08)}
  .term-title{font-family:'JetBrains Mono',monospace;font-size:12.5px;font-weight:600;color:#e6edf3}
  .term-name{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:#7d8ba1;margin-left:auto;word-break:break-all}
  .term-body{max-height:340px;overflow:auto;background:#0f1720;color:#c7d2de;
    font-family:'JetBrains Mono',monospace;font-size:12.5px;line-height:1.65;padding:12px 15px}
  .lrow{display:flex;align-items:baseline;gap:9px;padding:1px 0}
  .lt{color:#5a6b80;flex-shrink:0;font-size:11px;min-width:64px}
  .ldot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:#5a6b80;align-self:center}
  .lmsg{white-space:pre-wrap;word-break:break-word;color:#c7d2de}
  .lvl-ok .ldot{background:#3fb950} .lvl-ok .lmsg{color:#7ee787}
  .lvl-warn .ldot{background:#d29922} .lvl-warn .lmsg{color:#e3b341}
  .lvl-error .ldot{background:#f85149} .lvl-error .lmsg{color:#ff7b72}
  .lvl-info .lmsg{color:#c7d2de}
  .caret{color:#7d8ba1;animation:blink 1s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.22);border-top-color:#e6edf3;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  /* cards */
  .card{background:var(--surface-container-lowest);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    padding:14px 16px;margin-bottom:14px;box-shadow:var(--shadow-xs)}
  .ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .ch b{font-family:'Source Serif 4',Georgia,serif;font-size:16px;font-weight:600}
  .empty{color:var(--on-surface-muted);text-align:center;font-size:14px;padding:24px}
  .chip{font-family:'JetBrains Mono',monospace;font-size:11px;border-radius:20px;padding:2px 9px;font-weight:500;white-space:nowrap}
  .chip.warn{color:var(--warning);background:var(--warning-soft)} .chip.ok{color:var(--success);background:var(--success-soft)}

  /* ============ REVIEW CONSOLE (dense two-pane) ============ */
  /* status / action bar */
  .rvbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-top:16px;
    background:var(--surface-container-lowest);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    padding:10px 14px;box-shadow:var(--shadow-xs)}
  .rvbar-l{display:flex;align-items:center;gap:12px;min-width:0;flex-wrap:wrap}
  .rvbar-r{display:flex;gap:7px;flex-wrap:wrap;margin-left:auto}
  .rvmeta{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--on-surface-muted);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .schip{display:inline-flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;
    letter-spacing:.04em;border-radius:20px;padding:4px 11px;white-space:nowrap;border:1px solid transparent}
  .schip.sm{padding:3px 9px;font-size:10.5px}
  .schip.pending{color:var(--warning);background:var(--warning-soft);border-color:color-mix(in srgb,var(--warning) 30%,transparent)}
  .schip.verified{color:var(--success);background:var(--success-soft);border-color:color-mix(in srgb,var(--success) 30%,transparent)}
  .btn.ghost.danger{color:var(--error);border-color:color-mix(in srgb,var(--error) 40%,var(--outline))}
  .btn.ghost.danger:hover{background:var(--error-soft)}
  .btn.ghost.danger:focus-visible{outline:2px solid var(--error);outline-offset:2px}
  /* two-pane split */
  .rvsplit{display:grid;grid-template-columns:1.05fr 0.95fr;gap:18px;align-items:start;margin-top:14px}
  @media (max-width:1050px){.rvsplit{grid-template-columns:1fr}}
  .rvstack{display:flex;flex-direction:column;gap:14px;min-width:0}
  /* review cards */
  .rvcard{background:var(--surface-container-lowest);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    box-shadow:var(--shadow-xs);overflow:hidden}
  .rvch{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 13px;
    background:var(--surface-container-low);border-bottom:1px solid var(--outline-variant)}
  .rvct{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;letter-spacing:.1em;
    text-transform:uppercase;color:var(--on-surface-muted)}
  .rvbadge{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--on-surface-muted);
    background:var(--surface-container-high,var(--surface));border:1px solid var(--outline-variant);
    border-radius:6px;padding:1px 7px;white-space:nowrap}
  /* pdf pane */
  .pdfcard{position:sticky;top:14px}
  @media (max-width:1050px){.pdfcard{position:static}}
  .pdfseg{display:inline-flex;gap:2px;background:var(--surface-container-lowest);border:1px solid var(--outline);
    border-radius:var(--radius-md);padding:2px}
  .pseg{border:none;background:transparent;color:var(--on-surface-muted);font:inherit;font-size:11.5px;font-weight:600;
    padding:4px 11px;border-radius:calc(var(--radius-md) - 3px);cursor:pointer;transition:background .15s,color .15s}
  .pseg.on{background:var(--primary);color:var(--on-primary)}
  .pseg:disabled{opacity:.4;cursor:default} .pseg:not(.on):not(:disabled):hover{background:var(--surface-container-low);color:var(--on-surface)}
  .pseg:focus-visible{outline:2px solid var(--primary);outline-offset:1px}
  .pdfbody{padding:10px}
  .pdfframe{width:100%;height:78vh;min-height:520px;border:1px solid var(--outline-variant);border-radius:var(--radius-md);background:#fff;display:block}
  .annot{width:100%;border:1px solid var(--outline-variant);border-radius:var(--radius-md);background:#fff;display:block}
  .noprev{display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;color:var(--on-surface-muted);
    border:1px dashed var(--outline);border-radius:var(--radius-md);padding:40px 24px;font-size:13px}
  .noprev svg{color:var(--on-surface-muted)}
  /* dense declaration + items tables */
  .dtw{overflow-x:auto}
  .dtable{border-collapse:collapse;width:100%;font-size:13px}
  .dtable th{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.05em;
    color:var(--on-surface-muted);background:var(--surface-container-low);text-align:left;padding:6px 10px;
    border-bottom:1px solid var(--outline-variant);font-weight:600;position:sticky;top:0}
  .dtable td{padding:4px 10px;border-bottom:1px solid var(--outline-variant);vertical-align:middle}
  .dtable tr:last-child td{border-bottom:none}
  .dtable th.c-ck,.dtable td.d-ck{text-align:center;width:34px}
  .dtable th.c-field,.dtable td.d-field{width:38%}
  .d-field{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--primary);font-weight:600;
    text-transform:uppercase;letter-spacing:.02em;line-height:1.35}
  .d-val{padding:3px 8px}
  .d-input{width:100%;background:var(--surface);border:1px solid var(--outline);color:var(--on-surface);
    border-radius:var(--radius-sm);padding:5px 8px;font-size:13px;font-family:'JetBrains Mono',monospace;
    transition:border-color .15s ease,box-shadow .15s ease}
  .d-input:hover{border-color:var(--outline)}
  .d-input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-container)}
  .dtable tr.flag{background:var(--warning-soft)}
  .dtable tr.flag .d-input{border-color:color-mix(in srgb,var(--warning) 45%,var(--outline))}
  .mk{display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;border-radius:50%;font-weight:800;font-size:12px}
  .mk.ok{color:var(--success);background:var(--success-soft)}
  .mk.warn{color:var(--warning);background:var(--warning-soft)}
  /* items table */
  .dtable.items th.n,.dtable.items td.n{text-align:right;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
  .dtable.items td{padding:5px 10px}
  .dtable.items .iname{max-width:220px}
  .dtable.items .vmono{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;white-space:nowrap}
  .dtable.items .muted{color:var(--on-surface-muted)}
  .empty.sm{padding:14px;font-size:13px}
  .isum{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
    padding:8px 13px;border-top:1px solid var(--outline-variant);background:var(--surface-container-low)}
  .isum-t{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--on-surface-muted)}
  /* edit log */
  .elog{display:flex;flex-direction:column}
  .elog-row{display:flex;align-items:center;gap:8px;padding:6px 13px;border-bottom:1px solid var(--outline-variant);
    font-family:'JetBrains Mono',monospace;font-size:12px;flex-wrap:wrap}
  .elog-row:last-child{border-bottom:none}
  .elog-f{color:var(--primary);font-weight:600}
  .elog-from{color:var(--on-surface-muted);background:var(--error-soft);border-radius:5px;padding:1px 6px;text-decoration:line-through;text-decoration-color:var(--error)}
  .elog-arr{color:var(--on-surface-muted);flex-shrink:0}
  .elog-to{color:var(--success);background:var(--success-soft);border-radius:5px;padding:1px 6px;font-weight:600}
  .elog-empty{padding:14px 13px;color:var(--on-surface-muted);font-size:13px}
  /* history */
  .hist-top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0 14px}
  .rowacts{display:flex;gap:6px;white-space:nowrap}
  /* tables */
  .tw{overflow-x:auto;border:1px solid var(--outline-variant);border-radius:var(--radius-md);background:var(--surface-container-lowest)}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--outline-variant);vertical-align:top}
  th{font-family:'JetBrains Mono',monospace;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--on-surface-muted);
    background:var(--surface-container-low);position:sticky;top:0}
  th.n,td.n{text-align:right;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  td.docid{font-family:'JetBrains Mono',monospace;color:var(--primary);font-weight:500}
  td.vmono{font-family:'JetBrains Mono',monospace;font-weight:600;white-space:nowrap}
  /* toast */
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:80;
    display:inline-flex;align-items:center;gap:9px;background:var(--success);color:#fff;
    border-radius:var(--radius-lg);padding:11px 18px;font-size:13.5px;font-weight:600;
    box-shadow:0 8px 28px rgba(0,0,0,.18);animation:rise .22s ease}
  @keyframes rise{from{opacity:0;transform:translate(-50%,10px)}to{opacity:1;transform:translate(-50%,0)}}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
