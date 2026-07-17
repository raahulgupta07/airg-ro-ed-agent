<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { auth } from '$lib/stores/auth.svelte';
  let rows = $state<any[]>([]); let loading = $state(true); let error = $state('');
  async function load(){ loading = true; error=''; try { rows = await api.roverHistory(); } catch(e:any){ error=e.message; } loading=false; }
  onMount(load);
  function fmt(v:any){ if(v==null||v==='')return '—'; const n=Number(String(v).replace(/,/g,'')); return isNaN(n)?String(v):n.toLocaleString(); }
  async function dl(url:string,name:string){ try{ await auth.ensureValidToken(); const r=await fetch(url,{headers:auth.token?{Authorization:`Bearer ${auth.token}`}:{}}); const a=document.createElement('a'); a.href=URL.createObjectURL(await r.blob()); a.download=name; a.click(); }catch(e:any){error=e.message;} }
</script>
<svelte:head><title>City Agent ROVER · History</title></svelte:head>
<div class="pg">
  <div class="hd"><div><div class="kick">City Agent ROVER</div><h1>History</h1><p class="sub">Every approved document. Re-open or export any of them anytime.</p></div>
    <button class="btn" onclick={() => dl('/api/rover/export.xlsx','rover_report.xlsx')}><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>Export all (Excel)</button>
  </div>
  {#if error}<div class="err">{error}</div>{/if}
  <div class="tw"><table>
    <thead><tr><th>Document</th><th>Approved</th><th>By</th><th class="n">Customs value</th><th>Status</th><th></th></tr></thead>
    <tbody>
      {#each rows as r (r.doc_id)}
        <tr><td class="doc">{r.document_name || r.doc_id}</td><td>{(r.approved_at||'').replace('T',' ').slice(0,16) || '—'}</td>
          <td>{r.approved_by || '—'}</td><td class="n">{fmt(r.total_customs_value)}</td>
          <td><span class="chip ok">approved</span></td>
          <td><button class="lnk" onclick={() => dl(`/api/rover/documents/${encodeURIComponent(r.doc_id)}/export.xlsx`, r.doc_id+'.xlsx')}>Excel</button></td></tr>
      {/each}
      {#if !rows.length && !loading}<tr><td colspan="6" class="empty">No approved documents yet. Approve one from Process.</td></tr>{/if}
    </tbody>
  </table></div>
</div>
<style>
  .pg{max-width:1180px;margin:0 auto;padding:26px 24px 60px;color:var(--on-surface);font-family:'Inter',sans-serif}
  .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:16px}
  .kick{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--primary);font-weight:600}
  h1{font-family:'Source Serif 4',Georgia,serif;font-size:29px;margin:4px 0 5px} .sub{color:var(--on-surface-muted);margin:0;font-size:14px}
  .btn{display:inline-flex;gap:7px;align-items:center;background:var(--primary);color:var(--on-primary);border:none;border-radius:var(--radius-md);padding:9px 15px;font-weight:600;cursor:pointer;font-size:13.5px}
  .btn:hover{background:var(--primary-hover)}
  .err{background:var(--error-soft);color:var(--error);border:1px solid var(--error-container);border-radius:var(--radius-md);padding:10px 14px;margin-bottom:14px;font-size:14px}
  .tw{overflow:auto;border:1px solid var(--outline-variant);border-radius:var(--radius-lg);background:var(--surface-container-lowest);box-shadow:var(--shadow-xs)}
  table{border-collapse:collapse;width:100%;font-size:13px} th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--outline-variant);white-space:nowrap}
  th{font-family:'JetBrains Mono',monospace;font-size:10.5px;text-transform:uppercase;color:var(--on-surface-muted);background:var(--surface-container-low)} tr:last-child td{border-bottom:none}
  th.n,td.n{text-align:right;font-family:'JetBrains Mono',monospace} td.doc{font-family:'JetBrains Mono',monospace;color:var(--primary)}
  .chip{font-family:'JetBrains Mono',monospace;font-size:11px;border-radius:20px;padding:2px 9px;color:var(--success);background:var(--success-soft)}
  .lnk{border:none;background:none;color:var(--primary);cursor:pointer;font:inherit;font-size:12.5px} .empty{color:var(--on-surface-muted);text-align:center;padding:24px}
</style>
