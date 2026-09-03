<script lang="ts">
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';
  import Toast from './Toast.svelte';
  import ExcelTable from './ExcelTable.svelte';
  import EvidenceCard from './EvidenceCard.svelte';

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
    { id: '_serial', header: '#', cell: (row) => row._serial, width: 40, align: 'right', enableSort: false },
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
    slim = false,
  }: {
    jobId: string;
    job: any;
    onApprove?: () => void;
    onReject?: () => void;
    onClose?: () => void;
    slim?: boolean;
  } = $props();

  // ── Issues — machine-readable "what's wrong + why + fix" from the backend ──
  type Issue = { code: string; title?: string; severity: 'error' | 'warn' | 'info'; field?: string; detail: string; cause: string; fix: string };
  let issues = $state<Issue[]>([]);
  let issuesOpen = $state(true);
  $effect(() => {
    const id = jobId;
    if (!id) { issues = []; return; }
    (async () => {
      try {
        const r = await fetch(`/api/review/${id}`, { headers: { 'Authorization': `Bearer ${auth.token}` } });
        if (!r.ok) return;
        const d = JSON.parse(await r.text());
        issues = d.issues ?? [];
      } catch { /* panel is optional — never block review */ }
    })();
  });
  const sevColor = (s: string) => s === 'error' ? 'var(--error)' : s === 'warn' ? 'var(--warning)' : 'var(--info)';

  // ── Values the reader was unsure about (same cards as the Checks tab) ──
  // Loaded separately from issues: issues describe the DOCUMENT ("products do
  // not add up"), these describe ONE FIELD and can be settled in place.
  let evidenceChecks = $state<any[]>([]);
  let evidenceOpen = $state(true);
  $effect(() => {
    const id = jobId;
    if (!id) { evidenceChecks = []; return; }
    (async () => {
      try {
        const r = await api.evidenceForJob(id);
        evidenceChecks = r.checks ?? [];
      } catch { /* optional panel — a missing count must never block review */ }
    })();
  });
  function onEvidenceResolved(field: string, value: any) {
    evidenceChecks = evidenceChecks.filter(c => c.field !== field);
    // Mirror the settled value into the working copy so the declaration table
    // below does not keep showing the number the reviewer just replaced.
    if (field in workingDecl) workingDecl = { ...workingDecl, [field]: value };
  }

  // ── Local working copy (optimistic edits) ──
  // Accept both shapes: V11 response has `declaration` (singular).
  // /api/jobs/{id} (history endpoint) returns `declarations: [decl]` array.
  function _readDecl(j: any): Record<string, any> {
    return (j?.declaration ?? j?.declarations?.[0]) ?? {};
  }
  let workingDecl = $state<Record<string, any>>({ ..._readDecl(job) });
  let workingItems = $state<any[]>(Array.isArray(job?.items) ? job.items.map((i: any) => ({ ...i })) : []);

  // Track originals so we can detect user-edited fields (border-blue)
  let originalDecl = $state<Record<string, any>>({ ..._readDecl(job) });
  let originalItems = $state<any[]>(Array.isArray(job?.items) ? job.items.map((i: any) => ({ ...i })) : []);

  // Item review flags from job (yellow border on flagged fields)
  let flags = $state<any[]>(Array.isArray(job?.item_review_flags) ? job.item_review_flags : []);

  // Reactively sync when parent passes a different job (e.g. /history async load)
  let _lastSig = $state<string>('');
  $effect(() => {
    // Read job fields explicitly so Svelte tracks them as deps
    const j = job as any;
    const jid = j?.job_id ?? '';
    const declCount = j?.declarations?.length ?? (j?.declaration ? 1 : 0);
    const itemsCount = j?.items?.length ?? 0;
    const sig = `${jid}|${declCount}|${itemsCount}`;
    if (!jid) return;
    if (sig === _lastSig) return;
    _lastSig = sig;
    const d = _readDecl(j);
    workingDecl = { ...d };
    originalDecl = { ...d };
    const it = Array.isArray(j?.items) ? j.items.map((i: any) => ({ ...i })) : [];
    workingItems = it;
    originalItems = it.map((i: any) => ({ ...i }));
    flags = Array.isArray(j?.item_review_flags) ? j.item_review_flags : [];
  });

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

  // Tab from a declaration cell: save it, then open the next field's editor
  // (wraps at the end). Lets a reviewer keyboard down the whole declaration.
  function tabDeclField(field: string) {
    const idx = declRows.findIndex((r) => r.field === field);
    saveDeclField(field);
    if (idx < 0) return;
    const next = declRows[idx + 1];
    if (!next) return;
    setTimeout(() => startEdit(`decl:${next.field}`, workingDecl[next.field]), 0);
  }

  // ── Reject / approve modals ──
  let showRejectModal = $state(false);
  let rejectNotes = $state('');
  let showApproveConfirm = $state(false);
  let approveNotes = $state('');
  let approving = $state(false);
  let rejecting = $state(false);
  // Pre-mark from existing job.review_status so re-load doesn't allow double-approve.
  const _initialStatus = (job as any)?.review_status || '';
  let approved = $state(_initialStatus === 'approved');
  let rejected = $state(_initialStatus === 'rejected');

  // ── PDF blob loading (auth-safe iframe) ──
  let pdfBlobUrl = $state<string>('');
  let pdfLoading = $state(true);
  let pdfError = $state('');
  let pdfFallback = $state(false);  // blob fetch failed → using token-query URL

  $effect(() => {
    let cancelled = false;
    let revokeUrl: string | null = null;
    (async () => {
      pdfLoading = true;
      pdfError = '';
      pdfFallback = false;
      try {
        // NOTE: the route requires `token` as a QUERY param (native EventSource-era
        // signature) — a header-only fetch 422s and forced the fallback banner on
        // every job. Send both.
        const r = await fetch(`/api/jobs/${jobId}/pdf?token=${encodeURIComponent(auth.token ?? '')}`, {
          headers: { 'Authorization': `Bearer ${auth.token}` },
          credentials: 'include',
        });
        if (!r.ok) throw new Error(`pdf ${r.status}`);
        if (cancelled) return;
        // Use the direct URL (not a blob) so the browser PDF viewer's download
        // button keeps the server's filename: <decl_no>_<job_id>.pdf.
        pdfBlobUrl = `/api/jobs/${jobId}/pdf?token=${encodeURIComponent(auth.token ?? '')}`;
      } catch (e: any) {
        // Fallback: token-querystring URL — surface it so a blank viewer
        // isn't mistaken for a successful load.
        if (!cancelled) {
          console.warn('PDF blob fetch failed, using token-query fallback', e);
          pdfBlobUrl = `/api/jobs/${jobId}/pdf?token=${auth.token}`;
          pdfFallback = true;
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

  // Warn before leaving with unsaved review edits (backend already persists
  // each cell on PATCH, but the in-session edit log / draft state would be lost).
  $effect(() => {
    if (typeof window === 'undefined') return;
    const handler = (e: BeforeUnloadEvent) => {
      if ((unsavedDirty || editLog.length > 0) && !approved && !rejected) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
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
  // 0 means "we do not know", and the caller must not turn that into a page.
  //
  // This returned 1 as a last resort, so every field without a measured box
  // claimed to be on page 1. On a bundled release order whose declaration sits
  // on page 10 that is not a harmless default: it sends a reviewer to the wrong
  // document to confirm a customs figure, and it looks deliberate because it is
  // rendered identically to a real one. Neither `declFieldPages` nor
  // `decl_page_no` is served by the API either, so page 1 was the ONLY answer
  // this ever gave.
  function declPageRef(field: string): number {
    const p = declFieldPages?.[field];
    if (typeof p === 'number' && p > 0) return p;
    if (typeof job?.decl_page_no === 'number') return job.decl_page_no;
    return 0;
  }
  function itemPageRef(idx: number, field?: string): number {
    // A measured box first — it is the only source here that actually looked at
    // the page.
    const bb = itemBbox(idx, field || 'item_name')
            || itemBbox(idx, 'item_name') || itemBbox(idx, 'hs_code');
    if (bb?.page) return bb.page;
    const it = workingItems[idx] || {};
    if (typeof it.page_ref === 'number' && it.page_ref > 0) return it.page_ref;
    if (it.field_pages && field && typeof it.field_pages[field] === 'number') return it.field_pages[field];
    // The old fallback was `page = idx + 1`, i.e. item 3 is on page 3. That is
    // true of nothing: on these bundles every item sits in one block on the
    // declaration's second sheet, so a 7-item document sent the reviewer to
    // seven different pages, none of them right. 0 means "not located" and the
    // caller renders no chip rather than a confident wrong number.
    return 0;
  }

  // Debounced PDF jump (200ms) to avoid rapid iframe reloads on hover.
  let _jumpTimer: ReturnType<typeof setTimeout> | null = null;
  let pdfSearchTerm = $state<string>('');
  function jumpPdf(page: number, searchVal?: string) {
    if (!page || page < 1) return;
    if (_jumpTimer) clearTimeout(_jumpTimer);
    _jumpTimer = setTimeout(() => {
      if (page !== currentPage) currentPage = page;
      if (searchVal !== undefined) pdfSearchTerm = String(searchVal || '').slice(0, 80);
    }, 200);
  }
  function jumpPdfImmediate(page: number, searchVal?: string) {
    if (!page || page < 1) return;
    if (_jumpTimer) { clearTimeout(_jumpTimer); _jumpTimer = null; }
    currentPage = page;
    if (searchVal !== undefined) pdfSearchTerm = String(searchVal || '').slice(0, 80);
    // In the page column the pages are all mounted, so a jump is a scroll. The
    // observer sets `currentPage` again when the scroll lands; assigning it
    // above keeps the strip correct for the iframe view and while smooth
    // scrolling is still in flight.
    if (viewer === 'page') scrollToPage(page);
  }

  function jumpToPage(n: number) {
    jumpPdfImmediate(n);
  }

  // Field bboxes from backend: { declaration: {field: {page,x,y,w,h}}, items: {idx: {field: {...}}} }
  const fieldBboxes = $derived.by(() => (job as any)?.field_bboxes || {});

  // ── Marked PDF ─────────────────────────────────────────────────────────
  // The original file with every located value highlighted on it. It has been
  // buildable since the coordinates existed, but nothing in the UI linked to
  // it — the only way to see one was to type the URL by hand, so in practice a
  // run never produced one.
  //
  // The count is the SERVER's, not a count of the boxes in this payload. Those
  // two are not the same number: a stored box whose value is gone is not a
  // mark, and this job carries several — the engines emit `customs_duty` and
  // `security_fee`, the declarations table stores `import_export_customs_duty`
  // and `security_fee_sf`, and both spellings get located. Counting boxes here
  // said 28 where the file contains 21. A button that promises 28 highlights
  // and delivers 21 is worse than one that says nothing.
  let markCount = $state(0);
  let noMarksReason = $state('');
  $effect(() => {
    if (!jobId) return;
    let cancelled = false;
    api.markedPdfStatus(jobId)
      .then((s) => {
        if (cancelled) return;
        markCount = s?.marks ?? 0;
        noMarksReason = s?.reason || '';
      })
      .catch(() => { if (!cancelled) { markCount = 0; noMarksReason = ''; } });
    return () => { cancelled = true; };
  });
  // Never offer a download that cannot exist. A photographed declaration has no
  // text layer, so no position was ever measured — the button stays visible and
  // disabled with the reason, because hiding it entirely reads as the feature
  // being missing on this job for no stated cause.
  const markedPdfHref = $derived(markCount > 0 ? api.markedPdfUrl(jobId) : '');
  const markedPdfTitle = $derived(
    markCount > 0
      ? `Open the PDF with all ${markCount} extracted values highlighted on it`
      : (noMarksReason || 'No positions could be measured on this document, so nothing can be marked'));
  function declBbox(field: string) {
    return fieldBboxes?.declaration?.[field] || null;
  }
  function itemBbox(idx: number, field: string) {
    return fieldBboxes?.items?.[String(idx)]?.[field] || null;
  }

  // Zoom is REMEMBERED, not re-asserted.
  //
  // The viewer URL used to hardcode `zoom=page-fit`, and every declaration cell
  // re-writes that URL on mouseenter to jump the page. So zooming in and then
  // moving the mouse snapped the document straight back to fit — it looked like
  // zoom was disabled when it was actually being reset several times a second.
  // Holding the level in state means a jump preserves whatever the reviewer set.
  const PDF_ZOOM_STEPS = ['page-fit', 'page-width', '100', '150', '200', '300', '400'];
  let pdfZoom = $state<string>('page-fit');
  function zoomIn() {
    const i = PDF_ZOOM_STEPS.indexOf(pdfZoom);
    pdfZoom = PDF_ZOOM_STEPS[Math.min(i + 1, PDF_ZOOM_STEPS.length - 1)];
  }
  function zoomOut() {
    const i = PDF_ZOOM_STEPS.indexOf(pdfZoom);
    pdfZoom = PDF_ZOOM_STEPS[Math.max(i - 1, 0)];
  }
  const zoomLabel = $derived(
    pdfZoom === 'page-fit' ? 'FIT' : pdfZoom === 'page-width' ? 'WIDTH' : `${pdfZoom}%`);

  // ── Page view (default) vs the full PDF ────────────────────────────────
  // `page` renders one page as an image and is what page jumps and field hovers
  // drive. `pdf` is the browser's own viewer, kept for reading, text selection,
  // printing and download — the things it is genuinely better at.
  let viewer = $state<'page' | 'pdf'>('page');

  // Server picks the format: JPEG for a photographed page, PNG where there is a
  // text layer. 150 DPI is readable and keeps a scanned page around 300 KB —
  // the old fixed render was ~7 MB, which is not something to fetch on hover.
  function pageImgSrc(n: number) {
    if (!jobId || !n) return '';
    const t = encodeURIComponent(auth.token || '');
    return `/api/jobs/${jobId}/page-image/${n}?token=${t}&dpi=150`;
  }

  // ── The page column ────────────────────────────────────────────────────
  // Every page is rendered, stacked in one scrolling column, because a bundle
  // is read as a document: the CUSDEC continuation sheet holding the item block
  // is the page after the header, and paging one image at a time made the
  // commonest comparison in review a click-and-wait.
  //
  // `currentPage` is now DERIVED from the scroll position rather than driving
  // which image is mounted. It still drives the iframe (`pdfSrc`) and the page
  // strip.
  let scrollEl = $state<HTMLElement | null>(null);
  const pageEls: Record<number, HTMLElement> = {};

  function scrollToPage(n: number) {
    const el = pageEls[n];
    if (!el) return;
    el.scrollIntoView({
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      block: 'start',
    });
  }

  // The page you are looking at is the topmost one still crossing the upper
  // band of the column — NOT the most visible one, which flips between two
  // numbers while a page boundary sits mid-column.
  $effect(() => {
    const root = scrollEl;
    // `pageEls` is a plain object, so it is not reactive — read the page COUNT
    // so this re-runs when the pages arrive (the job loads after mount) or the
    // viewer flips back from the iframe. Without it the observer would watch
    // whatever existed on first run, which is nothing.
    const n = pageList.length;
    if (!root || viewer !== 'page' || !n) return;
    const els = Object.values(pageEls).filter(Boolean);
    if (!els.length) return;
    const io = new IntersectionObserver((entries) => {
      let best: IntersectionObserverEntry | null = null;
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        if (!best || e.boundingClientRect.top < best.boundingClientRect.top) best = e;
      }
      if (!best) return;
      const n = Number((best.target as HTMLElement).dataset.page);
      if (n && n !== currentPage) currentPage = n;
    }, { root, rootMargin: '-8% 0px -70% 0px', threshold: 0 });
    for (const el of els) io.observe(el);
    return () => io.disconnect();
  });

  // ── Highlight boxes ────────────────────────────────────────────────────
  // The coordinates have been computed, stored and served for a long time and
  // nothing ever drew them — `declBbox()` was used only to read `.page`. They
  // could not be drawn while the viewer was an iframe: you cannot position an
  // element over a browser PDF plugin. The page image can.
  //
  // Stored boxes are in PDF points (72 per inch). The image is rendered at
  // PAGE_IMG_DPI, so one point is dpi/72 image pixels, and dividing by the
  // image's natural width turns that into a percentage — which survives zoom,
  // window resizing and the browser's own scaling without any further work.
  const PAGE_IMG_DPI = 150;
  // One natural size PER PAGE. A bundle mixes A4 text pages with photographed
  // sheets of another size, so a single shared measurement would place boxes on
  // one page using another page's dimensions.
  let imgNaturals = $state<Record<number, { w: number; h: number }>>({});
  // Why a page could not be drawn, per page. The commonest cause is a job whose
  // `pdf_path` points somewhere the container no longer has — the render 404s
  // with "PDF not found" and the reviewer is owed that sentence, not a broken
  // image icon.
  let pageErrors = $state<Record<number, string>>({});
  let activeField = $state<string>('');

  function pctBox(bb: any, page: number) {
    const nat = imgNaturals[page];
    if (!bb || !nat?.w || !nat?.h) return null;
    const k = PAGE_IMG_DPI / 72;
    return {
      left: (bb.x * k) / nat.w * 100,
      top: (bb.y * k) / nat.h * 100,
      width: (bb.w * k) / nat.w * 100,
      height: (bb.h * k) / nat.h * 100,
    };
  }

  // Every box on the page currently shown, so a reviewer can see at a glance
  // which figures on this sheet the pipeline actually located.
  function boxesForPage(pageNo: number) {
    const out: { field: string; label: string; box: any }[] = [];
    const decl = fieldBboxes?.declaration || {};
    for (const [field, bb] of Object.entries<any>(decl)) {
      if (bb?.page !== pageNo) continue;
      const b = pctBox(bb, pageNo);
      if (b) out.push({ field, label: field, box: b });
    }
    // Item rows too. These were computed and stored alongside the declaration
    // boxes from the start and were just as unused. On these forms the item
    // block often sits on the declaration's SECOND sheet, so an item box and a
    // header box frequently land on different pages of the same document.
    const items = fieldBboxes?.items || {};
    for (const [idx, row] of Object.entries<any>(items)) {
      for (const [field, bb] of Object.entries<any>(row || {})) {
        if (bb?.page !== pageNo) continue;
        const b = pctBox(bb, pageNo);
        if (b) out.push({ field: `item:${idx}:${field}`,
                          label: `Item ${Number(idx) + 1} · ${field}`, box: b });
      }
    }
    return out;
  }

  // The same zoom control drives both viewers, so switching between them does
  // not silently change the size of the page.
  const imgWidthStyle = $derived.by(() => {
    if (pdfZoom === 'page-fit') return 'max-width: 100%; height: auto;';
    if (pdfZoom === 'page-width') return 'width: 100%; height: auto;';
    return `width: ${pdfZoom}%; max-width: none; height: auto;`;
  });

  const pdfSrc = $derived.by(() => {
    if (!pdfBlobUrl) return '';
    const search = pdfSearchTerm ? `&search=${encodeURIComponent(pdfSearchTerm)}` : '';
    return `${pdfBlobUrl}#page=${currentPage}&zoom=${pdfZoom}${search}`;
  });

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

  // Last deleted item — drives the inline "Undo" affordance for a few seconds.
  let lastDeleted = $state<any | null>(null);
  let _undoTimer: ReturnType<typeof setTimeout> | null = null;

  async function undoDelete() {
    if (!lastDeleted) return;
    const restore = { ...lastDeleted };
    lastDeleted = null;
    if (_undoTimer) { clearTimeout(_undoTimer); _undoTimer = null; }
    try {
      const r = await fetch(`/api/review/${jobId}/items`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${auth.token}` },
        body: JSON.stringify({ item: restore }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const created = j?.item || restore;
      workingItems = [...workingItems, { ...restore, ...created }];
      originalItems.push({ ...restore, ...created });
      unsavedDirty = true;
      toast('Delete undone — row re-added at end');
    } catch {
      toast('Undo failed', 'error');
    }
  }

  // ── Delete item row (soft delete on backend) ──
  async function deleteItemRow(idx: number) {
    if (!confirm(`Delete item ${idx + 1}?`)) return;
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
      // Offer undo for 8s (re-adds the soft-deleted row's values).
      lastDeleted = { ...removed };
      if (_undoTimer) clearTimeout(_undoTimer);
      _undoTimer = setTimeout(() => { lastDeleted = null; }, 8000);
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
    if (cur != null && orig != null && String(cur) !== String(orig)) return 'var(--info)'; // edited
    if (cur === '' || cur == null) return 'var(--warning)'; // empty
    if (isFlagged('decl', field)) return 'var(--warning)';
    return 'var(--success)';
  }
  function declBg(field: string): string {
    const cur = workingDecl[field];
    if (cur === '' || cur == null || isFlagged('decl', field)) return 'var(--warning-soft)';
    return '#ffffff';
  }
  function itemBorder(idx: number, field: string): string {
    const cur = workingItems[idx]?.[field];
    const orig = originalItems[idx]?.[field];
    if (cur != null && orig != null && String(cur) !== String(orig)) return 'var(--info)';
    if (cur === '' || cur == null) return 'var(--warning)';
    if (isFlagged('item', field, idx)) return 'var(--warning)';
    return 'var(--success)';
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
    if (approving || approved) return;  // guard against double-click / already approved
    approving = true;
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
      approved = true;
      toast('✓ APPROVED — saved to HISTORY');
      showApproveConfirm = false;
      onApprove?.();
      // Auto-navigate to history page after 1.5s so user sees the saved job
      setTimeout(() => {
        try {
          window.location.href = `/history?job=${encodeURIComponent(jobId)}`;
        } catch { /* ignore */ }
      }, 1500);
    } catch {
      toast('Approve failed', 'error');
      approving = false;  // allow retry on real error
    }
  }

  async function doReject() {
    if (rejecting || rejected || approved) return;
    if (!rejectNotes.trim()) {
      toast('Reason required', 'error');
      return;
    }
    rejecting = true;
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
      rejected = true;
      toast('Rejected');
      showRejectModal = false;
      onReject?.();
    } catch {
      toast('Reject failed', 'error');
      rejecting = false;
    }
  }

  // ── ExcelTable items bridge ──
  // Annotate items with _index so save handlers can target the right index.
  // Also inject a 1-based serial (#) and fall back currency to the declaration
  // currency (currency is a declaration-level field; items share it).
  const itemsForTable = $derived(workingItems.map((it, i) => ({
    ...it,
    _index: i,
    _serial: i + 1,
    currency: it.currency || workingDecl.currency || workingDecl.currency_2 || '',
  })));

  // Totals strip — validate extracted items against the declared customs total.
  const declaredTotal = $derived(Number(workingDecl.total_customs_value) || 0);
  const itemsValueSum = $derived(
    workingItems.reduce((s, it) => s + (Number(it.customs_value_mmk) || 0), 0)
  );
  const valueGap = $derived(declaredTotal ? declaredTotal - itemsValueSum : 0);
  const valueBalanced = $derived(
    declaredTotal > 0 && Math.abs(valueGap) / declaredTotal <= 0.05
  );

  function isItemCellEdited(idx: number, field: string): boolean {
    const cur = workingItems[idx]?.[field];
    const orig = originalItems[idx]?.[field];
    if (cur == null || orig == null) return false;
    return String(cur) !== String(orig);
  }

  // ── Declaration field rows definition ──
  const declRows = [
    { field: 'declaration_no', label: 'DECL_NO' },
    { field: 'declaration_date', label: 'DECL_DATE' },
    { field: 'release_order_date', label: 'RO_DATE' },
    { field: 'arrival_date', label: 'ARRIVAL_DATE' },
    { field: 'completion_date', label: 'COMPLETION_DATE' },
    { field: 'importer_name', label: 'IMPORTER' },
    { field: 'consignor_name', label: 'CONSIGNOR' },
    { field: 'invoice_number', label: 'INVOICE_NO' },
    { field: 'invoice_price_fc', label: 'INVOICE_PRICE (FC)' },
    { field: 'invoice_price', label: 'INVOICE_PRICE (MMK)' },
    { field: 'freight_value', label: 'FREIGHT' },
    { field: 'insurance_value', label: 'INSURANCE' },
    { field: 'adjustment_value', label: 'ADJUSTMENT' },
    { field: 'currency', label: 'CURRENCY' },
    { field: 'exchange_rate', label: 'EX_RATE' },
    { field: 'total_customs_value', label: 'CUSTOMS_VAL' },
    { field: 'import_export_customs_duty', label: 'DUTY (CD)' },
    { field: 'commercial_tax_ct', label: 'TAX (CT)' },
    { field: 'advance_income_tax_at', label: 'INCOME_TAX (AT)' },
    { field: 'security_fee_sf', label: 'SECURITY (SF)' },
    { field: 'maccs_service_fee_mf', label: 'MACCS (MF)' },
    { field: 'exemption_reduction', label: 'EXEMPTION' },
    { field: 'document_format', label: 'DOC_FORMAT' },
  ];

  // Pair the declaration rows into 2-up groups for the table layout (auto-height).
  const declPairs = (() => {
    const out: any[] = [];
    for (let i = 0; i < declRows.length; i += 2) out.push([declRows[i], declRows[i + 1] ?? null]);
    return out;
  })();

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
  style="border-color: var(--line); background: var(--surface);">
  <div class="flex flex-wrap items-center gap-3 px-3 py-2">
    <span class="px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider"
      style="background: var(--warning); color: white;">
      ⚠ {status}
    </span>
    {#if !slim}
      <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">
        {filename}
      </span>
    {/if}
    <span class="text-[10px] font-mono" style="color: var(--outline);">
      {#if slim}
        DEC:{decision} · EDITS:{editCount} · FLAGS:{flagCount}
      {:else}
        <span class="job-id" title={jobId}>JOB:{jobId}</span> · {totalPages} pg · ${cost.toFixed(3)} · ITEMS:{itemsCount} · ACC:{accuracy.toFixed(1)}% · DEC:{decision} · TIME:{duration ? duration.toFixed(0) + 's' : '—'} · TOK:{(((job as any)?.tokens_in ?? 0)/1000).toFixed(1)}k/{(((job as any)?.tokens_out ?? 0)/1000).toFixed(1)}k · {(job as any)?.model_used || 'Atlas V14'} · EDITS:{editCount} · FLAGS:{flagCount}
      {/if}
    </span>
    <div class="flex-1"></div>
    <div class="flex flex-wrap gap-2">
      <button
        class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
        style="border-color: var(--line); background: var(--primary-container); color: var(--on-surface); box-shadow: var(--shadow-sm);"
        onclick={() => {
          try {
            localStorage.removeItem('ro_ed_agent_queue');
            localStorage.removeItem('ro_ed_agent_sel');
          } catch { /* ignore */ }
          try { window.location.href = '/agent'; } catch { /* ignore */ }
        }}
        title="Start a new extraction job"
      >
        ↻ NEW JOB
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
        style="border-color: var(--line); color: var(--on-surface); background: var(--surface);"
        onclick={discardClicked}
      >
        ↩ DISCARD
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
        style="border-color: var(--line); color: var(--on-surface); background: var(--surface);"
        onclick={saveDraft}
      >
        💾 DRAFT
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-medium uppercase border-2"
        aria-label="Reject document"
        style="border-color: var(--line); background: {(approved || rejected) ? 'var(--outline-variant)' : 'var(--error)'}; color: white; cursor: {(approved || rejected || rejecting) ? 'not-allowed' : 'pointer'}; opacity: {(approved || rejected) ? 0.5 : 1};"
        disabled={approved || rejected || rejecting || approving}
        onclick={() => { if (!approved && !rejected) showRejectModal = true; }}
      >
        ✗ REJECT
      </button>
      <button
        class="px-3 py-1.5 text-[10px] font-medium uppercase border-2"
        aria-label="Approve document"
        style="border-color: var(--line); background: {(approved || rejected) ? 'var(--outline-variant)' : 'var(--success)'}; color: white; cursor: {(approved || approving || rejected) ? 'not-allowed' : 'pointer'}; opacity: {(approved || rejected) ? 0.5 : 1};"
        disabled={approving || approved || rejected || rejecting}
        onclick={approveClicked}
      >
        {approving ? '… APPROVING' : approved ? '✓ APPROVED' : '✓ APPROVE'}
      </button>
    </div>
  </div>
</div>

<!-- Mobile tab toggle -->
<div class="flex md:hidden gap-2 mb-2">
  <button class="flex-1 px-2 py-1 text-[10px] font-medium uppercase border-2"
    style="border-color: var(--line); background: {mobileTab === 'pdf' ? 'var(--on-surface)' : 'var(--surface)'}; color: {mobileTab === 'pdf' ? 'var(--surface)' : 'var(--on-surface)'};"
    onclick={() => mobileTab = 'pdf'}>PDF</button>
  <button class="flex-1 px-2 py-1 text-[10px] font-medium uppercase border-2"
    style="border-color: var(--line); background: {mobileTab === 'data' ? 'var(--on-surface)' : 'var(--surface)'}; color: {mobileTab === 'data' ? 'var(--surface)' : 'var(--on-surface)'};"
    onclick={() => mobileTab = 'data'}>DATA</button>
</div>

<!-- ═══ SPLIT GRID ═══ -->
<div class="review-grid">
  <!-- LEFT: PDF -->
  <div class="border-2 flex flex-col {mobileTab === 'data' ? 'hidden md:flex' : 'flex'}"
    style="border-color: var(--line); background: white;">
    <div class="dark-bar flex justify-between items-center text-xs">
      <span>PDF_VIEWER</span>
      <div class="flex items-center gap-2 relative">
        <button
          class="px-2 py-0.5 text-[9px] font-medium uppercase border cursor-pointer"
          style="border-color: var(--surface); color: var(--surface); background: transparent;"
          onclick={() => (viewer = viewer === 'page' ? 'pdf' : 'page')}
          title={viewer === 'page'
            ? 'Open the full PDF — text selection, print, download'
            : 'Back to the page view, where page jumps actually move'}
        >{viewer === 'page' ? 'FULL PDF' : 'PAGE VIEW'}</button>
        {#if markCount > 0}
          <a
            class="px-2 py-0.5 text-[9px] font-medium uppercase border cursor-pointer no-underline"
            style="border-color: var(--surface); color: var(--surface); background: transparent;"
            href={markedPdfHref} target="_blank" rel="noopener"
            title={markedPdfTitle}
          >MARKED PDF ({markCount})</a>
        {:else}
          <span
            class="px-2 py-0.5 text-[9px] font-medium uppercase border cursor-not-allowed"
            style="border-color: var(--surface); color: var(--surface); background: transparent; opacity: 0.45;"
            title={markedPdfTitle}
          >MARKED PDF —</span>
        {/if}
        <div class="flex items-center gap-1">
          <button
            class="px-1.5 py-0.5 text-[10px] font-bold border cursor-pointer"
            style="border-color: var(--surface); color: var(--surface); background: transparent;"
            onclick={zoomOut} disabled={pdfZoom === PDF_ZOOM_STEPS[0]}
            title="Zoom out"
          >−</button>
          <span class="text-[9px] font-mono w-10 text-center" title="Kept while you move between fields">{zoomLabel}</span>
          <button
            class="px-1.5 py-0.5 text-[10px] font-bold border cursor-pointer"
            style="border-color: var(--surface); color: var(--surface); background: transparent;"
            onclick={zoomIn} disabled={pdfZoom === PDF_ZOOM_STEPS[PDF_ZOOM_STEPS.length - 1]}
            title="Zoom in"
          >+</button>
        </div>
        <button
          class="px-2 py-0.5 text-[9px] font-medium uppercase border cursor-pointer"
          style="border-color: var(--surface); color: var(--surface); background: transparent;"
          onclick={() => showFieldJump = !showFieldJump}
          title="Jump to a field"
        >🔍 JUMP TO FIELD ▾</button>
        <span class="text-[10px]">page {currentPage}/{totalPages}</span>
        {#if showFieldJump}
          <div class="absolute right-0 top-full mt-1 z-30 border-2 stamp-shadow max-h-72 overflow-y-auto custom-scrollbar"
            style="border-color: var(--line); background: var(--surface); min-width: 220px;">
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
        style="border-color: var(--line); background: var(--surface);"
        disabled={currentPage <= 1}
        onclick={() => jumpToPage(Math.max(1, currentPage - 1))}>◀</button>
      <div class="flex gap-1 flex-1 overflow-x-auto custom-scrollbar">
        {#each pageList as p, i}
          {@const num = p?.page_number ?? (i + 1)}
          {@const editsHere = editsByPage[num] || 0}
          <button
            class="relative px-1.5 py-0.5 text-[8px] font-mono font-medium uppercase border cursor-pointer whitespace-nowrap"
            style="border-color: var(--line); background: {currentPage === num ? 'var(--on-surface)' : 'var(--surface)'}; color: {currentPage === num ? 'var(--surface)' : 'var(--on-surface)'};"
            onclick={() => jumpToPage(num)}
            title={editsHere > 0 ? `${editsHere} edit${editsHere === 1 ? '' : 's'} on this page` : ''}
          >
            {num}·{pageBadge(p)}
            {#if editsHere > 0}
              <span
                class="absolute"
                style="top: -3px; right: -3px; width: 8px; height: 8px; border-radius: 50%; background: var(--error); border: 1px solid var(--surface);"
              ></span>
            {/if}
          </button>
        {/each}
      </div>
      <button class="px-2 py-1 text-[10px] font-bold border cursor-pointer"
        style="border-color: var(--line); background: var(--surface);"
        disabled={currentPage >= totalPages}
        onclick={() => jumpToPage(Math.min(totalPages, currentPage + 1))}>▶</button>
    </div>

    <!-- Why there are no highlights, stated once, where the highlights would be.
         A reviewer who sees an unmarked page and a greyed-out button otherwise
         has to guess whether the run failed. It did not: the document is a
         photograph and this is the permanent, correct outcome for it. -->
    {#if markCount === 0 && noMarksReason}
      <div class="px-2 py-1 text-[10px] font-mono"
        style="background: var(--surface-container); color: var(--on-surface-muted); border-bottom: 1px solid rgba(56,56,50,0.15);">
        NO MARKS — {noMarksReason}
      </div>
    {/if}

    <!-- `text-align: center` and not `margin: auto`: the page wrapper is
         inline-block so it shrinks to the image's rendered size, and auto
         margins do not centre an inline-block. It sat hard left, which on a
         scrolled column reads as an empty pane. -->
    <div class="flex-1 min-h-[400px] overflow-auto custom-scrollbar"
      bind:this={scrollEl}
      style="background: var(--surface-container-low); text-align: center;">
      {#if viewer === 'page'}
        <!-- Page image. The reason this is the default: a browser PDF viewer
             reads `#page=` ONCE, when it loads. Rewriting the fragment on an
             already-loaded iframe changes nothing, so every page click and every
             field hover was silently ignored — the strip said page 10 while the
             document sat on page 3. An <img> has no such problem: change the
             src, the page changes. -->
        {#if jobId && totalPages > 0}
          {#each pageList as p, i (p?.page_number ?? i + 1)}
            {@const num = p?.page_number ?? (i + 1)}
            <!-- The wrapper is inline-block so it shrinks to the image's own
                 rendered size. Percentage-positioned boxes then line up at any
                 zoom without recomputing anything. -->
            <!-- `width: fit-content` + `margin: auto`, NOT inline-block: the
                 column is `text-align: center`, so inline-block pages flow into
                 each other and wrap side by side — which is exactly what a
                 failed image render looks like, since the alt text is narrow.
                 Block-level with a shrink-to-fit width centres the sheet AND
                 keeps one page per row. -->
            <div
              class="rv-page {currentPage === num ? 'on' : ''}"
              data-page={num}
              bind:this={pageEls[num]}
              style="position: relative; display: block; width: fit-content; margin: 0 auto 10px;"
            >
              {#if pageErrors[num]}
                <!-- A page that will not render says so, at the size a page
                     would have been. The browser's own broken-image alt text is
                     a few narrow words, so a failed render used to collapse the
                     column into a run of text and read as a layout bug rather
                     than a missing file. -->
                <div class="rv-page-missing">
                  <span>PAGE {num} UNAVAILABLE</span>
                  <span class="rv-page-missing-why">{pageErrors[num]}</span>
                </div>
              {:else}
                <img
                  src={pageImgSrc(num)}
                  alt="Page {num} of {totalPages}"
                  loading="lazy"
                  decoding="async"
                  style="display: block; {imgWidthStyle}"
                  onload={(e) => {
                    const t = e.currentTarget as HTMLImageElement;
                    imgNaturals = { ...imgNaturals, [num]: { w: t.naturalWidth, h: t.naturalHeight } };
                  }}
                  onerror={() => {
                    pageErrors = { ...pageErrors, [num]: 'The source PDF is not where this job recorded it.' };
                  }}
                />
              {/if}
              <span class="rv-pageno">{num}</span>
              {#each boxesForPage(num) as b (b.field)}
                <!-- Padded outward by a hair: `search_for` returns a tight glyph
                     box, and a rectangle drawn exactly on the ink reads as a
                     strikethrough rather than a highlight. -->
                <div
                  class="rv-box {activeField === b.field ? 'on' : ''}"
                  style="left: calc({b.box.left}% - 2px); top: calc({b.box.top}% - 2px);
                         width: calc({b.box.width}% + 4px); height: calc({b.box.height}% + 4px);"
                  title={b.label}
                ></div>
              {/each}
            </div>
          {/each}
        {:else}
          <div class="flex items-center justify-center h-full">
            <span class="text-xs font-bold uppercase" style="color: var(--outline);">NO PAGE</span>
          </div>
        {/if}
      {:else if pdfLoading}
        <div class="flex items-center justify-center h-full">
          <span class="text-xs font-bold uppercase" style="color: var(--outline);">LOADING PDF...</span>
        </div>
      {:else if pdfError}
        <div class="flex items-center justify-center h-full">
          <span class="text-xs font-bold uppercase" style="color: var(--tertiary);">{pdfError}</span>
        </div>
      {:else}
        {#if pdfFallback}
          <div class="px-2 py-1 text-[10px] font-mono uppercase tracking-wider"
            style="background: var(--warning-soft); color: var(--warning); border-bottom: 1px solid var(--line);">
            ⚠ Secure load failed — showing fallback view. If blank, reload the page.
          </div>
        {/if}
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
    <!-- Issues — why fields below are blank/wrong (mirrors the Excel "Issues" sheet) -->
    {#if issues.length > 0}
      <div class="border-2 stamp-shadow" style="border-color: var(--line); background: var(--surface);">
        <button type="button" class="dark-bar text-xs flex justify-between w-full cursor-pointer" style="border: none;"
                onclick={() => (issuesOpen = !issuesOpen)}>
          <span>⚠ CHECK THESE ({issues.length})</span>
          <span style="color: var(--on-surface-muted);">
            {issues.filter(i => i.severity === 'error').length} must fix · {issues.filter(i => i.severity === 'warn').length} look at {issuesOpen ? '▾' : '▸'}
          </span>
        </button>
        {#if issuesOpen}
          <div class="overflow-x-auto" style="background: var(--surface-container-lowest);">
            <table class="w-full text-[11px] font-mono">
              <tbody>
                {#each issues as iss}
                  <tr style="border-bottom: 1px solid var(--line-2);">
                    <td class="px-2 py-2 align-top" style="color: {sevColor(iss.severity)}; font-weight: 700; max-width: 190px; white-space: normal;">
                      {iss.severity === 'error' ? '✗' : iss.severity === 'warn' ? '⚠' : 'ℹ'} {iss.title ?? iss.code}
                    </td>
                    <td class="px-2 py-2 align-top" style="color: var(--on-surface); white-space: normal;">
                      {iss.detail}
                      <div style="color: var(--on-surface-muted);">Why: {iss.cause}.</div>
                      <div style="color: var(--success);">What to do: {iss.fix}.</div>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Uncertain values — the same cards as the Checks tab, for the reviewer who
         is already in the document and should not have to leave it to settle one
         number. Absent entirely when the engine recorded no evidence (older jobs,
         other engines), which is why this is gated on length rather than shown
         empty: "no evidence recorded" must never read as "nothing is wrong". -->
    {#if evidenceChecks.length > 0}
      <div class="border-2 stamp-shadow" style="border-color: var(--line); background: var(--surface);">
        <button type="button" class="dark-bar text-xs flex justify-between w-full cursor-pointer"
                style="border: none;" onclick={() => (evidenceOpen = !evidenceOpen)}>
          <span>◎ VALUES TO CONFIRM ({evidenceChecks.length})</span>
          <span style="color: var(--on-surface-muted);">{evidenceOpen ? '▾' : '▸'}</span>
        </button>
        {#if evidenceOpen}
          <div class="p-2" style="background: var(--surface-container-lowest);">
            {#each evidenceChecks as check (check.field)}
              <EvidenceCard jobId={job.job_id} {check} compact
                            onresolved={onEvidenceResolved} />
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Declaration -->
    <div class="border-2 stamp-shadow"
      style="border-color: var(--line); background: var(--surface);">
      <div class="dark-bar text-xs flex justify-between"><span>DECLARATION</span>
        <span style="color: var(--on-surface-muted);">{declRows.length} FIELDS</span></div>
      <div class="bg-white overflow-x-auto">
        <table class="w-full text-[11px] font-mono">
          <thead>
            <tr style="background: var(--surface-container);">
              {#each ['FIELD','VALUE','PG','✓','FIELD','VALUE','PG','✓'] as h}
                <th class="px-2 py-1 text-left text-[9px] font-medium uppercase border-b"
                  style="border-color: var(--outline); color: var(--on-surface-muted);">{h}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each declPairs as pair}
              <tr>
                {@render declCell(pair[0])}
                {#if pair[1]}{@render declCell(pair[1])}{:else}<td colspan="4"></td>{/if}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>

    {#snippet declCell(row)}
      {@const editing = editingKey === `decl:${row.field}`}
      {@const _bb = declBbox(row.field)}
      {@const dPage = (_bb?.page) || declPageRef(row.field)}
      {@const _val = workingDecl[row.field]}
      {@const _searchVal = String(_val ?? '')}
      <td class="px-2 py-1 text-[9px] uppercase tracking-wider align-top whitespace-nowrap border-b"
        style="color: var(--on-surface-muted); border-color: var(--outline);">{row.label}</td>
      <td class="px-2 py-1 align-top border-b"
        style="background: {declBg(row.field)}; border-color: var(--outline);"
        onmouseenter={() => { activeField = row.field; if (dPage > 0) jumpPdf(dPage, _searchVal); }}
        onmouseleave={() => { if (activeField === row.field) activeField = ''; }}
        role="cell">
        {#if editing}
          <input
            class="w-full text-[11px] font-mono font-bold border px-1 py-0.5"
            style="border-color: var(--line); background: white;"
            bind:value={editValue} autofocus
            onkeydown={(e) => { if (e.key === 'Enter') saveDeclField(row.field); else if (e.key === 'Escape') cancelEdit(); else if (e.key === 'Tab') { e.preventDefault(); tabDeclField(row.field); } }} />
          <div class="flex items-center gap-1 mt-1">
            <button class="px-1.5 py-0.5 text-[8px] font-medium uppercase border cursor-pointer"
              style="border-color: var(--line); background: var(--success); color: white;"
              onclick={() => saveDeclField(row.field)}>✓</button>
            <button class="px-1.5 py-0.5 text-[8px] font-medium uppercase border cursor-pointer"
              style="border-color: var(--line); background: var(--surface);"
              onclick={cancelEdit}>✗</button>
          </div>
        {:else}
          <button class="text-left w-full text-[11px] font-mono font-bold cursor-pointer"
            style="color: {declBorder(row.field) === 'var(--on-surface)' ? 'var(--on-surface)' : declBorder(row.field)};"
            onclick={() => startEdit(`decl:${row.field}`, _val)}>{_val || '—'}</button>
        {/if}
      </td>
      <td class="px-1 py-1 align-top border-b" style="border-color: var(--outline);">
        {#if dPage > 0}
          <button class="text-[9px] font-mono px-1 cursor-pointer border whitespace-nowrap"
            style="border-color: var(--primary); color: var(--primary);"
            title="Jump to page {dPage} — where this value was found"
            onclick={() => jumpPdfImmediate(dPage)}>p{dPage}</button>
        {:else}
          <!-- Not located. Say so, rather than printing a page number that
               happens to be 1. A reviewer can tell "we could not find this on
               the page" from "it is on page 1"; a fabricated chip cannot. -->
          <span class="text-[9px] font-mono px-1 whitespace-nowrap"
            style="color: var(--on-surface-subtle);"
            title="Not located on the page — the stored value could not be matched to any text">–</span>
        {/if}
      </td>
      <td class="px-1 py-1 align-top text-center border-b" style="border-color: var(--outline);">
        {#if _val}<span style="color: var(--success);">✓</span>{:else}<span style="color: var(--outline);">—</span>{/if}
      </td>
    {/snippet}

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
      onCellHover={(rowIdx, colId) => {
        if (!colId) { activeField = ''; return; }
        activeField = `item:${rowIdx}:${colId}`;
        const p = itemPageRef(rowIdx, colId);
        if (p > 0) jumpPdf(p);
      }}
      onRowMoveUp={(idx) => moveItem(idx, -1)}
      onRowMoveDown={(idx) => moveItem(idx, 1)}
      onRowDelete={(idx) => deleteItemRow(idx)}
      onAddRow={addItemRow}
      exportFilename="items.csv"
      maxHeight="none"
    />

    <!-- ═══ ITEMS VALIDATION STRIP ═══ -->
    <div class="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 mt-1 border-2 text-[10px] font-mono uppercase tracking-wider stamp-shadow"
      style="border-color: var(--line); background: var(--surface);">
      <span class="font-bold">TOTAL PRODUCTS: {itemsCount}</span>
      {#if lastDeleted}
        <button class="px-2 py-0.5 font-bold border cursor-pointer"
          style="color: var(--info); border-color: var(--info);"
          onclick={undoDelete}>↩ UNDO DELETE</button>
      {/if}
      <span style="color: var(--on-surface-muted);">|</span>
      <span>Σ VALUE: {itemsValueSum.toLocaleString(undefined, { maximumFractionDigits: 2 })} MMK</span>
      <span style="color: var(--on-surface-muted);">|</span>
      <span>DECLARED: {declaredTotal.toLocaleString(undefined, { maximumFractionDigits: 2 })} MMK</span>
      <span class="ml-auto px-2 py-0.5 font-bold border"
        style={valueBalanced
          ? 'color:var(--success);border-color:var(--success);'
          : 'color:var(--error);border-color:var(--error);'}>
        {#if !declaredTotal}NO TOTAL{:else if valueBalanced}✓ BALANCED{:else}⚠ GAP {(Math.abs(valueGap)).toLocaleString(undefined, { maximumFractionDigits: 0 })} MMK ({(declaredTotal ? Math.abs(valueGap) / declaredTotal * 100 : 0).toFixed(1)}%){/if}
      </span>
    </div>


    <!-- Edit log -->
    <div class="border-2"
      style="border-color: var(--line); background: var(--surface);">
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
                <span class="font-medium" style="color: var(--on-surface);">{e.field}</span>
                <span style="color: var(--outline);">{e.before} → </span>
                <span class="font-bold" style="color: var(--success);">{e.after}</span>
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
      style="border-color: var(--line); background: var(--surface);">
      <div class="dark-bar text-xs">REJECT_DOCUMENT</div>
      <div class="bg-white p-4">
        <div class="text-[10px] font-medium uppercase mb-2" style="color: var(--on-surface);">
          REASON FOR REJECTION (required)
        </div>
        <textarea
          class="w-full text-[11px] font-mono border-2 p-2"
          style="border-color: var(--line); min-height: 80px;"
          bind:value={rejectNotes}
          placeholder="e.g. Wrong document type — not a customs declaration"
        ></textarea>
        <div class="flex gap-2 mt-3 justify-end">
          <button class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
            style="border-color: var(--line); background: var(--surface); color: var(--on-surface);"
            onclick={() => showRejectModal = false}>CANCEL</button>
          <button class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
            style="border-color: var(--line); background: var(--error); color: white;"
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
      style="border-color: var(--line); background: var(--surface);">
      <div class="dark-bar text-xs">CONFIRM_APPROVE</div>
      <div class="bg-white p-4">
        <div class="text-[11px] font-bold uppercase mb-2" style="color: var(--warning);">
          ⚠ Some fields below 90% confidence — confirm approval?
        </div>
        <textarea
          class="w-full text-[11px] font-mono border-2 p-2 mt-2"
          style="border-color: var(--line); min-height: 60px;"
          bind:value={approveNotes}
          placeholder="Optional notes"
        ></textarea>
        <div class="flex gap-2 mt-3 justify-end">
          <button class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
            style="border-color: var(--line); background: var(--surface); color: var(--on-surface);"
            onclick={() => showApproveConfirm = false}>CANCEL</button>
          <button class="px-3 py-1.5 text-[10px] font-medium uppercase border-2 cursor-pointer"
            style="border-color: var(--line); background: var(--success); color: white;"
            onclick={doApprove}>✓ APPROVE ANYWAY</button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Job id in the status-bar meta line: long and not worth wrapping, but must
     stay fully selectable/copyable (users paste it into bug reports) even
     when visually clipped — hence ellipsis + a title with the full value. */
  .job-id {
    display: inline-block;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: bottom;
  }

  /* Where a value was found on the page.
     Two states on purpose. Every located value gets a faint mark, so a reviewer
     can see at a glance which figures on this sheet the pipeline actually found
     and which it did not — an absence is information. The row under the cursor
     gets the solid one.
     `pointer-events: none` matters: these sit over the page image, and a box
     that swallowed clicks would make the page feel broken. */
  .rv-box {
    position: absolute;
    pointer-events: none;
    border-radius: 2px;
    background: rgb(250 204 21 / 0.22);        /* amber wash, keeps ink legible */
    outline: 1px solid rgb(202 138 4 / 0.45);
    transition: background 90ms linear, outline-color 90ms linear;
  }
  .rv-box.on {
    background: rgb(250 204 21 / 0.42);
    outline: 2px solid var(--primary);
    box-shadow: 0 0 0 3px rgb(37 99 235 / 0.18);
  }

  /* One page in the scrolling column. The page the strip is pointing at gets a
     border, so after a jump it is obvious which sheet you landed on — with
     every page mounted, "the one on screen" is otherwise ambiguous. */
  .rv-page {
    outline: 1px solid var(--line);
    scroll-margin-top: 8px;
  }
  .rv-page.on {
    outline: 2px solid var(--primary);
  }
  /* Page number sits on the sheet's own corner: a stacked column has no other
     place to say which page you are reading. */
  /* A page that could not be rendered, at roughly the size the page would have
     been (A4 is 1:1.414) so the column keeps its shape and the scrollbar keeps
     telling the truth about how long the document is. */
  .rv-page-missing {
    width: min(78vw, 420px);
    aspect-ratio: 1 / 1.414;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    padding: 1rem;
    background: var(--surface-container);
    color: var(--on-surface-muted, var(--outline));
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-align: center;
  }
  .rv-page-missing-why {
    max-width: 32ch;
    letter-spacing: 0;
    font-size: 10px;
    line-height: 1.5;
    opacity: 0.75;
  }

  .rv-pageno {
    position: absolute;
    top: 0;
    left: 0;
    padding: 1px 5px;
    font-family: var(--font-mono, monospace);
    font-size: 9px;
    font-variant-numeric: tabular-nums;
    background: var(--on-surface);
    color: var(--surface);
    opacity: 0.65;
    pointer-events: none;
  }

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
