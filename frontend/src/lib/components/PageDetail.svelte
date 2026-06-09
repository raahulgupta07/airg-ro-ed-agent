<script lang="ts">
  import { auth } from '$lib/stores/auth.svelte';

  let {
    page = null as any,
    jobId = '' as string,
    totalPages = 0,
    onprev = () => {},
    onnext = () => {},
    onclose = () => {},
  } = $props();

  const typeColors: Record<string, string> = {
    customs_declaration: 'var(--info)',
    item_detail: 'var(--success)',
    invoice: 'var(--tertiary)',
    packing_list: '#5b4f8a',
    bill_of_lading: '#8b6914',
    delivery_order: '#4a7c59',
    stamps_only: '#999999',
  };

  const color = $derived(typeColors[page?.page_type] || '#666');
  const fields = $derived(page?.fields || {});
  const tables = $derived(page?.tables || []);
  const items = $derived(page?.items || []);  // backward compat
  const amounts = $derived(page?.amounts || []);
  const entities = $derived(page?.entities || {});
</script>

{#if page}
  <div class="border-2" style="border-color: var(--line);">
    <!-- Header -->
    <div class="dark-bar flex items-center justify-between text-xs">
      <span>PAGE {page.page_number} — {(page.page_type || 'unknown').replace(/_/g, ' ').toUpperCase()}</span>
      <button class="text-[10px] font-bold uppercase cursor-pointer" style="color: var(--primary-container);" onclick={onclose}>CLOSE</button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-0" style="background: white;">
      <!-- LEFT: PDF page as image (no scrolling — exact page only) -->
      {#if jobId}
        <div class="border-r overflow-y-auto" style="border-color: rgba(56,56,50,0.15); max-height: 600px;">
          {#key page.page_number}
            <img
              src="/api/jobs/{jobId}/page-image/{page.page_number}?token={auth.token}"
              alt="Page {page.page_number}"
              style="width: 100%; display: block;"
            />
          {/key}
        </div>
      {/if}

      <!-- RIGHT: Extracted data -->
      <div class="overflow-y-auto p-4 space-y-3" style="max-height: 600px;">
        <!-- Type + confidence -->
        <div class="flex items-center gap-2">
          <span class="px-2 py-1 text-[9px] font-medium uppercase text-white" style="background: {color};">
            {page.page_type?.replace(/_/g, ' ')}
          </span>
          <span class="text-[10px] font-mono" style="color: var(--outline);">
            confidence: {(page.confidence ?? 0).toFixed(2)}
          </span>
          <span class="text-[10px] font-mono" style="color: var(--outline);">
            {page.language || ''}
          </span>
        </div>

        <!-- Explanation -->
        {#if page.explanation}
          <div class="p-2 text-[11px] font-bold border" style="border-color: rgba(56,56,50,0.15); background: var(--surface-container); color: var(--on-surface);">
            {page.explanation}
          </div>
        {/if}

        <!-- Document info -->
        {#if page.doc_title || page.doc_issuer}
          <div class="border" style="border-color: rgba(56,56,50,0.15);">
            <div class="px-2 py-1 text-[8px] font-medium uppercase" style="background: var(--surface-container); color: var(--outline);">DOCUMENT</div>
            <div class="grid grid-cols-2 gap-0">
              {#each [['Title', page.doc_title], ['Issuer', page.doc_issuer], ['Date', page.doc_date], ['Reference', page.doc_reference], ['Country', page.doc_country]] as [label, value]}
                {#if value}
                  <div class="px-2 py-1 border-b border-r" style="border-color: rgba(56,56,50,0.08);">
                    <div class="text-[7px] font-medium uppercase" style="color: var(--outline);">{label}</div>
                    <div class="text-[10px] font-bold" style="color: var(--on-surface);">{value}</div>
                  </div>
                {/if}
              {/each}
            </div>
          </div>
        {/if}

        <!-- Fields -->
        {#if Object.keys(fields).length > 0}
          <div class="border" style="border-color: rgba(56,56,50,0.15);">
            <div class="px-2 py-1 text-[8px] font-medium uppercase" style="background: var(--surface-container); color: var(--outline);">
              FIELDS ({Object.keys(fields).length})
            </div>
            <div class="overflow-y-auto" style="max-height: 200px;">
              {#each Object.entries(fields) as [key, value]}
                <div class="flex border-b px-2 py-0.5" style="border-color: rgba(56,56,50,0.05);">
                  <span class="text-[9px] font-bold uppercase w-1/3 truncate" style="color: var(--outline);">{key}</span>
                  <span class="text-[10px] font-bold flex-1 truncate" style="color: var(--on-surface);">{value}</span>
                </div>
              {/each}
            </div>
          </div>
        {/if}

        <!-- Tables (extracted as-is from document) -->
        {#if tables.length > 0}
          {#each tables as tbl, ti}
            <div class="border" style="border-color: rgba(56,56,50,0.15);">
              <div class="px-2 py-1 text-[8px] font-medium uppercase" style="background: var(--surface-container); color: var(--outline);">
                {tbl.title || `TABLE ${ti+1}`} ({tbl.rows?.length || 0} rows)
              </div>
              <div class="overflow-x-auto">
                <table class="w-full text-[10px]">
                  {#if tbl.headers?.length > 0}
                    <thead>
                      <tr style="background: var(--surface-container);">
                        {#each tbl.headers as h}
                          <th class="px-2 py-1 text-left text-[8px] font-bold uppercase whitespace-nowrap" style="color: var(--outline);">{h}</th>
                        {/each}
                      </tr>
                    </thead>
                  {/if}
                  <tbody>
                    {#each tbl.rows || [] as row}
                      <tr class="border-t" style="border-color: rgba(56,56,50,0.05);">
                        {#each row as cell}
                          <td class="px-2 py-0.5 font-mono whitespace-nowrap" style="color: var(--on-surface);">{cell ?? '—'}</td>
                        {/each}
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </div>
          {/each}
        {/if}

        <!-- Legacy items (backward compat for old extractions) -->
        {#if items.length > 0 && tables.length === 0}
          <div class="border" style="border-color: rgba(56,56,50,0.15);">
            <div class="px-2 py-1 text-[8px] font-medium uppercase" style="background: var(--surface-container); color: var(--outline);">
              ITEMS ({items.length})
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-[10px]">
                <thead>
                  <tr style="background: var(--surface-container);">
                    <th class="px-2 py-1 text-left text-[8px] font-bold uppercase" style="color: var(--outline);">Name</th>
                    <th class="px-2 py-1 text-right text-[8px] font-bold uppercase" style="color: var(--outline);">Qty</th>
                    <th class="px-2 py-1 text-right text-[8px] font-bold uppercase" style="color: var(--outline);">Price</th>
                    <th class="px-2 py-1 text-left text-[8px] font-bold uppercase" style="color: var(--outline);">Origin</th>
                  </tr>
                </thead>
                <tbody>
                  {#each items as item}
                    <tr class="border-t" style="border-color: rgba(56,56,50,0.05);">
                      <td class="px-2 py-0.5 font-bold truncate" style="color: var(--on-surface); max-width: 200px;">{item.name || '—'}</td>
                      <td class="px-2 py-0.5 text-right font-mono" style="color: var(--on-surface);">{item.qty || '—'}</td>
                      <td class="px-2 py-0.5 text-right font-mono" style="color: var(--on-surface);">{item.unit_price || item.amount || '—'}</td>
                      <td class="px-2 py-0.5" style="color: var(--outline);">{item.origin || '—'}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </div>
        {/if}

        <!-- Amounts -->
        {#if amounts.length > 0}
          <div class="border" style="border-color: rgba(56,56,50,0.15);">
            <div class="px-2 py-1 text-[8px] font-medium uppercase" style="background: var(--surface-container); color: var(--outline);">
              AMOUNTS ({amounts.length})
            </div>
            {#each amounts as amt}
              <div class="flex justify-between px-2 py-0.5 border-b" style="border-color: rgba(56,56,50,0.05);">
                <span class="text-[9px] font-bold" style="color: var(--outline);">{amt.label || '—'}</span>
                <span class="text-[10px] font-mono font-bold" style="color: var(--on-surface);">{amt.value?.toLocaleString() ?? '—'} {amt.currency || ''}</span>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Visual -->
        <div class="flex gap-2 text-[9px] font-bold uppercase" style="color: var(--outline);">
          {#if page.has_logo}<span class="px-1 py-0.5 border" style="border-color: rgba(56,56,50,0.2);">LOGO</span>{/if}
          {#if page.has_stamp}<span class="px-1 py-0.5 border" style="border-color: rgba(56,56,50,0.2);">STAMP</span>{/if}
          {#if page.has_signature}<span class="px-1 py-0.5 border" style="border-color: rgba(56,56,50,0.2);">SIGNATURE</span>{/if}
          {#if page.has_barcode}<span class="px-1 py-0.5 border" style="border-color: rgba(56,56,50,0.2);">BARCODE</span>{/if}
          {#if page.visual_quality}<span class="px-1 py-0.5 border" style="border-color: rgba(56,56,50,0.2);">Quality: {page.visual_quality}</span>{/if}
        </div>
      </div>
    </div>

    <!-- Navigation -->
    <div class="flex items-center justify-between px-4 py-2 border-t" style="border-color: rgba(56,56,50,0.15); background: var(--surface-container);">
      <button class="text-[10px] font-bold uppercase cursor-pointer" style="color: {page.page_number > 1 ? 'var(--on-surface)' : 'var(--outline)'};"
        onclick={onprev} disabled={page.page_number <= 1}>
        &larr; PREV PAGE
      </button>
      <span class="text-[10px] font-mono font-bold" style="color: var(--outline);">
        Page {page.page_number} of {totalPages}
      </span>
      <button class="text-[10px] font-bold uppercase cursor-pointer" style="color: {page.page_number < totalPages ? 'var(--on-surface)' : 'var(--outline)'};"
        onclick={onnext} disabled={page.page_number >= totalPages}>
        NEXT PAGE &rarr;
      </button>
    </div>
  </div>
{/if}
