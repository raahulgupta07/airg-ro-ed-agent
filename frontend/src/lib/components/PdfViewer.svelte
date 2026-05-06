<script lang="ts">
  import { onMount } from 'svelte';

  export type Bbox = {
    field: string;
    label?: string;
    x: number;
    y: number;
    w: number;
    h: number;
    page: number;
    color?: string;
    kind?: 'decl' | 'item' | 'fee';
    item_index?: number;
  };

  let {
    pdfUrl,
    page = 1,
    bboxes = [],
    highlightField = null,
    onBoxClick,
    onPageChange,
    scale = $bindable(1.5),
  }: {
    pdfUrl: string;
    page?: number;
    bboxes?: Bbox[];
    highlightField?: string | null;
    onBoxClick?: (field: string, item_index?: number) => void;
    onPageChange?: (page: number) => void;
    scale?: number;
  } = $props();

  let pdfjsLib: any = null;
  let pdfDoc = $state<any>(null);
  let totalPages = $state(0);
  let canvasEl: HTMLCanvasElement | undefined = $state();
  let viewport: any = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let renderToken = 0;

  // Lazy-load pdfjs (worker bundled via Vite ?url import)
  async function loadPdfjs() {
    if (pdfjsLib) return pdfjsLib;
    try {
      const lib = await import('pdfjs-dist');
      // @ts-ignore — Vite ?url import returns string
      const workerUrl = (await import('pdfjs-dist/build/pdf.worker.min.mjs?url')).default;
      lib.GlobalWorkerOptions.workerSrc = workerUrl;
      pdfjsLib = lib;
      return lib;
    } catch (e: any) {
      loadError = `pdfjs load failed: ${e?.message || e}`;
      throw e;
    }
  }

  let lastUrl = '';
  async function loadPdf() {
    if (!pdfUrl || pdfUrl === lastUrl) return;
    lastUrl = pdfUrl;
    loading = true;
    loadError = '';
    try {
      const lib = await loadPdfjs();
      const task = lib.getDocument(pdfUrl);
      pdfDoc = await task.promise;
      totalPages = pdfDoc.numPages;
      await renderPage();
    } catch (e: any) {
      loadError = `PDF load failed: ${e?.message || e}`;
      pdfDoc = null;
    } finally {
      loading = false;
    }
  }

  async function renderPage() {
    if (!pdfDoc || !canvasEl) return;
    const myToken = ++renderToken;
    const safePage = Math.max(1, Math.min(page, pdfDoc.numPages));
    try {
      const pdfPage = await pdfDoc.getPage(safePage);
      const vp = pdfPage.getViewport({ scale });
      if (myToken !== renderToken) return;
      canvasEl.width = vp.width;
      canvasEl.height = vp.height;
      canvasEl.style.width = `${vp.width}px`;
      canvasEl.style.height = `${vp.height}px`;
      const ctx = canvasEl.getContext('2d');
      if (!ctx) return;
      await pdfPage.render({ canvasContext: ctx, viewport: vp }).promise;
      if (myToken !== renderToken) return;
      viewport = vp;
    } catch (e: any) {
      if (e?.name !== 'RenderingCancelledException') {
        loadError = `Render failed: ${e?.message || e}`;
      }
    }
  }

  // Effects
  $effect(() => {
    pdfUrl;
    loadPdf();
  });
  $effect(() => {
    page; scale;
    if (pdfDoc) renderPage();
  });

  function goPage(p: number) {
    if (!totalPages) return;
    const np = Math.max(1, Math.min(totalPages, p));
    if (np !== page) onPageChange?.(np);
  }
  function zoomIn() { scale = Math.min(4, +(scale + 0.25).toFixed(2)); }
  function zoomOut() { scale = Math.max(0.5, +(scale - 0.25).toFixed(2)); }
  function zoomFit() { scale = 1.5; }

  // Convert each bbox (in PDF page units) → viewport pixels
  type ViewBox = Bbox & { vx: number; vy: number; vw: number; vh: number };
  const currentPageBoxes = $derived.by<ViewBox[]>(() => {
    if (!viewport) return [];
    const out: ViewBox[] = [];
    for (const b of bboxes || []) {
      if (!b || (b.page || 1) !== page) continue;
      try {
        const [vx0, vy0] = viewport.convertToViewportPoint(b.x, b.y);
        const [vx1, vy1] = viewport.convertToViewportPoint(b.x + b.w, b.y + b.h);
        const vx = Math.min(vx0, vx1);
        const vy = Math.min(vy0, vy1);
        const vw = Math.abs(vx1 - vx0);
        const vh = Math.abs(vy1 - vy0);
        out.push({ ...b, vx, vy, vw, vh });
      } catch {
        // ignore malformed bbox
      }
    }
    return out;
  });
</script>

<div class="pdf-viewer" style="position: relative; display: flex; flex-direction: column; height: 100%;">
  <!-- Toolbar -->
  <div class="dark-bar flex items-center gap-2 text-xs" style="padding: 4px 8px;">
    <button
      class="px-2 py-0.5 text-[10px] font-black border cursor-pointer"
      style="border-color: var(--surface); color: var(--surface); background: transparent;"
      disabled={page <= 1}
      onclick={() => goPage(page - 1)}
    >◀</button>
    <span class="font-mono">p{page} / {totalPages || '?'}</span>
    <button
      class="px-2 py-0.5 text-[10px] font-black border cursor-pointer"
      style="border-color: var(--surface); color: var(--surface); background: transparent;"
      disabled={!totalPages || page >= totalPages}
      onclick={() => goPage(page + 1)}
    >▶</button>
    <span class="ml-auto flex items-center gap-1">
      <button class="px-1.5 text-[10px] font-black border cursor-pointer"
        style="border-color: var(--surface); color: var(--surface); background: transparent;"
        onclick={zoomOut}>−</button>
      <button class="px-1.5 text-[10px] font-black border cursor-pointer font-mono"
        style="border-color: var(--surface); color: var(--surface); background: transparent;"
        onclick={zoomFit} title="Reset zoom">{(scale * 100).toFixed(0)}%</button>
      <button class="px-1.5 text-[10px] font-black border cursor-pointer"
        style="border-color: var(--surface); color: var(--surface); background: transparent;"
        onclick={zoomIn}>+</button>
      <span class="font-mono ml-2">{currentPageBoxes.length} boxes</span>
    </span>
  </div>

  <!-- Canvas + overlay container -->
  <div style="flex: 1; overflow: auto; background: #525659; padding: 12px; display: flex; justify-content: center; align-items: flex-start;">
    {#if loading}
      <div style="color: white; font-family: monospace; font-size: 11px; padding: 40px;">LOADING PDF...</div>
    {:else if loadError}
      <div style="color: #fca5a5; font-family: monospace; font-size: 11px; padding: 40px; text-align: center;">
        {loadError}
      </div>
    {:else if !pdfDoc}
      <div style="color: white; font-family: monospace; font-size: 11px; padding: 40px;">PDF NOT AVAILABLE</div>
    {:else}
      <div style="position: relative; width: fit-content; box-shadow: 0 4px 20px rgba(0,0,0,0.4);">
        <canvas bind:this={canvasEl}></canvas>

        <div style="position: absolute; inset: 0; pointer-events: none;">
          {#each currentPageBoxes as box (box.field + ':' + (box.item_index ?? '') + ':' + box.page)}
            {@const isHi = highlightField === box.field}
            {@const c = box.color || '#94a3b8'}
            <button
              type="button"
              class="bbox-overlay"
              style="
                position: absolute;
                left: {box.vx}px;
                top: {box.vy}px;
                width: {box.vw}px;
                height: {box.vh}px;
                border: {isHi ? 3 : 2}px solid {c};
                background: {c}22;
                cursor: pointer;
                pointer-events: auto;
                transition: all 0.15s;
                padding: 0;
                {isHi ? `box-shadow: 0 0 0 2px white, 0 0 0 4px ${c};` : ''}
              "
              title={box.label || box.field}
              onclick={() => onBoxClick?.(box.field, box.item_index)}
            >
              <span style="
                position: absolute;
                top: -14px;
                left: 0;
                font-size: 9px;
                font-weight: 900;
                background: {c};
                color: white;
                padding: 1px 4px;
                white-space: nowrap;
                font-family: monospace;
                line-height: 1.2;
                letter-spacing: 0.02em;
              ">{box.label || box.field}</span>
            </button>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .bbox-overlay:hover {
    background-color: rgba(255, 255, 255, 0.15) !important;
    filter: brightness(1.15);
  }
</style>
