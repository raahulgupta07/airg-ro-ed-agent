<script lang="ts">
  import { auth } from '$lib/stores/auth.svelte';
  import Toast from './Toast.svelte';
  import ExcelTable from './ExcelTable.svelte';

  type Col = {
    id: string;
    header: string;
    accessor?: string;
    width?: number;
    frozen?: boolean;
    cell?: (row: any) => any;
    align?: 'left' | 'right' | 'center';
    enableSort?: boolean;
  };

  const ITEM_COLUMNS: Col[] = [
    { id: 'item_name', header: 'ITEM NAME', accessor: 'item_name', width: 240 },
    { id: 'hs_code', header: 'HS', accessor: 'hs_code', width: 110 },
    { id: 'quantity', header: 'QTY', accessor: 'quantity', width: 90 },
    { id: 'invoice_unit_price', header: 'UNIT PRICE', accessor: 'invoice_unit_price', width: 90, align: 'right' },
    { id: 'cif_unit_price', header: 'CIF PRICE', accessor: 'cif_unit_price', width: 90, align: 'right' },
    { id: 'currency', header: 'CUR', accessor: 'currency', width: 60 },
    { id: 'customs_duty_rate', header: 'DUTY%', accessor: 'customs_duty_rate', width: 70, align: 'right' },
    { id: 'commercial_tax_percent', header: 'TAX%', accessor: 'commercial_tax_percent', width: 70, align: 'right' },
    { id: 'exchange_rate', header: 'FX', accessor: 'exchange_rate', width: 80, align: 'right' },
    { id: 'origin_country', header: 'ORIGIN', accessor: 'origin_country', width: 80 },
    { id: 'customs_value_mmk', header: 'VALUE (MMK)', accessor: 'customs_value_mmk', width: 130, align: 'right' },
  ];

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
  type LogEntry = { ts: string; field: string; before: string; after: string; user: string; page_ref?: number };
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

  // ── Per-field page reference ──
  // Backend may provide job.declaration_field_pages: { [field]: pageNo }
  // Or job.field_pages. Items may have it.page_ref. Defaults: decl page 1, items idx+1.
  const declFieldPages: Record<string, number> = (
    job?.declaration_field_pages ||
    job?.field_pages?.declaration ||
    {}
  );
  function declPageRef(field: string): number {
    const p = declFieldPages?.[field];
    if (typeof p === 'number' && p > 0) return p;
    if (typeof job?.decl_page_no === 'number') return job.decl_page_no;
    return 1;
  }
  function itemPageRef(idx: number, field?: string): number {
    const it = workingItems[idx] || {};
    if (typeof it.page_ref === 'number' && it.page_ref > 0) return it.page_ref;
    if (it.field_pages && field && typeof it.field_pages[field] === 'number') return it.field_pages[field];
    // Heuristic: each item often on its own page; declaration may be on page 1.
    // Best guess: page = idx + 1, capped to totalPages.
    const guess = idx + 1;
    if (totalPages && guess > totalPages) return totalPages;
    return guess || 1;
  }

  // Debounced PDF jump (200ms) to avoid rapid iframe reloads on hover.
  let _jumpTimer: ReturnType<typeof setTimeout> | null = null;
  function jumpPdf(page: number) {
    if (!page || page < 1) return;
    if (_jumpTimer) clearTimeout(_jumpTimer);
    _jumpTimer = setTimeout(() => {
      if (page !== currentPage) currentPage = page;
    }, 200);
  }
  function jumpPdfImmediate(page: number) {
    if (!page || page < 1) return;
    if (_jumpTimer) { clearTimeout(_jumpTimer); _jumpTimer = null; }
    currentPage = page;
  }

  function jumpToPage(n: number) {
    jumpPdfImmediate(n);
  }
  const pdfSrc = $derived(pdfBlobUrl ? `${pdfBlobUrl}#page=${currentPage}&zoom=page-fit` : '');

  // ── Edits-by-page (red dot on page strip) ──
  const editsByPage = $derived.by(() => {
    const m: Record<number, number> = {};
    for (const e of editLog) {
      const p = (e as any).page_ref;
      if (typeof p === 'number' && p > 0) m[p] = (m[p] || 0) + 1;
    }
    return m;
  });

  // ── Jump-to-field dropdown ──
  let showFieldJump = $state(false);
  const fieldJumpList = $derived.by(() => {
    const list: Array<{ id: string; label: string; page: number }> = [];
    for (const r of declRows) {
      list.push({
        id: `field-decl-${r.field}`,
        label: r.label,
        page: declPageRef(r.field),
      });
    }
    for (let i = 0; i < workingItems.length; i++) {
      for (const c of itemCols) {
        list.push({
          id: `field-item-${i}-${c.field}`,
          label: `Item ${i + 1}: ${c.label}`,
          page: itemPageRef(i, c.field),
        });
      }
    }
    list.sort((a, b) => a.page - b.page);
    return list;
  });
  function jumpToField(id: string, page: number) {
    jumpPdfImmediate(page);
    showFieldJump = false;
    // Defer to next frame so any layout settles before scroll.
    setTimeout(() => {
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 50);
  }

  // ── Status / counters ──
  const status: string = job?.review_status || job?.status || 'PENDING_REVIEW';
  const filename: string = job?.pdf_name || job?.filename || 'document.pdf';
  const cost: number = job?.cost_usd || 0;
  const accuracy: number = job?.accuracy_percent || 0;
  const decision: string = job?.gate_decision || job?.decision || (accuracy >= 90 ? 'ACCEPTED' : 'ESCALATED');
  const duration: number = job?.processing_time_seconds || job?.duration || 0;
  const itemsCount = $derived(workingItems.length);
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
        page_ref: declPageRef(field),
      }];
      unsavedDirty = true;
    } catch (e) {
      // revert
      workingDecl = { ...workingDecl, [field]: before };
      toast('Save failed', 'error');
    }
  }

  async function saveItemField(idx: number, field: string, valueOverride?: any) {
    const item = workingItems[idx] || {};
    const before = item[field];
    const after = valueOverride !== undefined ? valueOverride : editValue;
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
        page_ref: itemPageRef(idx, field),
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

  // ── Add new item row (persists to backend) ──
  async function addItemRow() {
    const newItem = {
      item_name: '',
      hs_code: '',
      quantity: '',
      invoice_unit_price: '',
      cif_unit_price: '',
      currency: workingDecl.currency || '',
    };
    try {
      const r = await fetch(`/api/review/${jobId}/items`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ item: newItem }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const created = j?.item || newItem;
      workingItems = [...workingItems, { ...newItem, ...created }];
      originalItems.push({ ...newItem, ...created });
      editLog = [...editLog, {
        ts: new Date().toISOString().slice(11, 19),
        field: `ITEM[${workingItems.length}].ADDED`,
        before: '—',
        after: 'new row',
        user: auth?.user?.username || 'admin',
      }];
      unsavedDirty = true;
      toast('Row added');
    } catch {
      toast('Add failed', 'error');
    }
  }

  // ── Delete item row (soft delete on backend) ──
  async function deleteItemRow(idx: number) {
    if (!confirm(`Delete item ${idx + 1}? This can be undone by re-adding manually.`)) return;
    const removed = workingItems[idx];
    try {
      const r = await fetch(`/api/review/${jobId}/items/${idx}`, {
        method: 'DELETE',
        credentials: 'include',
        headers: {
          'Authorization': `Bearer ${auth.token}`,
        },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      // Optimistic local removal
      workingItems = workingItems.filter((_, i) => i !== idx);
      originalItems.splice(idx, 1);
      editLog = [...editLog, {
        ts: new Date().toISOString().slice(11, 19),
        field: `ITEM[${idx + 1}].DELETED`,
        before: String(removed?.item_name ?? '—'),
        after: '—',
        user: auth?.user?.username || 'admin',
      }];
      unsavedDirty = true;
      toast('Row deleted');
    } catch {
      toast('Delete failed', 'error');
    }
  }

  // ── Move row up / down (reorder via backend) ──
  async function moveItem(idx: number, dir: -1 | 1) {
    const target = idx + dir;
    if (target < 0 || target >= workingItems.length) return;
    // Optimistic local swap
    const newItems = [...workingItems];
    [newItems[idx], newItems[target]] = [newItems[target], newItems[idx]];
    const prevItems = workingItems;
    workingItems = newItems;

    // Build full order array — start as identity, then swap
    const order = workingItems.map((_, i) => i);
    // Map current idx in newItems → its old position in prevItems
    // After swap: newItems[target] = old item at idx; newItems[idx] = old item at target
    // So order should be: positions filled by old indexes
    const finalOrder = newItems.map((it) => prevItems.indexOf(it));

    try {
      const r = await fetch(`/api/review/${jobId}/items/reorder`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${auth.token}`,
        },
        body: JSON.stringify({ order: finalOrder }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      editLog = [...editLog, {
        ts: new Date().toISOString().slice(11, 19),
        field: `ITEM[${idx + 1}].MOVED`,
        before: `pos ${idx + 1}`,
        after: `pos ${target + 1}`,
        user: auth?.user?.username || 'admin',
      }];
      unsavedDirty = true;
    } catch {
      // revert
      workingItems = prevItems;
      toast('Reorder failed', 'error');
    }
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

  // ── ExcelTable items bridge ──
  // Annotate items with _index so save handlers can target the right index.
  const itemsForTable = $derived(workingItems.map((it, i) => ({ ...it, _index: i })));

  function isItemCellEdited(idx: number, field: string): boolean {
    const cur = workingItems[idx]?.[field];
    const orig = originalItems[idx]?.[field];
    if (cur == null || orig == null) return false;
    return String(cur) !== String(orig);
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
      {totalPages} pg · ${cost.toFixed(3)} · ITEMS:{itemsCount} · ACC:{accuracy.toFixed(1)}% · DEC:{decision} · TIME:{duration ? duration.toFixed(0) + 's' : '—'} · EDITS:{editCount} · FLAGS:{flagCount}
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
      <div class="flex items-center gap-2 relative">
        <button
          class="px-2 py-0.5 text-[9px] font-black uppercase border cursor-pointer"
          style="border-color: var(--surface); color: var(--surface); background: transparent;"
          onclick={() => showFieldJump = !showFieldJump}
          title="Jump to a field"
        >🔍 JUMP TO FIELD ▾</button>
        <span class="text-[10px]">page {currentPage}/{totalPages}</span>
        {#if showFieldJump}
          <div class="absolute right-0 top-full mt-1 z-30 border-2 stamp-shadow max-h-72 overflow-y-auto custom-scrollbar"
            style="border-color: var(--on-surface); background: var(--surface); min-width: 220px;">
            {#each fieldJumpList as f}
              <button
                class="block w-full text-left px-2 py-1 text-[10px] font-mono font-bold cursor-pointer hover:opacity-80"
                style="border-bottom: 1px solid rgba(56,56,50,0.1); color: var(--on-surface);"
                onclick={() => jumpToField(f.id, f.page)}
              >
                <span style="color: var(--outline);">p{f.page}</span> — {f.label}
              </button>
            {/each}
            {#if fieldJumpList.length === 0}
              <div class="px-2 py-2 text-[10px] font-mono uppercase" style="color: var(--outline);">No fields</div>
            {/if}
          </div>
        {/if}
      </div>
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
          {@const editsHere = editsByPage[num] || 0}
          <button
            class="relative px-1.5 py-0.5 text-[8px] font-mono font-black uppercase border cursor-pointer whitespace-nowrap"
            style="border-color: var(--on-surface); background: {currentPage === num ? 'var(--on-surface)' : 'var(--surface)'}; color: {currentPage === num ? 'var(--surface)' : 'var(--on-surface)'};"
            onclick={() => jumpToPage(num)}
            title={editsHere > 0 ? `${editsHere} edit${editsHere === 1 ? '' : 's'} on this page` : ''}
          >
            {num}·{pageBadge(p)}
            {#if editsHere > 0}
              <span
                class="absolute"
                style="top: -3px; right: -3px; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; border: 1px solid var(--surface);"
              ></span>
            {/if}
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
          {@const dPage = declPageRef(row.field)}
          <div
            id={`field-decl-${row.field}`}
            class="border-2 p-2"
            style="border-color: {declBorder(row.field)}; background: {declBg(row.field)};"
            onmouseenter={() => jumpPdf(dPage)}
            onfocusin={() => jumpPdf(dPage)}
            role="group"
          >
            <div class="flex items-center justify-between gap-1">
              <div class="text-[8px] font-black uppercase tracking-wider"
                style="color: var(--outline);">{row.label}</div>
              <div class="flex items-center gap-1">
                <button
                  class="text-[9px] font-mono font-bold px-1 cursor-pointer border"
                  style="border-color: var(--outline); color: var(--on-surface); background: transparent;"
                  title="Jump PDF to page {dPage}"
                  onclick={() => jumpPdfImmediate(dPage)}
                >📍 p{dPage}</button>
                {#if !editing}
                  <button class="text-[10px] cursor-pointer"
                    title="Edit"
                    onclick={() => startEdit(`decl:${row.field}`, workingDecl[row.field])}>✏</button>
                {/if}
              </div>
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
    <ExcelTable
      title="PRODUCT_ITEMS"
      columns={ITEM_COLUMNS}
      data={itemsForTable}
      editable={true}
      enableRowActions={true}
      enableAddRow={true}
      isCellEdited={(row, col) => isItemCellEdited(row._index, col.id)}
      onCellEdit={(row, col, val) => saveItemField(row._index, col.id, val)}
      onPageJump={(p) => jumpPdfImmediate(p)}
      pageRefAccessor={(row) => itemPageRef(row._index, 'item_name')}
      onRowMoveUp={(idx) => moveItem(idx, -1)}
      onRowMoveDown={(idx) => moveItem(idx, 1)}
      onRowDelete={(idx) => deleteItemRow(idx)}
      onAddRow={addItemRow}
      exportFilename="items.csv"
      maxHeight="400px"
    />
    <!-- Legacy items table (replaced by ExcelTable above) -->
    {#if false}
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
              <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b w-12"
                style="border-color: var(--on-surface);">⇅</th>
              <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b w-8"
                style="border-color: var(--on-surface);">✓</th>
              <th class="px-2 py-1 text-left text-[9px] font-black uppercase border-b w-8"
                style="border-color: var(--on-surface);">🗑</th>
            </tr>
          </thead>
          <tbody>
            {#each workingItems as item, idx}
              <tr class="border-b" style="border-color: rgba(56,56,50,0.1);">
                <td class="px-2 py-1 font-bold">{idx + 1}</td>
                {#each itemCols as col}
                  {@const ekey = `item:${idx}:${col.field}`}
                  {@const editing = editingKey === ekey}
                  {@const iPage = itemPageRef(idx, col.field)}
                  <td class="px-1 py-1">
                    <div
                      id={`field-item-${idx}-${col.field}`}
                      class="border-2 px-1 py-0.5"
                      style="border-color: {itemBorder(idx, col.field)}; background: {item[col.field] === '' || item[col.field] == null ? '#fefce8' : 'white'};"
                      onmouseenter={() => jumpPdf(iPage)}
                      onfocusin={() => jumpPdf(iPage)}
                      role="group"
                    >
                      <div class="flex items-center justify-end gap-1" style="margin-bottom: 1px;">
                        <button
                          class="text-[8px] font-mono font-bold px-0.5 cursor-pointer"
                          style="color: var(--outline); background: transparent; border: none;"
                          title="Jump PDF to page {iPage}"
                          onclick={(e) => { e.stopPropagation(); jumpPdfImmediate(iPage); }}
                        >📍p{iPage}</button>
                      </div>
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
                <td class="px-1 py-1 text-center whitespace-nowrap">
                  <button
                    class="px-1 text-[10px] font-bold cursor-pointer disabled:opacity-30"
                    title="Move up"
                    disabled={idx === 0}
                    onclick={() => moveItem(idx, -1)}
                  >▲</button>
                  <button
                    class="px-1 text-[10px] font-bold cursor-pointer disabled:opacity-30"
                    title="Move down"
                    disabled={idx === workingItems.length - 1}
                    onclick={() => moveItem(idx, 1)}
                  >▼</button>
                </td>
                <td class="px-2 py-1 text-center" style="color: #16a34a;">✓</td>
                <td class="px-1 py-1 text-center">
                  <button
                    class="px-1 text-[12px] cursor-pointer"
                    title="Delete row"
                    style="color: #ef4444;"
                    onclick={() => deleteItemRow(idx)}
                  >🗑</button>
                </td>
              </tr>
            {/each}
            {#if workingItems.length === 0}
              <tr><td colspan="9" class="px-2 py-4 text-center text-[10px] font-bold uppercase"
                style="color: var(--outline);">NO ITEMS</td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {/if}

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
