<script lang="ts">
  /**
   * One field the engine is not sure about, and everything needed to settle it:
   * the patch of paper it was read from, what we read, what a second read said,
   * why it is here, and three ways to finish it.
   *
   * Deliberately absent: any confidence percentage. The stored float is
   * uncalibrated — a real run returned 1.0 on a field it simultaneously marked
   * suspect — so showing it would teach reviewers to trust a number that means
   * nothing. Status and plain words carry the uncertainty instead.
   */
  import { api } from '$lib/api';

  let {
    jobId,
    check,
    onresolved = undefined as ((field: string, value: any) => void) | undefined,
    compact = false,
  } = $props();

  let manual = $state('');
  let busy = $state(false);
  let error = $state('');
  let imgFailed = $state(false);
  let showImage = $state(true);
  let enlarged = $state(false);
  let closeBtn: HTMLButtonElement | undefined = $state();

  const cropUrl = $derived(api.evidenceCropUrl(jobId, check.field));

  // How well we know where this came from. "unknown" is a real, honest state:
  // 11 of the 16 documents in the corpus have no text layer, so there is nothing
  // to measure and we show the whole page rather than a box we invented.
  const located = $derived(check.located as 'exact' | 'estimated' | 'unknown');
  const locatedLabel = $derived(
    located === 'exact' ? 'Located on the form'
    : located === 'estimated' ? 'Location estimated'
    : 'Location not known'
  );
  const locatedHint = $derived(
    located === 'exact' ? 'Measured from the page text.'
    : located === 'estimated' ? 'The reader reported this spot; it has not been measured.'
    : 'This page is a scan with no text layer, so the whole page is shown.'
  );

  function fmt(v: any): string {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'number') return v.toLocaleString('en-US', { maximumFractionDigits: 4 });
    return String(v);
  }

  async function resolve(value: any, choice: 'kept' | 'alternate' | 'manual') {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await api.evidenceResolve(jobId, check.field, value, choice);
      onresolved?.(check.field, value);
    } catch (e: any) {
      error = e?.message || 'Could not save that.';
    } finally {
      busy = false;
    }
  }

  function submitManual() {
    const v = manual.trim();
    if (!v) return;
    resolve(v, 'manual');
  }

  // Click-to-enlarge. Matters most for the "unknown" case — a whole scanned
  // page shrunk to fit a 150px card strip is illegible; the lightbox is the
  // only place a reviewer can actually read the figure they're confirming.
  function openLightbox() {
    if (imgFailed) return;
    enlarged = true;
  }
  function closeLightbox() {
    enlarged = false;
  }
  function onLightboxKeydown(e: KeyboardEvent) {
    if (enlarged && e.key === 'Escape') closeLightbox();
  }
  $effect(() => {
    if (enlarged) closeBtn?.focus();
  });
</script>

<div class="cl-panel ev" class:compact>
  <div class="cl-hd ev-hd">
    <span>{check.label}</span>
    <span class="pill {check.status === 'suspect' ? 'warn' : 'muted'}">
      {check.status === 'empty' ? 'not found' : 'needs a check'}
    </span>
  </div>

  <div class="cl-bd ev-bd">
    <!-- What the form actually says -->
    {#if showImage && !imgFailed}
      <figure class="shot" class:unknown={located === 'unknown'}>
        <button type="button" class="shot-trigger" onclick={openLightbox}
                aria-label={located === 'unknown'
                  ? 'View the full scanned page (this is a whole page, not a close crop)'
                  : 'View this crop full size'}>
          <img src={cropUrl} alt="The part of the document this value was read from"
               onerror={() => (imgFailed = true)} />
          {#if located === 'unknown'}
            <span class="enlarge-hint">⤢ Whole page — tap to read</span>
          {/if}
        </button>
        <figcaption>
          <span class="loc loc-{located}">{locatedLabel}</span>
          {locatedHint}
          {#if check.page}<span class="pg">page {check.page}</span>{/if}
        </figcaption>
      </figure>
    {:else if imgFailed}
      <p class="fallback">The page image could not be loaded. The text below is what was read.</p>
    {/if}

    <!-- The two readings, side by side -->
    <div class="reads">
      <div class="read read-ours">
        <span class="rl">We read</span>
        <span class="rv">{fmt(check.value)}</span>
      </div>
      {#each check.alternates as alt}
        <div class="read read-alt">
          <span class="rl">Second opinion</span>
          <span class="rv">{fmt(alt)}</span>
        </div>
      {/each}
    </div>

    <p class="why">{check.reason}</p>

    {#if check.source}
      <p class="src" title="The line of text this value was taken from">{check.source}</p>
    {/if}

    <!-- Finish it -->
    <div class="acts">
      <button class="cl-btn sm" disabled={busy}
              onclick={() => resolve(check.value, 'kept')}>
        Keep {fmt(check.value)}
      </button>
      {#each check.alternates as alt}
        <button class="cl-btn sm" disabled={busy}
                onclick={() => resolve(alt, 'alternate')}>
          Use {fmt(alt)}
        </button>
      {/each}
      <span class="or">or</span>
      <input class="cl-inp sm" bind:value={manual} placeholder="type the correct value"
             disabled={busy}
             onkeydown={(e) => { if (e.key === 'Enter') submitManual(); }} />
      <button class="cl-btn sm primary" disabled={busy || !manual.trim()}
              onclick={submitManual}>Save</button>
    </div>

    {#if error}<p class="err">{error}</p>{/if}
  </div>
</div>

<svelte:window onkeydown={onLightboxKeydown} />

{#if enlarged}
  <!-- Backdrop only closes on a click that lands on it directly (not one that
       bubbled up from the dialog contents), so the dialog itself needs no
       click/stopPropagation handler of its own — Escape (global, above) is
       the keyboard path. -->
  <div class="lightbox-backdrop" role="presentation"
       onclick={(e) => { if (e.target === e.currentTarget) closeLightbox(); }}>
    <div class="lightbox" role="dialog" aria-modal="true" aria-label="Full-size page image" tabindex="-1">
      <button type="button" class="lightbox-close" bind:this={closeBtn} onclick={closeLightbox}>
        ✕ Close
      </button>
      <img src={cropUrl} alt="The part of the document this value was read from" />
    </div>
  </div>
{/if}

<style>
  .ev { margin-bottom: 12px; }
  .ev-hd { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .ev-bd { display: flex; flex-direction: column; gap: 10px; }

  .shot { margin: 0; }
  .shot-trigger {
    display: block; width: 100%; padding: 0; margin: 0;
    border: none; background: none; cursor: zoom-in; position: relative;
    font: inherit; text-align: left;
  }
  .shot-trigger:focus-visible {
    outline: 2px solid var(--primary); outline-offset: 2px; border-radius: var(--radius-md);
  }
  .shot img {
    display: block; width: 100%; max-width: 100%; height: auto;
    max-height: 320px; object-fit: contain; object-position: left top;
    background: var(--surface-container-lowest);
    border: 1px solid var(--line); border-radius: var(--radius-md);
  }
  /* The "unknown" case is a whole A4 page, not a tight crop — a small strip
     renders it illegible. Give it more room and a visible reason to enlarge,
     instead of the tiny crop sizing that suits a measured box. */
  .shot.unknown img { max-height: 420px; object-fit: contain; }
  .shot.unknown .enlarge-hint {
    position: absolute; right: 6px; bottom: 6px;
    font-family: var(--font-mono, monospace); font-size: 10px;
    padding: 3px 7px; border-radius: var(--radius-sm);
    background: var(--surface); border: 1px solid var(--line);
    color: var(--on-surface-subtle);
  }
  .shot figcaption {
    margin-top: 5px; font-size: 11px; line-height: 1.5;
    color: var(--on-surface-subtle);
  }
  .loc {
    font-family: var(--font-mono, monospace); font-size: 10px;
    text-transform: uppercase; letter-spacing: .04em;
    padding: 1px 5px; margin-right: 6px; border-radius: var(--radius-sm);
    border: 1px solid var(--line);
  }
  .loc-exact    { color: var(--success); border-color: var(--success); }
  .loc-estimated{ color: var(--warning); border-color: var(--warning); }
  .loc-unknown  { color: var(--on-surface-subtle); }
  .pg { margin-left: 6px; }

  .reads { display: flex; flex-wrap: wrap; gap: 8px; }
  .read {
    flex: 1 1 160px; min-width: 0;
    padding: 7px 10px; border-radius: var(--radius-md);
    border: 1px solid var(--line); background: var(--surface-container-lowest);
  }
  .read-ours { border-color: var(--error); }
  .read-alt  { border-color: var(--success); }
  .rl {
    display: block; font-size: 10px; text-transform: uppercase;
    letter-spacing: .04em; color: var(--on-surface-subtle);
  }
  .rv {
    display: block; font-family: var(--font-mono, monospace);
    font-size: 17px; font-variant-numeric: tabular-nums;
    color: var(--on-surface); word-break: break-all;
  }
  .read-ours .rv { color: var(--error); }
  .read-alt  .rv { color: var(--success); }

  .why { margin: 0; font-size: 13px; line-height: 1.5; color: var(--on-surface); }
  .src {
    margin: 0; font-family: var(--font-mono, monospace); font-size: 11px;
    color: var(--on-surface-subtle); overflow-x: auto; white-space: pre;
  }

  .acts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .or { font-size: 11px; color: var(--on-surface-subtle); }
  .acts .cl-inp { flex: 1 1 150px; min-width: 120px; }
  .err { margin: 0; font-size: 12px; color: var(--error); }
  .fallback { margin: 0; font-size: 12px; color: var(--on-surface-muted); }

  .compact .shot img { max-height: 150px; }
  /* Same reasoning as the non-compact rule above, scaled to the review
     panel's tighter column: 150px is fine for a tight measured crop, but a
     whole scanned page needs enough height to actually read a figure. */
  .compact .shot.unknown img { max-height: 260px; }

  .lightbox-backdrop {
    position: fixed; inset: 0; z-index: 1000;
    background: rgb(0 0 0 / 0.6);
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .lightbox {
    position: relative; max-width: min(92vw, 1100px); max-height: 92vh;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-md); box-shadow: var(--shadow-lg);
    padding: 40px 12px 12px; display: flex;
  }
  .lightbox img {
    display: block; max-width: 100%; max-height: calc(92vh - 52px);
    margin: 0 auto; object-fit: contain;
    background: var(--surface-container-lowest);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
  }
  .lightbox-close {
    position: absolute; top: 8px; right: 8px;
    font-family: var(--font-mono, monospace); font-size: 11px;
    padding: 5px 10px; border-radius: var(--radius-sm);
    border: 1px solid var(--line); background: var(--surface-container-lowest);
    color: var(--on-surface); cursor: pointer;
  }
  .lightbox-close:focus-visible {
    outline: 2px solid var(--primary); outline-offset: 2px;
  }

  @media (prefers-reduced-motion: no-preference) {
    .lightbox-backdrop { animation: ev-fade-in 120ms ease-out; }
  }
  @keyframes ev-fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }
</style>
