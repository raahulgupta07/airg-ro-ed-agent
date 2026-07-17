<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  let rows = $state<any[]>([]); let loading = $state(true); let error = $state('');
  async function load(){ loading=true; error=''; try { rows = await api.roverProducts(); } catch(e:any){ error=e.message; } loading=false; }
  onMount(load);
  function fmt(v:any){ if(v==null||v==='')return '—'; const n=Number(String(v).replace(/,/g,'')); return isNaN(n)?String(v):n.toLocaleString(undefined,{maximumFractionDigits:4}); }
</script>
<svelte:head><title>City Agent ROVER · Items</title></svelte:head>
<div class="pg">
  <div class="hd"><div><div class="kick">City Agent ROVER</div><h1>Items <span class="badge">{rows.length}</span></h1><p class="sub">Every product line item extracted across all documents.</p></div></div>
  {#if error}<div class="err">{error}</div>{/if}
  <div class="tw"><table>
    <thead><tr><th>Document</th><th class="n">#</th><th>HS code</th><th>Description</th><th class="n">Qty</th><th>Unit</th><th class="n">Value</th></tr></thead>
    <tbody>
      {#each rows as r, i (i)}
        <tr><td class="doc">{r.document_name || r.doc_id}</td><td class="n">{r.no ?? ''}</td><td class="mono">{r.hs_code ?? '—'}</td>
          <td class="desc">{r.description ?? '—'}</td><td class="n">{fmt(r.quantity)}</td><td>{r.unit ?? '—'}</td><td class="n">{fmt(r.value)}</td></tr>
      {/each}
      {#if !rows.length && !loading}<tr><td colspan="7" class="empty">No items yet.</td></tr>{/if}
    </tbody>
  </table></div>
</div>
<style>
  .pg{max-width:1180px;margin:0 auto;padding:26px 24px 60px;color:var(--on-surface);font-family:'Inter',sans-serif}
  .hd{margin-bottom:16px} .kick{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--primary);font-weight:600}
  h1{font-family:'Source Serif 4',Georgia,serif;font-size:29px;margin:4px 0 5px;display:flex;gap:8px;align-items:center} .sub{color:var(--on-surface-muted);margin:0;font-size:14px}
  .badge{background:var(--surface-container-high);color:var(--on-surface-muted);border-radius:20px;padding:1px 9px;font-size:13px;font-family:'JetBrains Mono',monospace}
  .err{background:var(--error-soft);color:var(--error);border:1px solid var(--error-container);border-radius:var(--radius-md);padding:10px 14px;margin-bottom:14px;font-size:14px}
  .tw{overflow:auto;border:1px solid var(--outline-variant);border-radius:var(--radius-lg);background:var(--surface-container-lowest);box-shadow:var(--shadow-xs)}
  table{border-collapse:collapse;width:100%;font-size:13px} th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--outline-variant);white-space:nowrap;vertical-align:top}
  th{font-family:'JetBrains Mono',monospace;font-size:10.5px;text-transform:uppercase;color:var(--on-surface-muted);background:var(--surface-container-low)} tr:last-child td{border-bottom:none}
  th.n,td.n{text-align:right;font-family:'JetBrains Mono',monospace} td.doc{font-family:'JetBrains Mono',monospace;color:var(--primary)} td.mono{font-family:'JetBrains Mono',monospace} td.desc{white-space:normal;max-width:420px}
  .empty{color:var(--on-surface-muted);text-align:center;padding:24px}
</style>
