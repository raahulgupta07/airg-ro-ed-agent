<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';

  type Field = string;
  type Measure = { id: string; field?: string; agg: string; label: string };
  type Spec = {
    name: string;
    grain: 'document' | 'product' | 'joined';
    dimensions: Field[];
    measures: Measure[];
    filters: any[];
    sort: any;
  };

  const GRAINS: { key: Spec['grain']; label: string }[] = [
    { key: 'document', label: 'Document' },
    { key: 'product', label: 'Product' },
    { key: 'joined', label: 'Mix' },
  ];

  let spec = $state<Spec>({ name: '', grain: 'document', dimensions: [], measures: [], filters: [], sort: null });
  let fields = $state<{ dimensions: Field[]; measures: Field[] }>({ dimensions: [], measures: [] });
  let saved = $state<Spec[]>([]);
  let result = $state<any>(null);
  let error = $state('');
  let running = $state(false);
  let savedName = $state('');            // the name of the currently-loaded/saved report (enables export)

  let debounce: ReturnType<typeof setTimeout> | null = null;

  async function loadFields() {
    error = '';
    try {
      fields = await api.cubeFields(spec.grain);
    } catch (e: any) { error = e.message || 'Failed to load fields'; fields = { dimensions: [], measures: [] }; }
  }

  async function loadSaved() {
    try { saved = await api.cubeList(); }
    catch (e: any) { error = e.message || 'Failed to load saved reports'; }
  }

  onMount(async () => { await Promise.all([loadFields(), loadSaved()]); recompute(); });

  async function onGrain(g: Spec['grain']) {
    if (g === spec.grain) return;
    spec.grain = g;
    spec.dimensions = [];
    spec.measures = [];
    savedName = '';
    result = null;
    await loadFields();
    recompute();
  }

  function toggleDim(d: Field, on: boolean) {
    spec.dimensions = on ? [...spec.dimensions, d] : spec.dimensions.filter((x) => x !== d);
    savedName = '';
    recompute();
  }

  function hasMeasure(id: string) { return spec.measures.some((m) => m.id === id); }
  function toggleMeasure(m: Measure, on: boolean) {
    spec.measures = on ? [...spec.measures, m] : spec.measures.filter((x) => x.id !== m.id);
    savedName = '';
    recompute();
  }

  function recompute() {
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(run, 250);
  }

  async function run() {
    if (!spec.dimensions.length || !spec.measures.length) { result = null; return; }
    running = true; error = '';
    try {
      result = await api.cubeRun({ ...spec });
    } catch (e: any) { error = e.message || 'Run failed'; result = null; }
    running = false;
  }

  async function save() {
    const name = spec.name.trim();
    if (!name) { error = 'Give the report a name first.'; return; }
    if (!spec.dimensions.length || !spec.measures.length) { error = 'Pick a group-by and a total before saving.'; return; }
    error = '';
    try {
      await api.cubeSave({ ...spec, name });
      savedName = name;
      await loadSaved();
    } catch (e: any) { error = e.message || 'Save failed'; }
  }

  async function loadReport(name: string) {
    error = '';
    try {
      const s: Spec = await api.cubeGet(name);
      spec = {
        name: s.name || name,
        grain: s.grain || 'document',
        dimensions: s.dimensions || [],
        measures: s.measures || [],
        filters: s.filters || [],
        sort: s.sort ?? null,
      };
      savedName = spec.name;
      await loadFields();
      run();
    } catch (e: any) { error = e.message || 'Failed to load report'; }
  }

  async function del(name: string) {
    error = '';
    try {
      await api.cubeDelete(name);
      if (savedName === name) savedName = '';
      await loadSaved();
    } catch (e: any) { error = e.message || 'Delete failed'; }
  }

  async function exportCsv() {
    if (!savedName) return;
    error = '';
    try {
      await auth.ensureValidToken();
      const res = await fetch(`/api/rover/cube/${encodeURIComponent(savedName)}/export.csv`, {
        headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
      });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = `${savedName}.csv`;
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e: any) { error = e.message || 'Export failed'; }
  }

  function fmt(v: any) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(String(v).replace(/,/g, ''));
    return isNaN(n) ? String(v) : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  const title = $derived(
    (spec.dimensions.length ? spec.dimensions.join(' · ') : 'no group-by') +
    ' × ' +
    (spec.measures.length ? spec.measures.map((m) => m.label).join(', ') : 'no totals')
  );
</script>

<svelte:head><title>City Agent ROVER · Report Builder</title></svelte:head>

<div class="rover">
  <header class="rhead">
    <div>
      <div class="kick">City Agent ROVER</div>
      <h1>Report Builder</h1>
      <p class="sub">Slice every verified release order into a pivot — group by any field, total any measure, save the view, export the numbers.</p>
    </div>
  </header>

  {#if error}<div class="err">{error}</div>{/if}

  <div class="grid">
    <!-- LEFT: picker -->
    <section>
      <div class="card">
        <div class="lbl">Grain</div>
        <div class="seg">
          {#each GRAINS as g (g.key)}
            <button class:on={spec.grain === g.key} onclick={() => onGrain(g.key)}>{g.label}</button>
          {/each}
        </div>

        <div class="lbl mt">Group by · dimensions</div>
        {#if fields.dimensions.length}
          <div class="checks">
            {#each fields.dimensions as d (d)}
              <label class="chk">
                <input type="checkbox" checked={spec.dimensions.includes(d)}
                       onchange={(e) => toggleDim(d, (e.target as HTMLInputElement).checked)} />
                <span>{d}</span>
              </label>
            {/each}
          </div>
        {:else}<div class="muted">No dimensions for this grain.</div>{/if}

        <div class="lbl mt">Totals · measures</div>
        <div class="checks">
          <label class="chk">
            <input type="checkbox" checked={hasMeasure('__count')}
                   onchange={(e) => toggleMeasure({ id: '__count', agg: 'count', label: 'count' }, (e.target as HTMLInputElement).checked)} />
            <span>record count</span>
          </label>
          {#each fields.measures as m (m)}
            <label class="chk">
              <input type="checkbox" checked={hasMeasure(m)}
                     onchange={(e) => toggleMeasure({ id: m, field: m, agg: 'sum', label: m }, (e.target as HTMLInputElement).checked)} />
              <span>{m} <em>· sum</em></span>
            </label>
          {/each}
        </div>
      </div>

      <div class="card">
        <div class="lbl">Save this report</div>
        <div class="saverow">
          <input class="fin" placeholder="report name" bind:value={spec.name} />
          <button class="btn sm" onclick={save}>Save</button>
        </div>
      </div>

      <div class="card">
        <div class="lbl">Saved reports <span class="badge">{saved.length}</span></div>
        {#if saved.length}
          <div class="tblwrap">
            <table>
              <thead><tr><th>Name</th><th>Grain</th><th></th></tr></thead>
              <tbody>
                {#each saved as s (s.name)}
                  <tr class:sel={savedName === s.name}>
                    <td class="docid">{s.name}</td>
                    <td>{s.grain}</td>
                    <td class="rowacts">
                      <button class="btn ghost sm" onclick={() => loadReport(s.name)}>run</button>
                      <button class="btn ghost sm x" onclick={() => del(s.name)} aria-label="delete">✕</button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}<div class="muted">No saved reports yet.</div>{/if}
      </div>
    </section>

    <!-- RIGHT: pivot -->
    <section>
      <div class="rtitle">
        <div>
          <h2>{title}</h2>
          <div class="meta">
            {#if result}{result.group_count} group{result.group_count === 1 ? '' : 's'} · {result.row_count} row{result.row_count === 1 ? '' : 's'}{:else}—{/if}
          </div>
        </div>
        <button class="btn ghost" onclick={exportCsv} disabled={!savedName}
                title={savedName ? 'Export the saved report' : 'Save the report to export'}>⬇ Export CSV</button>
      </div>

      {#if !spec.dimensions.length || !spec.measures.length}
        <div class="empty card">Pick a group-by and a total.</div>
      {:else if running && !result}
        <div class="empty card">Computing…</div>
      {:else if result}
        <div class="tblwrap">
          <table>
            <thead>
              <tr>
                {#each result.dimensions as dn (dn)}<th>{dn}</th>{/each}
                {#each result.measures as m (m.id)}<th class="num">{m.label}</th>{/each}
              </tr>
            </thead>
            <tbody>
              {#each result.rows as row, i (i)}
                <tr>
                  {#each result.dimensions as dn (dn)}<td>{row[dn] ?? '—'}</td>{/each}
                  {#each result.measures as m (m.id)}<td class="num v">{fmt(row[m.id])}</td>{/each}
                </tr>
              {/each}
              {#if !result.rows?.length}
                <tr><td colspan={result.dimensions.length + result.measures.length} class="empty">No matching data.</td></tr>
              {/if}
              {#if result.rows?.length}
                <tr class="totrow">
                  {#each result.dimensions as dn, i (dn)}<td>{i === 0 ? 'TOTAL' : ''}</td>{/each}
                  {#each result.measures as m (m.id)}<td class="num v">{fmt(result.totals?.[m.id])}</td>{/each}
                </tr>
              {/if}
            </tbody>
          </table>
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
  h2 { font-family: 'Source Serif 4', Georgia, serif; font-size: 19px; margin: 0 0 4px;
    display: flex; align-items: center; gap: 8px; }
  .sub { color: var(--on-surface-muted); margin: 0; max-width: 62ch; font-size: 14px; }
  .badge { background: var(--surface-container-high); color: var(--on-surface-muted); border-radius: 20px;
    padding: 1px 9px; font-size: 12px; font-family: 'JetBrains Mono', monospace; }
  .err { background: var(--error-soft); color: var(--error); border: 1px solid var(--error-container);
    border-radius: var(--radius-md); padding: 10px 14px; margin: 14px 0; font-size: 14px; }
  .grid { display: grid; grid-template-columns: 360px 1fr; gap: 22px; align-items: start; margin-top: 22px; }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--surface-container-lowest); border: 1px solid var(--outline-variant);
    border-radius: var(--radius-lg); padding: 14px 16px; margin-bottom: 12px; box-shadow: var(--shadow-xs); }
  .lbl { font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase;
    letter-spacing: .08em; color: var(--on-surface-muted); margin-bottom: 8px;
    display: flex; align-items: center; gap: 8px; }
  .lbl.mt { margin-top: 16px; }
  .muted { color: var(--on-surface-muted); font-size: 13px; padding: 4px 0; }
  .seg { display: inline-flex; border: 1px solid var(--outline); border-radius: var(--radius-md); overflow: hidden; }
  .seg button { background: transparent; border: none; padding: 7px 14px; font-size: 13px; cursor: pointer;
    color: var(--on-surface-muted); font-family: 'Inter', sans-serif; border-right: 1px solid var(--outline-variant); }
  .seg button:last-child { border-right: none; }
  .seg button.on { background: var(--primary-container); color: var(--primary-hover); font-weight: 600; }
  .seg button:hover:not(.on) { background: var(--surface-container-low); }
  .checks { display: flex; flex-direction: column; gap: 2px; }
  .chk { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 4px 4px; border-radius: var(--radius-sm);
    cursor: pointer; color: var(--on-surface); }
  .chk:hover { background: var(--surface-container-low); }
  .chk input { accent-color: var(--primary); width: 15px; height: 15px; }
  .chk em { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--on-surface-subtle); }
  .saverow { display: flex; gap: 8px; align-items: center; }
  .fin { flex: 1; background: var(--surface); border: 1px solid var(--outline); color: var(--on-surface);
    border-radius: var(--radius-sm); padding: 7px 10px; font-size: 13px; font-family: 'JetBrains Mono', monospace; }
  .fin:focus { outline: none; border-color: var(--primary); }
  .btn { background: var(--primary); color: var(--on-primary); border: none; border-radius: var(--radius-md);
    padding: 9px 16px; font-weight: 600; cursor: pointer; font-size: 14px; }
  .btn:hover { background: var(--primary-hover); }
  .btn.sm { padding: 7px 14px; font-size: 13px; }
  .btn.ghost { background: transparent; color: var(--primary); border: 1px solid var(--outline); }
  .btn.ghost:hover { background: var(--primary-container); }
  .btn.ghost.x { color: var(--on-surface-muted); padding: 7px 10px; }
  .btn.ghost.x:hover { background: var(--error-soft); color: var(--error); }
  .btn:disabled { opacity: .5; cursor: default; }
  .rowacts { display: flex; gap: 6px; justify-content: flex-end; }
  .rtitle { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 12px; }
  .meta { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--on-surface-muted); }
  .empty { color: var(--on-surface-muted); text-align: center; font-size: 14px; padding: 22px; }
  .tblwrap { overflow-x: auto; border: 1px solid var(--outline-variant); border-radius: var(--radius-md);
    background: var(--surface-container-lowest); }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--outline-variant); vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; color: var(--on-surface-muted); font-family: 'JetBrains Mono', monospace; }
  th.num, td.num { text-align: right; font-family: 'JetBrains Mono', monospace; }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--surface-container-low); }
  tr.sel { background: var(--primary-container); }
  tr.totrow { background: var(--surface-container-high); font-weight: 600; }
  tr.totrow:hover { background: var(--surface-container-high); }
  tr.totrow td { border-top: 2px solid var(--outline); }
  td.docid { font-family: 'JetBrains Mono', monospace; color: var(--primary); }
  td.v { font-family: 'JetBrains Mono', monospace; font-weight: 600; white-space: nowrap; }
</style>
