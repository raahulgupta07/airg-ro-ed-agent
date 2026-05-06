<script lang="ts">
  import { auth } from '$lib/stores/auth.svelte';
  import Toast from './Toast.svelte';

  let {
    jobId,
    job,
    onApprove,
    onReject,
    onClose,
  }: {
    jobId: string;
    job: any;
    onApprove?: () => void;
    onReject?: () => void;
    onClose?: () => void;
  } = $props();

  // ── Local working copy (optimistic edits) ──
  let workingDecl = $state<Record<string, any>>({ ...(job?.declaration ?? {}) });
  let workingItems = $state<any[]>(Array.isArray(job?.items) ? job.items.map((i: any) => ({ ...i })) : []);

  // Track originals so we can detect user-edited fields (border-blue)
  const originalDecl: Record<string, any> = { ...(job?.declaration ?? {}) };
  const originalItems: any[] = Array.isArray(job?.items) ? job.items.map((i: any) => ({ ...i })) : [];

  // Item review flags from job (yellow border on flagged fields)
  let flags = $state<any[]>(Array.isArray(job?.item_review_flags) ? job.item_review_flags : []);

  // Edit log (visual only — backend already persisted via PATCH per save)
  type LogEntry = { ts: string; field: string; before: string; after: string; user: string };
  let editLog = $state<LogEntry[]>([]);
  let editLogCollapsed = $state(false);

  // Toast
  let toastVisible = $state(false);
  let toastMsg = $state('');
  let toastType = $state<'success' | 'error'>('success');
  function toast(msg: string, type: 'success' | 'error' = 'success') {
    toastMsg = msg;
    toastType = type;
    toastVisible = true;
    setTimeout(() => { toastVisible = false; }, 2200);
  }

  // ── Inline editing state ──
  let editingKey = $state<string | null>(null); // e.g. "decl:currency" or "item:0:hs_code"
  let editValue = $state('');

  function startEdit(key: string, current: any) {
    editingKey = key;
    editValue = current == null ? '' : String(current);
  }
  function cancelEdit() {
    editingKey = null;
    editValue = '';
  }

  // ── Reject / approve modals ──
  let showRejectModal = $state(false);
  let rejectNotes = $state('');
  let showApproveConfirm = $state(false);
  let approveNotes = $state('');

  // ── PDF blob loading (auth-safe iframe) ──
  let pdfBlobUrl = $state<string>('');
  let pdfLoading = $state(true);
  let pdfError = $state('');

  $effect(() => {
    let cancelled = false;
    let revokeUrl: string | null = null;
    (async () => {
      pdfLoading = true;
      pdfError = '';
      try {
        const r = await fetch(`/api/jobs/${jobId}/pdf`, {
          headers: { 'Authorization': `Bearer ${auth.token}` },
          credentials: 'include',
        });
        if (!r.ok) throw new Error(`pdf ${r.status}`);
        const blob = await r.blob();
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        revokeUrl = url;
        pdfBlobUrl = url;
      } catch (e: any) {
        // Fallback: use token-querystring URL
        if (!cancelled) {
          pdfBlobUrl = `/api/jobs/${jobId}/pdf?token=${auth.token}`;
          pdfError = '';
        }
      } finally {
        if (!cancelled) pdfLoading = false;
      }
    })();
    return () => {
      cancelled = true;
      if (revokeUrl) URL.revokeObjectURL(revokeUrl);
    };
  });

  // ── Page strip ──
  const totalPages = $derived(job?.total_pages ?? job?.pages?.length ?? 0);
  let currentPage = $state(1);
  const pageList = $derived(
    Array.isArray(job?.pages) && job.pages.length
      ? job.pages
      : Array.from({ length: totalPages }, (_, i) => ({ page_number: i + 1, page_type: 'PAGE' }))
  );
  function pageBadge(p: any) {
    const t = (p?.page_type || 'PAGE').toString().slice(0, 3).toUpperCase();
    return t;
  }
  function jumpToPage(n: number) {
    currentPage = n;
    // PDF.js style hash navigation: #page=N
    if (pdfBlobUrl && !pdfBlobUrl.startsWith('blob:')) {
      // For server URLs, we can append #page= safely
      // (blob URLs don't support page hash in all browsers; harmless either way)
    }
  }
  const pdfSrc = $derived(pdfBlobUrl ? `${pdfBlobUrl}#page=${currentPage}&zoom=page-fit` : '');

  // ── Status / counters ──
  const status: string = job?.review_status || job?.status || 'PENDING_REVIEW';
  const filename: string = job?.pdf_name || job?.filename || 'document.pdf';
  const cost: number = job?.cost_usd || 0;
  const flagCount = $derived(Array.isArray(flags) ? flags.length : 0);
  const editCount = $derived(editLog.length);
  let unsavedDirty = $state(false);

  // ── Save (PATCH) helpers with optimistic update ──
  async function saveDeclField(field: string) {
    const before = workingDecl[field];
    const after = editValue;
    if (String(before ?? '') === String(after ?? '')) {
      cancelEdit();
      return;
    }
    // optimistic
    workingDecl = { ...workingDecl, [field]: after };
    editingKey = null;
    try {
      const r = await fetch(`/api/review/${jobId}/declaration`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ field, value: after }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      editLog = [...editLog, {
        ts: new Date().toISOString().slice(11, 19),
        field: `DECL.${field.toUpperCase()}`,
        before: String(before ?? '—'),
        after: String(after ?? '—'),
        user: auth?.user?.username || 'admin',
      }];
      unsavedDirty = true;
    } catch (e) {
      // revert
      workingDecl = { ...workingDecl, [field]: before };
      toast('Save failed', 'error');
    }
  }

  async function saveItemField(idx: number, field: string) {
    const item = workingItems[idx] || {};
    const before = item[field];
    const after = editValue;
    if (String(before ?? '') === String(after ?? '')) {
      cancelEdit();
      return;
    }
    const newItem = { ...item, [field]: after };
    const newItems = [...workingItems];
    newItems[idx] = newItem;
    workingItems = newItems;
    editingKey = null;
    try {
      const r = await fetch(`/api/review/${jobId}/items/${idx}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ field, value: after }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      editLog = [...editLog, {
        ts: new Date().toISOString().slice(11, 19),
        field: `ITEM[${idx + 1}].${field.toUpperCase()}`,
        before: String(before ?? '—'),
        after: String(after ?? '—'),
        user: auth?.user?.username || 'admin',
      }];
      unsavedDirty = true;
    } catch {
      // revert
      const reverted = [...workingItems];
      reverted[idx] = { ...reverted[idx], [field]: before };
      workingItems = reverted;
      toast('Save failed', 'error');
    }
  }

  // ── Add new item row ──
  function addItemRow() {
    workingItems = [
      ...workingItems,
      {
        item_name: '',
        hs_code: '',
        quantity: '',
        invoice_unit_price: '',
        cif_unit_price: '',
        currency: workingDecl.currency || '',
      },
    ];
    toast('Row added (saves on first edit)');
  }

  // ── Border color rules ──
  function isFlagged(scope: 'decl' | 'item', field: string, idx?: number): boolean {
    if (!Array.isArray(flags) || !flags.length) return false;
    return flags.some((f: any) => {
      if (scope === 'decl' && (f.scope === 'declaration' || !f.item_index)) {
        return f.field === field;
      }
      if (scope === 'item') return f.scope === 'item' && f.item_index === idx && f.field === field;
      return false;
    });
  }
  function declBorder(field: string): string {
    const cur = workingDecl[field];
    const orig = originalDecl[field];
    if (cur != null && orig != null && String(cur) !== String(orig)) return '#3b82f6'; // blue (edited)
    if (cur === '' || cur == null) return '#eab308'; // yellow (empty)
    if (isFlagged('decl', field)) return '#eab308';
    return '#22c55e'; // green
  }
  function declBg(field: string): string {
    const cur = workingDecl[field];
    if (cur === '' || cur == null || isFlagged('decl', field)) return '#fefce8';
    return '#ffffff';
  }
  function itemBorder(idx: number, field: string): string {
    const cur = workingItems[idx]?.[field];
    const orig = originalItems[idx]?.[field];
    if (cur != null && orig != null && String(cur) !== String(orig)) return '#3b82f6';
    if (cur === '' || cur == null) return '#eab308';
    if (isFlagged('item', field, idx)) return '#eab308';
    return '#22c55e';
  }

  // ── Action handlers ──
  async function saveDraft() {
    try {
      const r = await fetch(`/api/review/${jobId}/draft`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ declaration: workingDecl, items: workingItems }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      unsavedDirty = false;
      toast('Draft saved');
    } catch {
      toast('Draft save failed', 'error');
    }
  }

  function discardClicked() {
    if (unsavedDirty || editLog.length > 0) {
      if (!confirm('Unsaved edits — really discard?')) return;
    }
    onClose?.();
  }

  function approveClicked() {
    // Confidence-aware confirm: count fields below 90%
    const conf = job?.confidence || {};
    let lowConfCount = 0;
    if (conf?.fields && typeof conf.fields === 'object') {
      for (const v of Object.values<any>(conf.fields)) {
        if (typeof v === 'number' && v < 0.9) lowConfCount++;
      }
    }
    if (lowConfCount > 0) {
      showApproveConfirm = true;
      return;
    }
    doApprove();
  }
  async function doApprove() {
    try {
      const r = await fetch(`/api/review/${jobId}/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ notes: approveNotes || undefined }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      toast('Approved');
      showApproveConfirm = false;
      onApprove?.();
    } catch {
      toast('Approve failed', 'error');
    }
  }

  async function doReject() {
    if (!rejectNotes.trim()) {
      toast('Reason required', 'error');
      return;
    }
    try {
      const r = await fetch(`/api/review/${jobId}/reject`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ notes: rejectNotes }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      toast('Rejected');
      showRejectModal = false;
      onReject?.();
    } catch {
      toast('Reject failed', 'error');
    }
  }

  // ── Declaration field rows definition ──
  const declRows = [
    { field: 'declaration_no', label: 'DECL_NO' },
    { field: 'declaration_date', label: 'DATE' },
    { field: 'importer_name', label: 'IMPORTER' },
    { field: 'consignor_name', label: 'CONSIGNOR' },
    { field: 'invoice_number', label: 'INVOICE_NO' },
    { field: 'invoice_price', label: 'INVOICE_PRICE' },
    { field: 'currency', label: 'CURRENCY' },
    { field: 'exchange_rate', label: 'EX_RATE' },
    { field: 'total_customs_value', label: 'CUSTOMS_VAL' },
    { field: 'import_export_customs_duty', label: 'DUTY (CD)' },
    { field: 'commercial_tax_ct', label: 'TAX (CT)' },
    { field: 'advance_income_tax_at', label: 'INCOME_TAX (AT)' },
    { field: 'security_fee_sf', label: 'SECURITY (SF)' },
    { field: 'maccs_service_fee_mf', label: 'MACCS (MF)' },
    { field: 'exemption_reduction', label: 'EXEMPTION' },
  ];

  const itemCols = [
    { field: 'hs_code', label: 'HS', w: 'w-32' },
    { field: 'item_name', label: 'NAME', w: '' },
    { field: 'quantity', label: 'QTY', w: 'w-24' },
    { field: 'invoice_unit_price', label: 'INV_PRICE', w: 'w-24' },
    { field: 'cif_unit_price', label: 'CIF_PRICE', w: 'w-24' },
  ];

  // ── Mobile tab ──
  let mobileTab = $state<'pdf' | 'data'>('pdf');
</script>

<Toast message={toastMsg} type={toastType} visible={toastVisible} />

<!-- ═══ STATUS BAR ═══ -->
<div class="border-2 stamp-shadow mb-2"
  style="border-color: var(--on-surface); background: var(--surface);">
  <div class="flex flex-wrap items-center gap-3 px-3 py-2">
    <span class="px-2 py-0.5 text-[10px] font-black uppercase tracking-wider"
      style="background: #b45309; color: white;">
      ⚠ {status}
    </span>
    <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">
      {filename}
    </span>
    <span class="text-[10px] font-mono" style="color: var(--outline);">
      {totalPages} pgs · ${cost.toFixed(2)} · edits={editCount} · flags={flagCount}
    </span>
    <div class="flex-1"></div>
    <div class="flex flex-wrap gap-2">
      <button
        class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
        style="border-color: var(--on-surface); color: var(--on-surface); background: var(--surface);"
        onclick={discardClicked}
      >
        ↩ DISCARD
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
        style="border-color: var(--on-surface); color: var(--on-surface); background: var(--surface);"
        onclick={saveDraft}
      >
        💾 DRAFT
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
        style="border-color: var(--on-surface); background: #ef4444; color: white;"
        onclick={() => { showRejectModal = true; }}
      >
        ✗ REJECT
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
        style="border-color: var(--on-surface); background: #16a34a; color: white;"
        onclick={approveClicked}
      >
        ✓ APPROVE
      </button>
    </div>
  </div>
</div>

<!-- Mobile tab toggle -->
<div class="flex md:hidden gap-2 mb-2">
  <button class="flex-1 px-2 py-1 text-[10px] font-black uppercase border-2"
    style="border-color: var(--on-surface); background: {mobileTab === 'pdf' ? 'var(--on-surface)' : 'var(--surface)'}; color: {mobileTab === 'pdf' ? 'var(--surface)' : 'var(--on-surface)'};"
    onclick={() => mobileTab = 'pdf'}>PDF</button>
  <button class="flex-1 px-2 py-1 text-[10px] font-black uppercase border-2"
    style="border-color: var(--on-surface); background: {mobileTab === 'data' ? 'var(--on-surface)' : 'var(--surface)'}; color: {mobileTab === 'data' ? 'var(--surface)' : 'var(--on-surface)'};"
    onclick={() => mobileTab = 'data'}>DATA</button>
</div>

<!-- ═══ SPLIT GRID ═══ -->
<div class="review-grid">
  <!-- LEFT: PDF -->
  <div class="border-2 flex flex-col {mobileTab === 'data' ? 'hidden md:flex' : 'flex'}"
    style="border-color: var(--on-surface); background: white;">
    <div class="dark-bar flex justify-between items-center text-xs">
      <span>PDF_VIEWER</span>
      <span class="text-[10px]">page {currentPage}/{totalPages}</span>
    </div>

    <!-- page strip -->
    <div class="px-2 py-1 flex items-center gap-1 overflow-x-auto custom-scrollbar"
      style="background: var(--surface-container); border-bottom: 1px solid rgba(56,56,50,0.15);">
      <button class="px-2 py-1 text-[10px] font-bold border cursor-pointer"
        style="border-color: var(--on-surface); background: var(--surface);"
        disabled={currentPage <= 1}
        onclick={() => jumpToPage(Math.max(1, currentPage - 1))}>◀</button>
      <div class="flex gap-1 flex-1 overflow-x-auto custom-scrollbar">
        {#each pageList as p, i}
          {@const num = p?.page_number ?? (i + 1)}
          <button
            class="px-1.5 py-0.5 text-[8px] font-mono font-black uppercase border cursor-pointer whitespace-nowrap"
            style="border-color: var(--on-surface); background: {currentPage === num ? 'var(--on-surface)' : 'var(--surface)'}; color: {currentPage === num ? 'var(--surface)' : 'var(--on-surface)'};"
            onclick={() => jumpToPage(num)}
          >
            {num}·{pageBadge(p)}
          </button>
        {/each}
      </div>
      <button class="px-2 py-1 text-[10px] font-bold border cursor-pointer"
        style="border-color: var(--on-surface); background: var(--surface);"
        disabled={currentPage >= totalPages}
        onclick={() => jumpToPage(Math.min(totalPages, currentPage + 1))}>▶</button>
    </div>

    <div class="flex-1 min-h-[400px]">
      {#if pdfLoading}
        <div class="flex items-center justify-center h-full">
          <span class="text-xs font-bold uppercase" style="color: var(--outline);">LOADING PDF...</span>
        </div>
      {:else if pdfError}
        <div class="flex items-center justify-center h-full">
          <span class="text-xs font-bold uppercase" style="color: var(--tertiary);">{pdfError}</span>
        </div>
      {:else}
        <iframe
          src={pdfSrc}
          title="PDF"
          style="width: 100%; height: 100%; border: none; min-height: calc(100vh - 280px);"
        ></iframe>
      {/if}
    </div>
  </div>

  <!-- RIGHT: DATA -->
  <div class="flex flex-col gap-2 {mobileTab === 'pdf' ? 'hidden md:flex' : 'flex'}" style="min-width: 0;">
    <!-- Declaration -->
    <div class="border-2 stamp-shadow"
      style="border-color: var(--on-surface); background: var(--surface);">
      <div class="dark-bar text-xs">DECLARATION</div>
      <div class="bg-white p-2 grid grid-cols-1 sm:grid-cols-2 gap-2">
        {#each declRows as row}
          {@const editing = editingKey === `decl:${row.field}`}
          <div class="border-2 p-2"
            style="border-color: {declBorder(row.field)}; background: {declBg(row.field)};">
            <div class="flex items-center justify-between">
              <div class="text-[8px] font-black uppercase tracking-wider"
                style="color: var(--outline);">{row.label}</div>
              {#if !editing}
                <button class="text-[10px] cursor-pointer"
                  onclick={() => startEdit(`decl:${row.field}`, workingDecl[row.field])}>✏</button>
              {/if}
            </div>
            {#if editing}
              <input
                class="w-full mt-1 text-[11px] font-mono font-bold border px-1 py-0.5"
                style="border-color: var(--on-surface); background: white;"
                bind:value={editValue}
                autofocus
                onkeydown={(e) => {
                  if (e.key === 'Enter') saveDeclField(row.field);
                  else if (e.key === 'Escape') cancelEdit();
                }}
              />
              <div class="flex items-center gap-1 mt-1">
                <button class="px-2 py-0.5 text-[9px] font-black uppercase border cursor-pointer"
                  style="border-color: var(--on-surface); background: #16a34a; color: white;"
                  onclick={() => saveDeclField(row.field)}>✓ SAVE</button>
                <button class="px-2 py-0.5 text-[9px] font-black uppercase border cursor-pointer"
                  style="border-color: var(--on-surface); background: var(--surface);"
                  onclick={cancelEdit}>✗ CANCEL</button>
                <span class="text-[8px] font-mono ml-auto" style="color: var(--outline);">
                  orig: {String(originalDecl[row.field] ?? '—')}
                </span>
              </div>
            {:else}
              <button
                class="text-left w-full mt-0.5 text-[11px] font-mono font-bold cursor-pointer"
                style="color: var(--on-surface);"
                onclick={() => startEdit(`decl:${row.field}`, workingDecl[row.field])}
              >
                {workingDecl[row.field] || '—'}
              </button>
            {/if}
          </div>
        {/each}
      </div>
    </div>

    <!-- Items -->
    <div class="border-2 stamp-shadow"
      style="border-color: var(--on-surface); background: var(--surface);">
      <div class="dark-bar flex items-center justify-between text-xs">
        <span>ITEMS ({workingItems.length})</span>
        <button class="px-2 py-0.5 text-[9px] font-black uppercase border cursor-pointer"
          style="border-color: var(--surface); color: var(--surface); background: transparent;"
          onclick={addItemRow}>+ ADD</button>
      </div>
      <div class="bg-white overflow-x-auto custom-scrollbar">
        <table class="w-full text-[10px] font-mono">
          <thead>
            <tr style="background: var(--surface-container);">
              <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b"
                style="border-color: var(--on-surface);">#</th>
              {#each itemCols as col}
                <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b {col.w}"
                  style="border-color: var(--on-surface);">{col.label}</th>
              {/each}
              <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b w-8"
                style="border-color: var(--on-surface);">✓</th>
            </tr>
          </thead>
          <tbody>
            {#each workingItems as item, idx}
              <tr class="border-b" style="border-color: rgba(56,56,50,0.1);">
                <td class="px-2 py-1 font-bold">{idx + 1}</td>
                {#each itemCols as col}
                  {@const ekey = `item:${idx}:${col.field}`}
                  {@const editing = editingKey === ekey}
                  <td class="px-1 py-1">
                    <div class="border-2 px-1 py-0.5"
                      style="border-color: {itemBorder(idx, col.field)}; background: {item[col.field] === '' || item[col.field] == null ? '#fefce8' : 'white'};">
                      {#if editing}
                        <input
                          class="w-full text-[10px] font-mono font-bold border px-1"
                          style="border-color: var(--on-surface);"
                          bind:value={editValue}
                          autofocus
                          onkeydown={(e) => {
                            if (e.key === 'Enter') saveItemField(idx, col.field);
                            else if (e.key === 'Escape') cancelEdit();
                          }}
                          onblur={() => saveItemField(idx, col.field)}
                        />
                      {:else}
                        <button
                          class="text-left w-full cursor-pointer truncate font-bold"
                          onclick={() => startEdit(ekey, item[col.field])}
                          title={String(item[col.field] ?? '')}
                        >
                          ✏ {item[col.field] || '—'}
                        </button>
                      {/if}
                    </div>
                  </td>
                {/each}
                <td class="px-2 py-1 text-center" style="color: #16a34a;">✓</td>
              </tr>
            {/each}
            {#if workingItems.length === 0}
              <tr><td colspan="7" class="px-2 py-4 text-center text-[10px] font-bold uppercase"
                style="color: var(--outline);">NO ITEMS</td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Edit log -->
    <div class="border-2"
      style="border-color: var(--on-surface); background: var(--surface);">
      <button
        class="dark-bar flex items-center justify-between text-xs w-full cursor-pointer"
        onclick={() => editLogCollapsed = !editLogCollapsed}
      >
        <span>EDIT_LOG ({editLog.length} edits this session)</span>
        <span>{editLogCollapsed ? '▸' : '▾'}</span>
      </button>
      {#if !editLogCollapsed}
        <div class="bg-white p-2 max-h-48 overflow-y-auto custom-scrollbar">
          {#if editLog.length === 0}
            <div class="text-[10px] font-mono uppercase" style="color: var(--outline);">
              No edits yet. Click ✏ on any field to start editing.
            </div>
          {:else}
            {#each editLog as e}
              <div class="text-[10px] font-mono py-0.5 flex gap-2">
                <span style="color: var(--outline);">{e.ts}</span>
                <span class="font-black" style="color: var(--on-surface);">{e.field}</span>
                <span style="color: var(--outline);">{e.before} → </span>
                <span class="font-bold" style="color: #16a34a;">{e.after}</span>
                <span class="ml-auto" style="color: var(--outline);">{e.user}</span>
              </div>
            {/each}
          {/if}
        </div>
      {/if}
    </div>
  </div>
</div>

<!-- Reject modal -->
{#if showRejectModal}
  <div class="fixed inset-0 z-50 flex items-center justify-center"
    style="background: rgba(0,0,0,0.5);">
    <div class="border-2 stamp-shadow w-full max-w-md mx-4"
      style="border-color: var(--on-surface); background: var(--surface);">
      <div class="dark-bar text-xs">REJECT_DOCUMENT</div>
      <div class="bg-white p-4">
        <div class="text-[10px] font-black uppercase mb-2" style="color: var(--on-surface);">
          REASON FOR REJECTION (required)
        </div>
        <textarea
          class="w-full text-[11px] font-mono border-2 p-2"
          style="border-color: var(--on-surface); min-height: 80px;"
          bind:value={rejectNotes}
          placeholder="e.g. Wrong document type — not a customs declaration"
        ></textarea>
        <div class="flex gap-2 mt-3 justify-end">
          <button class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
            style="border-color: var(--on-surface); background: var(--surface); color: var(--on-surface);"
            onclick={() => showRejectModal = false}>CANCEL</button>
          <button class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
            style="border-color: var(--on-surface); background: #ef4444; color: white;"
            onclick={doReject}>✗ CONFIRM REJECT</button>
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- Approve confirm modal (low confidence) -->
{#if showApproveConfirm}
  <div class="fixed inset-0 z-50 flex items-center justify-center"
    style="background: rgba(0,0,0,0.5);">
    <div class="border-2 stamp-shadow w-full max-w-md mx-4"
      style="border-color: var(--on-surface); background: var(--surface);">
      <div class="dark-bar text-xs">CONFIRM_APPROVE</div>
      <div class="bg-white p-4">
        <div class="text-[11px] font-bold uppercase mb-2" style="color: #b45309;">
          ⚠ Some fields below 90% confidence — confirm approval?
        </div>
        <textarea
          class="w-full text-[11px] font-mono border-2 p-2 mt-2"
          style="border-color: var(--on-surface); min-height: 60px;"
          bind:value={approveNotes}
          placeholder="Optional notes"
        ></textarea>
        <div class="flex gap-2 mt-3 justify-end">
          <button class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
            style="border-color: var(--on-surface); background: var(--surface); color: var(--on-surface);"
            onclick={() => showApproveConfirm = false}>CANCEL</button>
          <button class="px-3 py-1.5 text-[10px] font-black uppercase border-2 cursor-pointer"
            style="border-color: var(--on-surface); background: #16a34a; color: white;"
            onclick={doApprove}>✓ APPROVE ANYWAY</button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .review-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0.5rem;
  }
  @media (min-width: 768px) {
    .review-grid {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }
  }
</style>
